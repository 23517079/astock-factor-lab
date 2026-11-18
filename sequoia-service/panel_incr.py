#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""panel_incr.py — factor_panel 增量计算（每交易日只补新增的几天）

为什么需要这个文件
------------------
`factor_panel.py` 和 `rebuild_panel.py` 都只能「按 symbol 全历史重算」：
日常只新增 1 个交易日，却要把 5030 只票的 1566 根 K 线全部重算 24 个因子，
实测 39 分钟。塞不进 16:10 的定时任务，也让「滚动验证 ≤5 分钟」不成立。

本文件只做一件事：**给定目标日期（通常是今天），只算这几天的因子值并写库。**

三个关键常量（都是有依据的，改之前先读理由）
────────────────────────────────────────────
`WARMUP_BARS = 300`
    逐股读取时，从「目标区间首日」再往前多取 300 个交易日。依据：
      * 最大滚动窗口 250 日（`dist_high250` 的 `rolling(250, min_periods=60)`）；
      * 最大 `min_bars` 141（`mom120` 需要 close[t-140]）；
      * 250 + 20（动量跳过的最近 20 日）< 300，留有余量。
    取 300 之后，「读 320 根算完再丢弃预热期」与「读全历史算完再切片」
    **数学等价**（唯一需要论证的是 `obv_slope20`：它用 cumsum，绝对水平随起点
    变化，但因子取 20 日 OLS 斜率，对常数偏移不变，所以仍等价。这条**不靠推理**，
    由 tests/test_incremental_panel.py 强制覆盖实测）。

`MIN_SYMBOLS_PER_DAY = 1200`
    某日行情库里有数据的标的少于这个数，判为「残日」剔除，不算进目标日期。
    典型场景：15:40 的 K 线增量任务只补了一半就失败、或当天只跑了个别板块。
    半日数据进了面板会污染当天的横截面排名（RankIC 按横截面排，少一半票
    会让排名发生系统性偏移），比「今天没数据」危害大得多。

`MAX_DAYS = 30`
    连续 30 个交易日没跑（长假 + 机器关机）时，本次只补最近 30 个交易日，
    剩下的下次继续（`skippedDates` 会带出来）。超过这个量级说明缓存/面板
    已经严重落后，应当提示运维改走全量重建，而不是让定时任务跑一小时。

写入语义
--------
`INSERT OR REPLACE INTO factor_panel(date, symbol, factor, value)`，
唯一键 `(date, symbol, factor)`（生产库上由 `ux_factor_panel_new` 保证）。
**天然幂等**：同一批日期跑两次，第二次全是覆盖写原名，行数不变。

用法
----
    python panel_incr.py --db data/sequoia_v2.db --dry-run     # 只看缺口
    python panel_incr.py --limit-bars 0 --workers 8            # 真跑
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import factor_panel as FP                     # noqa: E402
from factors import compute_series, registry  # noqa: E402

DB = os.path.join(HERE, "data", "sequoia_v2.db")

# 预热窗口（交易日根数）。理由见模块 docstring。
WARMUP_BARS = 300
# 单日最少标的数；低于此判为残日（半日数据）
MIN_SYMBOLS_PER_DAY = 1200
# 单次最多补多少个交易日；超出部分留到下次
MAX_DAYS = 30
# 单事务写入行数：2 万行约 1.4MB，兼顾事务开销与内存
COMMIT_EVERY = 20_000
# 计算一个目标日期「至少」需要多少根 K 线：最大窗口 250 + 动量跳过的 20
MIN_BARS_FOR_EQUIV = 270

INSERT_SQL = ("INSERT OR REPLACE INTO factor_panel(date, symbol, factor, value) "
              "VALUES(?,?,?,?)")


# ────────────────────────────────────────────────────────────
# 计划：找出该补哪些日期
# ────────────────────────────────────────────────────────────
def data_through(conn: sqlite3.Connection) -> Optional[str]:
    """面板「已算到哪天」。

    取**每个因子各自 MAX(date) 的最小值**，而不是全表 `MAX(date)`。
    理由：不同因子可能分批建（冷启动只补了 7 个），全表 MAX 会被最新的那个
    因子掩盖掉其他因子的缺口。逐因子取 MIN 是保守但正确的口径 —— 宁多重算
    几天（幂等，代价极小），也不能漏。

    成本：每个因子一条 `MAX(date) WHERE factor=?`，走 (factor, date) 索引，
    24 个因子合计约 0.2 秒。
    """
    rows = conn.execute(
        "SELECT factor, MAX(date) FROM factor_panel GROUP BY factor").fetchall()
    ends = [r[1] for r in rows if r[1]]
    if not ends:
        return None
    return min(ends)


