#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""freshness.py — 「数据新鲜度」的唯一实现（因子体检页 + 智能选股页共用）

为什么必须只有一个实现
----------------------
改造前两个页面各自判断「数据是不是最新」，而且都用
`new Date(某字段)` 推算 —— 于是页面上的「数据截止」可以比真实内容还新。
现在判定规则只在这里，页面只做展示（T05 的 `freshness.js` 不再自己算日期）。

判定表（说明书 §8，先命中先返回）
--------------------------------
    1. lastCalcOk === false            → failed   reason = error[:120]
    2. running !== null                → pending  reason = "今日重算进行中"
    3. lagTradingDays === 0            → fresh    reason = None
    4. lagTradingDays >= 2             → stale    reason = "已过期 N 个交易日"
    5. 今日工作日且 now >= factor_time → stale    reason = "今日未完成重算"
    6. 其他（未到点 / 非交易日）        → pending  reason = "今日尚未到重算时点"

`failed` 优先级最高（共享知识第 3 条）：一次失败的运行必须压过「看起来还不算旧」，
否则用户会以为昨天的数据是今天算的。

`skipped` = 「本次**根本没开始算**」（调度器门禁拦住，如 K 线未更新）
------------------------------------------------------------------
它与 `lastCalcOk === false`（算了但出错）是两回事，所以：
  * `state` 仍按 `lag` 判（数据旧就是旧，不能因为「没算」就刷成 failed 红字）；
  * 只往 `reason` 里补一句「本次未开始：<原因>」，且优先级高于常规文案
    （`failed` 例外 —— 算了但出错比没开始更严重）；
  * 有效性看 **`skipped.at > lastCalcAt`**，不看「是不是今天」：昨晚被门禁拦住、
    今早才打开页面，这条原因恰恰最该显示；而一旦之后成功算过，`lastCalcAt`
    就会越过它，旧提示自动失效，不会变成天天挂着的墙纸。

`dataThrough` 必须用**全表 MAX(date)**
--------------------------------------
刻意不 import `panel_incr.data_through()`：那个函数算的是「逐因子 MAX(date) 的
最小值」，用来定增量缺口（哪个因子最落后）。**同名不同义**，混用会让
「缓存落后于库」被误判成「数据已最新」。

成本
----
    SELECT MAX(date) FROM factor_panel                                  0.0s
    SELECT MAX(date) FROM stock_daily WHERE date <= ?                   约 5s
    SELECT COUNT(DISTINCT date) FROM stock_daily WHERE date > ? 且 <= ?  约 0.2s

约 5 秒对「每 30 秒刷新一次的页面」仍然偏重，所以提供 `cached()`（默认 60 秒 TTL）。

用法
----
    import freshness
    fresh = freshness.compute(conn, factor_time="16:10")
    fresh = freshness.cached(conn, factor_time="16:10", ttl=60)
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from datetime import datetime
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import factor_run as FR                        # noqa: E402
import factor_ts as TS                         # noqa: E402
import panel_cache as PC                       # noqa: E402
import sched_time                              # noqa: E402

# 4 态枚举（共享知识第 3 条：小写，只此 4 个，`failed` 优先）
STATUS_FRESH = "fresh"
STATUS_PENDING = "pending"
STATUS_STALE = "stale"
STATUS_FAILED = "failed"
STATUSES = (STATUS_FRESH, STATUS_PENDING, STATUS_STALE, STATUS_FAILED)

# 状态 → 页面文案（唯一来源，前端不硬编码）
STATUS_LABELS: Dict[str, str] = {
    STATUS_FRESH: "数据已更新",
    STATUS_PENDING: "今日待更新",
    STATUS_STALE: "数据已过期",
    STATUS_FAILED: "上次计算失败",
}

# 因子任务的默认时点；T04 会把它放进 scheduler.DEFAULT_CFG["factor_time"]
DEFAULT_FACTOR_TIME = "16:10"

# `lagTradingDays >= 2` 才算 stale：滞后 1 天是正常的「今天还没算」窗口，
# 判 stale 会让页面在每天 15:40~16:10 之间闪一次红。
LAG_STALE_THRESHOLD = 2