def trading_dates_between(conn: sqlite3.Connection, after: Optional[str],
                          through: str) -> List[str]:
    """行情库里 (after, through] 之间的交易日（升序）。"""
    sql = "SELECT DISTINCT date FROM stock_daily WHERE date <= ?"
    params: List[Any] = [through]
    if after:
        sql += " AND date > ?"
        params.append(after)
    sql += " ORDER BY date ASC"
    return [r[0] for r in conn.execute(sql, params)]


def symbols_per_date(conn: sqlite3.Connection, dates: Sequence[str]) -> Dict[str, int]:
    """每个交易日行情库里有几只有效数据（用于剔残日）。"""
    if not dates:
        return {}
    sql = ("SELECT date, COUNT(DISTINCT symbol) FROM stock_daily "
           "WHERE date IN (%s) GROUP BY date" % ",".join("?" * len(dates)))
    return {r[0]: int(r[1]) for r in conn.execute(sql, list(dates))}


def plan(conn: sqlite3.Connection, today: Optional[str] = None,
         max_days: int = MAX_DAYS,
         min_symbols: int = MIN_SYMBOLS_PER_DAY) -> Dict[str, Any]:
    """算出本次要算哪些日期。

    Args:
        conn: 数据库连接。
        today: 「今天」的日期字符串；None 表示不设上界（用行情库最新日期）。
               传了之后，`expectedThrough = MAX(stock_daily.date) <= today`，
               这样盘中/周末跑也不会把未来日期当缺口。
        max_days: 本次最多补多少个交易日。
        min_symbols: 单日最少标的数，低于此判残日剔除。

    Returns:
        {"ok", "reason", "dataThrough", "expectedThrough", "lagDays",
         "targetDates": [...], "skippedDates": [...], "residualDates": [...],
         "isFullRebuild": bool}
        `targetDates` 为空且 `lagDays==0` → 已是最新，幂等空跑（退出码 0）。
    """
    t0 = time.time()
    through_panel = data_through(conn)

    sql = "SELECT MAX(date) FROM stock_daily"
    params: List[Any] = []
    if today:
        sql += " WHERE date <= ?"
        params.append(today)
    expected = conn.execute(sql, params).fetchone()[0]

    out: Dict[str, Any] = {
        "ok": True, "reason": None,
        "dataThrough": through_panel, "expectedThrough": expected,
        "targetDates": [], "skippedDates": [], "residualDates": [],
        "skippedByMaxDays": [], "lagDays": 0, "symbolsPerDay": {},
        "isFullRebuild": False, "elapsed": 0.0,
    }
    if expected is None:
        out.update(ok=False, reason="行情库为空（stock_daily 没有数据）")
        return out
    if through_panel is None:
        # 面板一行都没有 → 增量没有意义，必须全量重建
        out.update(ok=False, isFullRebuild=True,
                   reason="factor_panel 为空，请先跑 rebuild_panel.py 做全量重建")
        return out
    if through_panel > expected:
        out.update(reason="面板已领先行情库（%s > %s），无需增量"
                          % (through_panel, expected),
                   elapsed=round(time.time() - t0, 2))
        return out

    candidates = trading_dates_between(conn, through_panel, expected)
    out["lagDays"] = len(candidates)
    counts = symbols_per_date(conn, candidates)
    out["symbolsPerDay"] = counts

    keep: List[str] = []
    residual: List[str] = []
    for d in candidates:
        if counts.get(d, 0) >= min_symbols:
            keep.append(d)
        else:
            residual.append(d)

    # 缺口过多：只补最近 max_days 个，其余留给下次（§3 决策 2）
    if len(keep) > max_days:
        out["skippedByMaxDays"] = keep[:len(keep) - max_days]
        keep = keep[len(keep) - max_days:]

    out["targetDates"] = keep
    out["residualDates"] = residual
    out["skippedDates"] = out["skippedByMaxDays"]     # 给下一轮继续补的日期
    out["elapsed"] = round(time.time() - t0, 2)
    if not keep:
        if residual and not out["skippedByMaxDays"]:
            out.update(ok=False,
                       reason="缺口日全部是残日（标的不满 %d 只）：%s"
                              % (min_symbols, residual[:5]))
    return out


# ────────────────────────────────────────────────────────────
# 预热窗口
# ────────────────────────────────────────────────────────────
def calc_window(conn: sqlite3.Connection, first_target: str, end: str,
                warmup_bars: int = WARMUP_BARS) -> str:
    """算出预热起点（日期字符串）：目标区间首日往前 warmup_bars 个交易日。

    用**全市场交易日历**（`SELECT DISTINCT date FROM stock_daily` 的升序）做
    后退基准，而不是每只票自己的日历 —— 这样一次查询就够，不必逐股查两遍。
    代价：停牌多的票在同样长度的窗口里根数会偏少，因此调用方
    `build_incremental_rows` 会检查根数不足时回退全历史（见其 docstring）。

    日期不够（新股/数据起点太近）时返回行情库的最早日期，即退化为读全历史。
    """
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM stock_daily WHERE date <= ? ORDER BY date ASC",
        (end,))]
    if not dates:
        return first_target
    try:
        i = dates.index(first_target)
    except ValueError:
        # 目标日不在行情库日历里（不该发生，plan 的日期就来自这张表）
        return dates[0]
    return dates[max(0, i - int(warmup_bars))]


# ────────────────────────────────────────────────────────────
# 单只股票 → 待写入行（多进程 worker）
# ────────────────────────────────────────────────────────────
def build_incremental_rows(task: tuple) -> tuple:
    """worker：算一只股票在目标日期上的因子值。

    Args:
        task: (db_path, symbol, calc_start, end, target_dates, factors)
              `calc_start` 是预热起点；`target_dates` 是 frozenset，只写这些日期。

    Returns:
        (symbol, rows, err)；rows 元素为 (date, symbol, factor, value)。
        出错不抛异常（多进程里抛异常难排查），把 err 带回去交给主进程统计。

    数学等价性的两处保证
    --------------------
    1. **丢弃预热期**：只保留 `date in target_dates` 的行落库，预热带进来的
       那些历史行一行都不写（否则会把旧日期重写一遍，虽幂等但白花写入时间）。
    2. **根数不足回退全历史**：停牌多的票在 WARMUP_BARS 个交易日内可能不足
       MIN_BARS_FOR_EQUIV(=270) 根，这时 250 日窗口会退化成 min_periods=60 的
       部分窗口，与全量重算不等价。检测到就直接重读全历史（该票只有几百根，
       代价可以忽略），保证「永远与全量重算逐位相同」。
    """
    db_path, symbol, calc_start, end, target_dates, factors = task
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=60)
        df = FP.load_ohlcv(conn, symbol, end, start=calc_start)
        if df.empty:
            return symbol, [], "no_data"
        if calc_start and len(df) < MIN_BARS_FOR_EQUIV:
            # 预热窗口内根数不足 → 退回全历史，牺牲这一只的速度换正确性
            df = FP.load_ohlcv(conn, symbol, end)
            if df.empty:
                return symbol, [], "no_data"

        series = compute_series(df, factors)
        if series.empty:
            return symbol, [], None

        index = series.index.astype(str)
        mask = index.isin(target_dates)
        sub = series.loc[mask]
        if sub.empty:
            return symbol, [], None

        rows: List[tuple] = []
        names = list(factors)
        for date, values in zip(sub.index.tolist(), sub.to_numpy()):
            for i, factor in enumerate(names):
                value = values[i]
                if value is None or value != value:      # NaN 自己不等于自己
                    continue
                rows.append((str(date), symbol, factor, float(value)))
        return symbol, rows, None
    except Exception as exc:                             # noqa: BLE001 — 兜底，带上原因
        return symbol, [], "%s: %s" % (type(exc).__name__, str(exc)[:160])
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