# 「数据到位、但新流程一次都没跑过」时的说明（判定表第 2 条的 reason）。
# 场景：本轮改造后首次部署，面板来自旧版全量重建 / --build-all，
# `factor_run.json` 还是空的。此时 `lag == 0` 会判 fresh —— 数据到位是事实，
# 但页面会并排显示「最新」和「上次计算 —」，等于在说一次没发生的更新。
# status 不动（那是数据事实），只把来源说清；明天首跑成功后这句自然消失。
NO_CALC_REASON = ("新流程尚未运行过：当前面板来自旧版全量重建，"
                  "尚未经过每日增量与滚动验证")

# 进程内缓存（页面高频轮询用）
_CACHE: Dict[str, Dict[str, Any]] = {}
_CACHE_TTL_DEFAULT = 60.0


def _db_key(conn: Optional[sqlite3.Connection]) -> str:
    """连接的主库绝对路径（作为进程内缓存键；取不到就用 "<memory>"）。"""
    if conn is None:
        return "<no-conn>"
    try:
        for row in conn.execute("PRAGMA database_list").fetchall():
            if len(row) >= 3 and row[1] == "main":
                return os.path.abspath(str(row[2]))
    except sqlite3.Error:
        pass
    return "<unknown>"


def _scalar(conn: Optional[sqlite3.Connection], sql: str,
            params: tuple = ()) -> Any:
    """取单值；连接为空 / 表不存在 / 查询失败一律返回 None（判定表能继续走）。"""
    if conn is None:
        return None
    try:
        row = conn.execute(sql, params).fetchone()
    except sqlite3.Error:
        return None
    return None if not row else row[0]


def data_through(conn: Optional[sqlite3.Connection]) -> Optional[str]:
    """面板数据截止日 = `SELECT MAX(date) FROM factor_panel`（全表 MAX，0.0s）。

    ⚠ 与 `panel_incr.data_through()` **同名不同义**，不要互相 import：
    那个是「逐因子 MAX(date) 的最小值」（增量缺口用），这个是「面板最晚一天」。
    """
    got = _scalar(conn, "SELECT MAX(date) FROM factor_panel")
    return None if got is None else str(got)


def expected_through(conn: Optional[sqlite3.Connection],
                     today: Optional[str] = None) -> Optional[str]:
    """行情库「本应算到」的日期 = `MAX(date) FROM stock_daily WHERE date <= today`。"""
    day = today or sched_time.date_str()
    got = _scalar(conn, "SELECT MAX(date) FROM stock_daily WHERE date <= ?", (day,))
    return None if got is None else str(got)


def lag_trading_days(conn: Optional[sqlite3.Connection], through: Optional[str],
                     expected: Optional[str]) -> int:
    """面板落后行情多少个交易日（两个日期之间 `stock_daily` 的真实交易日数）。"""
    if not through or not expected or str(through) >= str(expected):
        return 0
    got = _scalar(conn,
                  "SELECT COUNT(DISTINCT date) FROM stock_daily "
                  "WHERE date > ? AND date <= ?", (str(through), str(expected)))
    try:
        return int(got or 0)
    except (TypeError, ValueError):
        return 0


def _cache_verify() -> Dict[str, Any]:
    """缓存/时间序列侧的探针（对账状态、ts 轴、面板轴）。

    全部只读 JSON 头，**不加载任何 npy/npz** —— 这是页面高频调用的路径，
    不能为了一个徽标去读 24 个矩阵。
    """
    out: Dict[str, Any] = {"reconcile": {}, "ts": {}, "panel": {}}
    try:
        ts_meta = TS.load_meta()
    except Exception:                                    # noqa: BLE001 — 探针不该阻断判定
        ts_meta = {}
    factors = ts_meta.get("factors") or {}
    lasts = [str((v or {}).get("last")) for v in factors.values() if (v or {}).get("last")]
    out["reconcile"] = ts_meta.get("reconcile") or {}
    out["ts"] = {
        "initialized": bool(factors),
        "factors": len(factors),
        "last": max(lasts) if lasts else None,
        "first": min((str((v or {}).get("first")) for v in factors.values()
                      if (v or {}).get("first")), default=None),
        "suspects": [name for name, v in sorted(factors.items())
                     if str((v or {}).get("state") or "ok") == "suspect"],
        "builtAt": ts_meta.get("builtAt"),
        "lastAppendAt": ts_meta.get("lastAppendAt"),
        "params": {k: ts_meta.get(k) for k in ("horizon", "entry", "minStocks", "nGroups")},
    }
    try:
        info = PC.info()
        out["panel"] = {"sig": info.get("sig"), "factors": info.get("factors"),
                        "dates": info.get("dates"), "symbols": info.get("symbols"),
                        "sizeMB": info.get("sizeMB")}
    except Exception:                                    # noqa: BLE001
        out["panel"] = {}
    return out