# ────────────────────────────────────────────────────────────
# 主流程：计算 + 写库
# ────────────────────────────────────────────────────────────
def _main_db_path(conn: sqlite3.Connection) -> Optional[str]:
    """取连接所指向的 main 库文件路径（worker 进程要自己开连接，必须传对路径）。"""
    try:
        for row in conn.execute("PRAGMA database_list"):
            if row[1] == "main":
                return row[2] or None
    except sqlite3.Error:
        return None
    return None


def run_incremental(db_path: Optional[str] = None,
                    target_dates: Optional[Sequence[str]] = None,
                    factors: Optional[Iterable[str]] = None,
                    workers: int = 8, warmup_bars: int = WARMUP_BARS,
                    commit_every: int = COMMIT_EVERY,
                    progress: bool = True,
                    conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """把 `target_dates` 的因子值算出来写进 factor_panel。

    Args:
        db_path: SQLite 路径（多进程 worker 各自按此路径开连接）。
                 **None 时会自动从 `conn` 反查**——绝不能让 worker 默认去读生产库：
                 测试用临时库时必须读临时库，否则会拿生产 K 线算出因子写进临时库。
        target_dates: 要补的日期；None 表示用 `plan()` 的结果。
        factors: 因子名；None 表示全部登记因子。
        workers: 进程数；1 表示主进程串行（测试/调试用）。
        warmup_bars: 预热交易日数（默认 300，见模块 docstring）。
        commit_every: 每多少行提交一次事务。
        progress: 是否打印进度。
        conn: 复用一个已开连接（主进程写入用）；None 则自行 connect。

    Returns:
        {"ok", "targetDates", "factors", "plainRows", "residualDates",
         "symbolsTotal", "symbolsDone", "errors", "elapsed", "plan"}
    """
    factor_names = list(factors) if factors else registry.all_names()
    for name in factor_names:
        registry.get(name)                      # 未登记直接 KeyError

    own_conn = conn is None
    t0 = time.time()
    if own_conn:
        db_path = db_path or DB
        conn = FP.connect(db_path)
    assert conn is not None
    # worker 进程用这个路径开连接，必须是「主进程正在写的那个库」
    db_path = db_path or _main_db_path(conn) or DB

    plan_info: Dict[str, Any] = {}
    try:
        if target_dates is None:
            plan_info = plan(conn)
            if not plan_info.get("ok", True):
                return {"ok": False, "reason": plan_info.get("reason"),
                        "targetDates": [], "factors": factor_names,
                        "plainRows": 0, "residualDates": [], "symbolsTotal": 0,
                        "symbolsDone": 0, "errors": [], "elapsed": 0.0,
                        "plan": plan_info}
            dates = list(plan_info["targetDates"])
        else:
            dates = [str(d) for d in target_dates]
            plan_info = {"ok": True, "reason": None, "dataThrough": None,
                         "expectedThrough": dates[-1] if dates else None,
                         "targetDates": dates, "skippedDates": [],
                         "residualDates": [], "lagDays": len(dates),
                         "isFullRebuild": False}

        if not dates:
            return {"ok": True, "reason": "已是最新，无需增量（幂等空跑）",
                    "targetDates": [], "factors": factor_names, "plainRows": 0,
                    "residualDates": [], "symbolsTotal": 0, "symbolsDone": 0,
                    "errors": [], "elapsed": round(time.time() - t0, 2),
                    "plan": plan_info}

        first, end = dates[0], dates[-1]
        calc_start = calc_window(conn, first, end, warmup_bars)
        symbols = FP.list_symbols(conn, start=None, end=end)
        if progress:
            print("[incr] 目标 %d 个交易日（%s ~ %s），%d 只票 × %d 个因子；"
                  "预热自 %s 起"
                  % (len(dates), first, end, len(symbols), len(factor_names), calc_start),
                  flush=True)

        tasks = [(db_path, s, calc_start, end, frozenset(dates), factor_names)
                 for s in symbols]
        iterator: Iterable[tuple]
        pool = None
        if workers and workers > 1 and len(tasks) > 1:
            from multiprocessing import get_context
            ctx = get_context("spawn")          # spawn：Windows 唯一可靠的选择
            pool = ctx.Pool(processes=min(workers, len(tasks)))
            iterator = pool.imap_unordered(build_incremental_rows, tasks, chunksize=4)
        else:
            iterator = (build_incremental_rows(t) for t in tasks)

        buf: List[tuple] = []
        written = 0
        done = 0
        no_data = 0
        errors: List[Dict[str, str]] = []
        # 逐日统计实际写入的标的数（写完后用它报残日，主流程据此提示）
        per_date_syms: Dict[str, set] = {d: set() for d in dates}

        def flush(force: bool = False) -> None:
            nonlocal buf, written
            if len(buf) >= commit_every or (force and buf):
                conn.executemany(INSERT_SQL, buf)   # type: ignore[union-attr]
                conn.commit()                       # type: ignore[union-attr]
                written += len(buf)
                buf = []

        t_calc = time.time()
        try:
            for symbol, rows, err in iterator:
                done += 1
                if err == "no_data":
                    no_data += 1
                elif err:
                    errors.append({"symbol": symbol, "error": err})
                elif rows:
                    for row in rows:
                        per_date_syms.setdefault(row[0], set()).add(symbol)
                    buf.extend(rows)
                    if len(buf) >= commit_every:
                        flush()
                if progress and (done % 500 == 0 or done == len(symbols)):
                    elapsed = time.time() - t_calc
                    rate = done / elapsed if elapsed > 0 else 0.0
                    print("      %d/%d  写入 %s 行  无数据 %d  失败 %d  %.1f 只/秒"
                          % (done, len(symbols), f"{written + len(buf):,}",
                             no_data, len(errors), rate), flush=True)
        finally:
            if pool is not None:
                try:
                    pool.close()
                    pool.join()
                except Exception:
                    pool.terminate()
                    pool.join()
            flush(force=True)

        residual = [d for d in dates
                    if 0 < len(per_date_syms.get(d, ())) < MIN_SYMBOLS_PER_DAY]
        return {"ok": True, "reason": None, "targetDates": dates,
                "factors": factor_names, "plainRows": written,
                "symbolsTotal": len(symbols), "symbolsDone": done,
                "symbolsNoData": no_data, "errors": errors[:50],
                "residualDates": residual,
                "symbolsPerDate": {d: len(v) for d, v in per_date_syms.items()},
                "calcStart": calc_start,
                "elapsed": round(time.time() - t0, 2), "plan": plan_info}
    finally:
        if own_conn and conn is not None:
            conn.close()


def count_rows(conn: sqlite3.Connection, dates: Sequence[str]) -> int:
    """统计指定日期的面板行数（幂等性验证用）。"""
    if not dates:
        return 0
    sql = ("SELECT COUNT(*) FROM factor_panel WHERE date IN (%s)"
           % ",".join("?" * len(dates)))
    return int(conn.execute(sql, list(dates)).fetchone()[0])


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────
def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="factor_panel 增量计算")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--dates", default="", help="逗号分隔的目标日期；默认自动探测缺口")
    ap.add_argument("--today", default=None, help="今天（YYYY-MM-DD），用于限定缺口上界")
    ap.add_argument("--factors", default=None, help="逗号分隔，默认全部")
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--warmup-bars", type=int, default=WARMUP_BARS)
    ap.add_argument("--max-days", type=int, default=MAX_DAYS)
    ap.add_argument("--dry-run", action="store_true", help="只打印计划，不写库")
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=120)
    try:
        pl = plan(conn, today=args.today, max_days=args.max_days)
        print("[plan] %s" % {k: pl[k] for k in
                             ("ok", "dataThrough", "expectedThrough", "lagDays",
                              "targetDates", "skippedDates", "residualDates", "reason")})
        if args.dry_run:
            return 0 if pl.get("ok") else 2
        if not pl.get("ok"):
            return 2
    finally:
        conn.close()

    dates = [d.strip() for d in args.dates.split(",") if d.strip()] or None
    factors = [f.strip() for f in args.factors.split(",") if f.strip()] if args.factors else None
    res = run_incremental(args.db, dates, factors, workers=args.workers,
                          warmup_bars=args.warmup_bars, progress=True)
    print("[done] %s" % {k: res[k] for k in
                         ("ok", "plainRows", "symbolsDone", "residualDates", "elapsed")})
    return 0 if res.get("ok") else 2


if __name__ == "__main__":
    raise SystemExit(main())