def compute(conn: Optional[sqlite3.Connection],
            factor_time: str = DEFAULT_FACTOR_TIME,
            today: Optional[str] = None,
            now: Optional[Any] = None) -> Dict[str, Any]:
    """按判定表算出新鲜度（唯一实现，无副作用）。

    Args:
        conn: 数据库连接（None 时只能给出 pending/failed，不报错）。
        factor_time: 因子任务的时点 `"HH:MM"`（默认 16:10）。
        today: 覆盖「今天」（测试用）；None 取系统时间。
        now: 覆盖当前时刻（`datetime`，测试用）；None 取系统时间。

    Returns:
        {
          "ok": True, "status", "statusLabel", "reason",
          "dataThrough", "dataThroughWeekday", "expectedThrough", "lagTradingDays",
          "lastCalcAt", "lastCalcOk", "lastCalcError", "partial", "warnings",
          "runId", "cost", "nextRunAt", "factorTime", "evaluatedAt", "cacheVerify"
        }

    ⚠ 字段边界（说明书 §8「状态文件 vs 导出文件」）：本函数返回的 DTO 会被
    `factor_api.json` / `factor_score.json` **固化到磁盘**，所以**不放任何
    「当前正在做什么」的瞬态字段**（`running` / `busy` / `phase`）。
    原因很实在：导出本身就发生在进程内部，导出那一刻 `running` 恒为 true，
    写进文件后没人再导一次，那句 `running:true` 就成了永久假话。
    页面要显示「正在重算」→ 读 `/api/scheduler/status.busy.factor`（实时）。
    `factor_run.json` 里的 `running` 保留不动 —— 它是给实时读的状态文件。
    """
    moment = now or sched_time.now()
    day = today or moment.strftime(sched_time.DATE_FMT)
    run = FR.read()
    last = FR.last_calc()

    # `running` 是持久化的：进程被强杀时它会永久停在 true，页面就永久卡在
    # 「今日重算进行中」。所以只在 `startedAt` 距现在 ≤ 90 分钟内才当作有效。
    running_obj = run.get("running") if isinstance(run.get("running"), dict) else None
    running_stale = False
    stale_started = None
    if running_obj and FR.running_is_stale(running_obj, moment.strftime(FR.ISO_FMT)):
        running_stale = True
        stale_started = running_obj.get("startedAt")
        running_obj = None                    # 过期 → 当作没有进程在跑，继续按 lag 判

    # `skipped` = 「本次根本没开始算」（调度器门禁拦住，例如 K 线还没更新）。
    #
    # 有效性判据是「**晚于上次真实完成时刻**（`lastCalcAt`）」，**不是**「是不是今天」：
    #   11-16 16:10 门禁拦住 → 11-17 早上用户打开页面，按「今天」过滤会把这条
    #   最有用的原因一起扔掉，页面只剩「已过期 N 个交易日」，用户只能自己猜
    #   「是我关机了？还是没登录？」——而答案明明就存在文件里。
    # 反过来，只要之后成功算过（`lastCalcAt` 越过它），这条自然失效：
    # 既能把「昨晚为什么没跑」说清楚，也不会让一条旧提示变成天天挂着的墙纸。
    skipped = run.get("skipped") if isinstance(run.get("skipped"), dict) else None
    if skipped:
        skip_at = str(skipped.get("at") or "")
        last_at = str(last.get("at") or "")
        # 没有时刻（写坏了）或早于上次完成时刻（之后已经成功算过）→ 一律忽略
        if not skip_at or (last_at and skip_at <= last_at):
            skipped = None

    through = data_through(conn)
    expected = expected_through(conn, day)
    lag = lag_trading_days(conn, through, expected)

    status = STATUS_PENDING
    reason: Optional[str] = None

    if last["ok"] is False:
        # 1. 上次失败压过一切（否则用户会以为昨天的数据是今天算的）
        status = STATUS_FAILED
        reason = FR.truncate_error(last.get("error")) or "上次重算失败（未记录原因）"
    elif lag == 0 and through:
        # 2. 已追平行情 —— 必须排在「正在跑」前面。
        # 理由：这是两个维度。数据到不到位看 `lag`，有没有进程在跑看 `running`。
        # 顺序写反会出现「16:08 已经算完、页面却说今日待更新」，而导出文件之后
        # 不会有人再写一次，这句假话会挂一整天。
        # `fresh` 的 reason 允许非空：中断提示（下面）要能带出来。
        status = STATUS_FRESH
        reason = None
        if running_stale:
            # ⚠ 过期只说明「进程可能死了」，**不说明数据是新的**。
            # 这里能说 fresh 是因为 lag==0（数据确实追平了）；若 lag>0，
            # 下面会落到 stale —— 绝不能因为 running 过期就直接 fresh。
            reason = ("上次运行未正常结束（startedAt=%s，已超过 %d 分钟），但数据已到位"
                      % (stale_started, FR.RUNNING_STALE_SEC // 60))
        elif not last.get("at"):
            # 数据到位、但**新流程一次都没跑过**（`factor_run.json` 是空的）。
            # 不写这一句，页面会并排出现「最新」和「上次计算 —」——等于声称
            # 一次没发生的更新。status 保持 fresh（数据到位是事实），只交代来源。
            reason = NO_CALC_REASON
    elif running_obj:
        # 3. 正在跑，且数据还没到位（lag > 0）→ 这才叫「今日重算进行中」
        status = STATUS_PENDING
        reason = "今日重算进行中"
    elif lag >= LAG_STALE_THRESHOLD:
        # 4. 落后 2 个交易日以上
        status = STATUS_STALE
        reason = "已过期 %d 个交易日" % lag
    elif sched_time.is_workday(moment) and sched_time.due(moment, factor_time):
        # 5. 今天该跑但还没跑成
        status = STATUS_STALE
        reason = "今日未完成重算"
    else:
        # 6. 未到点 / 非交易日
        status = STATUS_PENDING
        reason = "今日尚未到重算时点"

    # `running` 过期但数据没追平 → 必须明确说「疑似中断」，不能让用户以为只是「没到点」。
    if running_stale and status in (STATUS_STALE, STATUS_PENDING):
        reason = ("上次运行疑似中断，数据停在 %s（startedAt=%s，已超过 %d 分钟）"
                  % (through or "未知", stale_started, FR.RUNNING_STALE_SEC // 60))

    # reason 优先级：**没开始算** > 常规文案。
    # `failed` 例外：那是「算了但出错」，比「没开始」更严重，保留错误原因。
    # 页面规则（说明书 §8）：reason 非空就显示，**不要按 state 过滤**——
    # 否则「数据已到位但上次收尾异常」这种 fresh + reason 会被状态吃掉。
    if skipped and status != STATUS_FAILED:
        # 不是今天的 skipped 要带上时刻：跨夜打开页面时，读者得知道
        # 「这条说的不是今天，是昨晚那次」，否则「本次未开始」会被误读成现在。
        skip_at = str(skipped.get("at") or "")
        stamp = skip_at[:16].replace("T", " ") if skip_at else ""
        if stamp and skip_at[:10] != day:
            reason = "本次未开始（%s）：%s" % (stamp, skipped.get("reason"))
        else:
            reason = "本次未开始：%s" % skipped.get("reason")

    # 「今天这一档跑成功了吗」——决定 nextRunAt 是今天还是下个工作日
    done_today = bool(last.get("ok") and str(last.get("at") or "").startswith(day))
    next_run_at = sched_time._next_run(moment, factor_time, done_today)

    payload = {
        "ok": True,
        "status": status,
        "statusLabel": STATUS_LABELS.get(status, status),
        "reason": reason,
        "dataThrough": through,
        "dataThroughWeekday": (sched_time.weekday_cn(_parse_date(through))
                               if through else None),
        "expectedThrough": expected,
        "lagTradingDays": lag,
        "lastCalcAt": last.get("at"),
        "lastCalcOk": last.get("ok"),
        "lastCalcError": FR.truncate_error(last.get("error")),
        # `partial` = 主体成功但有环节缺失（目前只有「滚动验证本次无有效期」）。
        # 它**不参与** status 判定：产物确实更新了，status 该是 fresh；
        # 页面拿它去在滚动卡片上写一行灰字，而不是把整页刷成失败红字。
        "partial": bool(last.get("partial")),
        "warnings": list(run.get("warnings") or []),
        # `running` 刻意**不在这里**（见 docstring）：它是瞬态的。
        "runId": run.get("runId"),
        "cost": run.get("cost"),
        "nextRunAt": next_run_at,
        "factorTime": factor_time,
        "evaluatedAt": FR.iso_now(),
        "cacheVerify": _cache_verify(),
    }
    return payload


def _parse_date(text: Optional[str]) -> Optional[datetime]:
    """`"YYYY-MM-DD"` → datetime（解析失败返回 None，调用方据此跳过星期几）。"""
    if not text:
        return None
    try:
        return datetime.strptime(str(text)[:10], "%Y-%m-%d")
    except (ValueError, TypeError):
        return None


def cached(conn: Optional[sqlite3.Connection],
           factor_time: str = DEFAULT_FACTOR_TIME,
           ttl: float = _CACHE_TTL_DEFAULT,
           today: Optional[str] = None,
           now: Optional[Any] = None) -> Dict[str, Any]:
    """带 TTL 的 `compute()`（默认 60 秒），供 HTTP 接口高频调用。

    缓存键包含 `factor_time` 与「今天」：跨过零点或用户改了时点后必须立即重算，
    否则页面会给出与设置不一致的下次运行时间。
    """
    moment = now or sched_time.now()
    day = today or moment.strftime(sched_time.DATE_FMT)
    key = "%s|%s|%s" % (_db_key(conn), factor_time, day)
    slot = _CACHE.get(key)
    stamp = time.time()
    # ttl <= 0 表示「不缓存，每次重算」—— 注意不能写成 `ttl <= 0 or ...`：
    # 那等于「ttl=0 时永远命中缓存」，与调用方的意图正好相反。
    if slot and float(ttl) > 0 and (stamp - float(slot.get("at") or 0)) <= float(ttl):
        return dict(slot["value"])
    value = compute(conn, factor_time=factor_time, today=day, now=moment)
    _CACHE[key] = {"at": stamp, "value": value}
    # 只保留最近 8 个键，防止长时间运行后字典无限增长
    if len(_CACHE) > 8:
        for stale_key in sorted(_CACHE, key=lambda k: _CACHE[k]["at"])[:-8]:
            _CACHE.pop(stale_key, None)
    return value


def reset_cache() -> None:
    """清空进程内缓存（改配置 / 测试用）。"""
    _CACHE.clear()


def main(argv: Optional[Any] = None) -> int:
    """打印当前新鲜度（排障用：`python freshness.py`）。"""
    import argparse
    import json

    ap = argparse.ArgumentParser(description="打印数据新鲜度判定")
    ap.add_argument("--db", default=os.path.join(HERE, "data", "sequoia_v2.db"))
    ap.add_argument("--factor-time", default=DEFAULT_FACTOR_TIME)
    ap.add_argument("--today", default=None)
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=120)
    try:
        payload = compute(conn, factor_time=args.factor_time, today=args.today)
    finally:
        conn.close()
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
