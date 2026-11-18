#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_freshness.py — 数据新鲜度判定表（说明书 §8）的验收测试

判定表 6 条，先命中先返回：
    1. lastCalcOk === false                       → failed
    2. lagTradingDays === 0                       → fresh（数据已到位）
    3. running !== null（且 lag > 0）             → pending（今日重算进行中）
    4. lagTradingDays >= 2                        → stale（已过期 N 个交易日）
    5. 今日工作日且 now >= factor_time            → stale（今日未完成重算）
    6. 其他                                       → pending（今日尚未到重算时点）

⚠ 第 2/3 条的顺序是刻意的：**「数据到不到位」看 lag，「有没有进程在跑」看 running**，
这是两个维度。顺序写反会出现「16:08 已经算完、页面却显示今日待更新」——
用户只会以为今天没跑成。

⚠ `running` **不出现在返回的 DTO 里**（说明书 §8 字段边界）：DTO 会被固化进
`factor_api.json` / `factor_score.json`，而导出就在进程内部发生，那一刻
`running` 恒为 true —— 固化必然失真。页面要看「正在重算」请读
`/api/scheduler/status.busy.factor`（实时）。本模块只把 `running` 用于内部判定，
且带 **90 分钟有效期**：强杀留下的脏 running 不能被当成「还在跑」。

⚠ `reason` 允许在 `fresh` 下非空（中断提示），页面规则是
**「reason 非空就显示，不按 state 过滤」**。

⚠ `skipped`（「本次根本没开始算」）的有效性看 **`skipped.at > lastCalcAt`**，
不看「是不是今天」：昨晚被门禁拦住、今早才打开页面，这条原因恰恰最该显示；
而一旦之后成功算过，`lastCalcAt` 就越过它，旧提示自动失效。

重点验证的三件事：
  * **`dataThrough` 必须是全表 `MAX(date)`**，不是
    `panel_incr.data_through()`（逐因子 MAX 的最小值）—— 同名不同义。
    用例：`mom20` 到 11-13、`turn20` 只到 11-10 → 判定必须基于 11-13；
  * **`failed` 优先**：一次失败的运行要压过「看起来还不算旧」；
  * `nextRunAt` 与 `/api/scheduler/status` 同源（都调 `sched_time._next_run`）。

全部在临时库 + 临时 `factor_run.json` 上跑，不碰生产文件。

运行：
    python tests/test_freshness.py
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
TESTS = os.path.dirname(os.path.abspath(__file__))
for path in (HERE, TESTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import factor_run as FR                         # noqa: E402
import freshness                               # noqa: E402
import sched_time                               # noqa: E402
import prod_guard                               # noqa: E402

PASS = 0
FAIL = 0
FAILS = []

# 交易日（工作日）：2026-11-02 ~ 2026-11-13
TRADING_DAYS = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05",
                "2026-11-06", "2026-11-09", "2026-11-10", "2026-11-11",
                "2026-11-12", "2026-11-13"]
TODAY = "2026-11-16"          # 周一
FACTOR_TIME = "16:10"


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  — %s" % detail) if detail else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  ✗ %s%s" % (name, ("  — %s" % detail) if detail else ""))


class Env:
    """临时库（factor_panel + stock_daily）+ 临时 factor_run.json。"""

    def __init__(self, root: str) -> None:
        self.dir = root
        os.makedirs(self.dir, exist_ok=True)
        self.db = os.path.join(self.dir, "fresh.db")
        self.run_path = os.path.join(self.dir, "factor_run.json")
        self.ts_dir = os.path.join(self.dir, "factor_ts")
        os.makedirs(self.ts_dir, exist_ok=True)
        os.environ["FACTOR_RUN_PATH"] = self.run_path
        os.environ["FACTOR_RUN_LOCK_PATH"] = os.path.join(self.dir, "factor_run.lock")
        os.environ["FACTOR_TS_DIR"] = self.ts_dir

        self.conn = sqlite3.connect(self.db)
        self.conn.execute("CREATE TABLE stock_daily (date TEXT, symbol TEXT, close REAL)")
        self.conn.execute("CREATE TABLE factor_panel (date TEXT, symbol TEXT, "
                          "factor TEXT, value REAL)")
        self.conn.executemany(
            "INSERT INTO stock_daily(date, symbol, close) VALUES(?,?,?)",
            [(d, "S%04d" % (i % 5), 10.0) for d in TRADING_DAYS for i in range(5)])
        self.conn.commit()
        freshness.reset_cache()
        self.set_panel_through(TRADING_DAYS[-1])

    def set_panel_through(self, last_date: str, lagging_factor: str = "turn20",
                          lagging_through: str | None = None) -> None:
        """重建 factor_panel：全因子到 `last_date`，`lagging_factor` 只到更早。"""
        self.conn.execute("DELETE FROM factor_panel")
        rows = []
        for date in TRADING_DAYS:
            if date <= last_date:
                rows.append((date, "S0000", "mom20", 0.1))
            lim = lagging_through or last_date
            if date <= lim:
                rows.append((date, "S0000", lagging_factor, 0.2))
        self.conn.executemany(
            "INSERT INTO factor_panel(date, symbol, factor, value) VALUES(?,?,?,?)", rows)
        self.conn.commit()
        freshness.reset_cache()

    def write_run(self, ok_value: bool | None = True, error: str | None = None,
                  running: dict | None = None, finished: str | None = None,
                  skipped: dict | None = None) -> None:
        payload = {"runId": "20261116T161012",
                   "startedAt": finished or "2026-11-16T16:10:12",
                   "finishedAt": finished if ok_value is not None else None,
                   "targetDates": ["2026-11-13"],
                   "ok": ok_value, "cost": 512.3, "error": error,
                   "trigger": "scheduler", "verify": {}, "running": running,
                   "skipped": skipped}
        with open(self.run_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)

    def compute(self, now: datetime | None = None, today: str = TODAY):
        return freshness.compute(self.conn, factor_time=FACTOR_TIME,
                                 today=today, now=now)

    def cleanup(self) -> None:
        for key in ("FACTOR_RUN_PATH", "FACTOR_RUN_LOCK_PATH", "FACTOR_TS_DIR"):
            os.environ.pop(key, None)
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)


# ────────────────────────────────────────────────────────────
def case_table(tmp: str) -> None:
    print("\n[1] 判定表 6 条（先命中先返回）")
    env = Env(tmp)
    try:
        # 规则 3：已追平
        env.write_run(ok_value=True, finished="2026-11-16T16:12:00")
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("lag=0 且上次成功 → fresh", got["status"] == "fresh", got["status"])
        ok("dataThrough 是行情库最新日", got["dataThrough"] == "2026-11-13",
           str(got["dataThrough"]))
        ok("fresh 时 reason 为 None", got["reason"] is None, str(got["reason"]))

        # 规则 4：落后 2 个交易日
        env.set_panel_through("2026-11-11")
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("lag=2 → stale 且文案带交易日数",
           got["status"] == "stale" and got["reason"] == "已过期 2 个交易日",
           "%s / %s" % (got["status"], got["reason"]))
        ok("lagTradingDays 按 stock_daily 真实交易日数算", got["lagTradingDays"] == 2,
           str(got["lagTradingDays"]))

        # 规则 5：落后 1 天 + 工作日已过点
        env.set_panel_through("2026-11-12")
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("lag=1 且已过 16:10 → stale「今日未完成重算」",
           got["status"] == "stale" and got["reason"] == "今日未完成重算",
           "%s / %s" % (got["status"], got["reason"]))

        # 规则 6a：未到点
        got = env.compute(now=datetime(2026, 11, 16, 9, 0))
        ok("lag=1 且未到 16:10 → pending「今日尚未到重算时点」",
           got["status"] == "pending" and got["reason"] == "今日尚未到重算时点",
           "%s / %s" % (got["status"], got["reason"]))

        # 规则 6b：非交易日（周六）
        got = env.compute(now=datetime(2026, 11, 14, 16, 30), today="2026-11-14")
        ok("周六即使过了 16:10 也是 pending（非交易日）",
           got["status"] == "pending", "%s / %s" % (got["status"], got["reason"]))

        # 规则 2：running 非空
        env.write_run(ok_value=True, finished="2026-11-16T16:12:00",
                      running={"runId": "run2", "startedAt": "2026-11-16T16:10:12",
                               "phase": "compute", "targetDates": ["2026-11-13"]})
        got = env.compute(now=datetime(2026, 11, 16, 16, 20))
        ok("running 非空 → pending「今日重算进行中」",
           got["status"] == "pending" and got["reason"] == "今日重算进行中",
           "%s / %s" % (got["status"], got["reason"]))
        ok("**running 不出现在 DTO 里**（导出型文件只放结果，不放瞬态状态）",
           "running" not in got, str(sorted(got)))

        # 规则 2 与 3 的先后：数据已到位时，`running` 不该把徽标按回「待更新」。
        # 真实场景：16:08 算完 → 16:09 页面重新导出（另一次 retry 正占着锁）→
        # 若顺序写反，页面会显示「今日待更新」而实际已经更新完了。
        env.set_panel_through("2026-11-13")          # 追平行情
        got = env.compute(now=datetime(2026, 11, 16, 16, 20))
        ok("lag=0 且 running 非空 → 仍是 fresh（数据已到位就不能说「待更新」）",
           got["status"] == "fresh" and got["reason"] is None,
           "%s / %s" % (got["status"], got["reason"]))
        ok("此时 DTO 里同样没有 running（页面读 status.busy.factor）",
           "running" not in got, str(sorted(got)))

        # 规则 1：上次失败
        env.write_run(ok_value=False, error="RuntimeError: staging 校验未通过")
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("ok=false → failed",
           got["status"] == "failed" and "staging" in (got["reason"] or ""),
           "%s / %s" % (got["status"], got["reason"]))
        ok("lastCalcOk 透出为 false", got["lastCalcOk"] is False, str(got["lastCalcOk"]))

        # failed 优先于 stale
        env.set_panel_through("2026-11-09")
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("failed 压过 stale（lag=4 时仍是 failed）",
           got["status"] == "failed", "%s / lag=%d" % (got["status"], got["lagTradingDays"]))

        # 从未跑过
        if os.path.exists(env.run_path):
            os.remove(env.run_path)
        freshness.reset_cache()
        got = env.compute(now=datetime(2026, 11, 16, 9, 0))
        ok("从未跑过时 ok=None，不误判成 failed",
           got["lastCalcOk"] is None and got["status"] in ("pending", "stale"),
           "%s / ok=%s" % (got["status"], got["lastCalcOk"]))

        # 「从未跑过」+ 数据已追平：fresh 是事实，但必须交代来源。
        # 不交代的话页面会并排显示「最新」和「上次计算 —」——等于声称一次没发生的更新
        # （本轮改造的真实情况：面板来自旧版全量重建 / --build-all，factor_run.json 是空的）。
        env.set_panel_through("2026-11-13")
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("last 全空 + lag=0 → 仍是 fresh（数据到位是事实）",
           got["status"] == "fresh", got["status"])
        ok("但 reason 必须说「新流程尚未运行过」",
           "新流程尚未运行过" in (got.get("reason") or ""),
           str(got.get("reason")))
        ok("并点明面板来自旧版全量重建（不是「今天已更新」）",
           "旧版全量重建" in (got.get("reason") or ""), str(got.get("reason")))
        ok("文案与 freshness.NO_CALC_REASON 同源（页面不硬编码）",
           got.get("reason") == freshness.NO_CALC_REASON, str(got.get("reason"))[:40])
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
def case_data_through(tmp: str) -> None:
    print("\n[2] dataThrough 必须用全表 MAX(date)，不是逐因子 MAX 的最小值")
    env = Env(tmp)
    try:
        env.set_panel_through("2026-11-13", lagging_factor="turn20",
                              lagging_through="2026-11-10")
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("dataThrough = 2026-11-13（mom20 的最晚日）",
           got["dataThrough"] == "2026-11-13", str(got["dataThrough"]))
        ok("不是 2026-11-10（若误用 panel_incr.data_through 就会是这个值）",
           got["dataThrough"] != "2026-11-10", str(got["dataThrough"]))
        ok("由此 lag=0 → fresh（不会被最落后因子拖成 stale）",
           got["status"] == "fresh" and got["lagTradingDays"] == 0,
           "%s / lag=%d" % (got["status"], got["lagTradingDays"]))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
def case_next_run(tmp: str) -> None:
    print("\n[3] nextRunAt 与调度器同源（同一个 sched_time._next_run）")
    env = Env(tmp)
    try:
        env.write_run(ok_value=True, finished="2026-11-16T16:12:00")
        got = env.compute(now=datetime(2026, 11, 16, 17, 0))
        want = sched_time._next_run(datetime(2026, 11, 16, 17, 0), FACTOR_TIME, True)
        ok("今天已成功 → nextRunAt 指向下个工作日",
           got["nextRunAt"] == want == "2026-11-17 16:10",
           "%s / %s" % (got["nextRunAt"], want))
        ok("格式是 YYYY-MM-DD HH:MM（空格分隔，不是 ISO 的 T）",
           len(got["nextRunAt"]) == 16 and "T" not in got["nextRunAt"],
           got["nextRunAt"])

        # 上一次运行是上周五 → 今天还没跑成，nextRunAt 应是今天 16:10
        env.write_run(ok_value=True, finished="2026-11-13T16:12:00")
        got = env.compute(now=datetime(2026, 11, 16, 9, 0))
        ok("今天还没跑 → nextRunAt 是今天 16:10", got["nextRunAt"] == "2026-11-16 16:10",
           got["nextRunAt"])

        got = env.compute(now=datetime(2026, 11, 20, 17, 0), today="2026-11-20")
        ok("周五收盘后 → 下个工作日是周一（跳过周末）",
           got["nextRunAt"] == "2026-11-23 16:10", got["nextRunAt"])
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
def case_enums(tmp: str) -> None:
    print("\n[4] 状态枚举与中文标签（共享知识第 3 条）")
    env = Env(tmp)
    try:
        env.write_run(ok_value=True, finished="2026-11-16T16:12:00")
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("只有 4 个状态值", sorted(freshness.STATUSES) ==
           ["failed", "fresh", "pending", "stale"], str(freshness.STATUSES))
        ok("4 态都有中文标签", all(k in freshness.STATUS_LABELS
                                for k in freshness.STATUSES), str(freshness.STATUS_LABELS))
        ok("返回值的 status 属于枚举", got["status"] in freshness.STATUSES, got["status"])
        ok("statusLabel 与状态对应", got["statusLabel"] == freshness.STATUS_LABELS[got["status"]],
           got["statusLabel"])
        ok("带 dataThroughWeekday（页面要写「（周五）」）",
           got["dataThroughWeekday"] == "周五",
           "%s / %s" % (got["dataThroughWeekday"], got["dataThrough"]))
        ok("cacheVerify 三段齐全（reconcile / ts / panel）",
           set(got["cacheVerify"].keys()) == {"reconcile", "ts", "panel"},
           str(sorted(got["cacheVerify"].keys())))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
def case_error_and_cache(tmp: str) -> None:
    print("\n[5] 错误信息截断 / TTL 缓存")
    env = Env(tmp)
    try:
        long_error = "x" * 500
        env.write_run(ok_value=False, error=long_error)
        got = env.compute(now=datetime(2026, 11, 16, 16, 30))
        ok("error 截断到 120 字（页面不会塞进一整段 traceback）",
           len(got["reason"] or "") == 120, "%d 字" % len(got["reason"] or ""))
        ok("factor_run.truncate_error 与判定用同一个上限", FR.ERROR_MAX_LEN == 120,
           str(FR.ERROR_MAX_LEN))

        env.write_run(ok_value=True, finished="2026-11-16T16:12:00")
        freshness.reset_cache()
        first = freshness.cached(env.conn, factor_time=FACTOR_TIME, ttl=60,
                                 today=TODAY, now=datetime(2026, 11, 16, 16, 30))
        # 直接改库（**不**调 set_panel_through：它会 reset_cache，那样就测不出 TTL）
        env.conn.execute("DELETE FROM factor_panel WHERE date > '2026-11-10'")
        env.conn.commit()
        second = freshness.cached(env.conn, factor_time=FACTOR_TIME, ttl=60,
                                  today=TODAY, now=datetime(2026, 11, 16, 16, 31))
        ok("TTL 内命中缓存（60 秒内不重复算，省掉 5 秒的 MAX(date) 查询）",
           first["status"] == second["status"] and second["status"] == "fresh",
           "%s → %s" % (first["status"], second["status"]))
        third = freshness.cached(env.conn, factor_time=FACTOR_TIME, ttl=0,
                                 today=TODAY, now=datetime(2026, 11, 16, 16, 32))
        ok("ttl=0 时立即重算（拿到新的过期状态）", third["status"] == "stale",
           third["status"])
    finally:
        env.cleanup()



# ────────────────────────────────────────────────────────────
def case_running_guard(tmp: str) -> None:
    print("\n[6] running 的 90 分钟守卫 + skipped（本次未开始）的 reason 优先级")
    env = Env(tmp)
    try:
        # --- running 未过期：仍是「今日重算进行中」 ---
        env.set_panel_through("2026-11-12")            # lag=1
        env.write_run(ok_value=True, finished="2026-11-16T16:10:12",
                      running={"runId": "r1", "startedAt": "2026-11-16T16:10:12"})
        got = env.compute(now=datetime(2026, 11, 16, 16, 20))
        ok("running 10 分钟前开始（未过期）→ pending「今日重算进行中」",
           got["status"] == "pending" and got["reason"] == "今日重算进行中",
           "%s / %s" % (got["status"], got["reason"]))

        # --- running 已过期（140 分钟）+ 数据没追平 ---
        env.write_run(ok_value=True, finished="2026-11-16T14:00:00",
                      running={"runId": "r2", "startedAt": "2026-11-16T14:00:00"})
        got = env.compute(now=datetime(2026, 11, 16, 16, 20))
        ok("running 过期 + lag>0 → **不是** fresh（过期≠数据新）",
           got["status"] != "fresh", got["status"])
        ok("过期且数据没追平 → reason 说「疑似中断」并给出数据停在哪",
           "疑似中断" in (got.get("reason") or "")
           and "2026-11-12" in (got.get("reason") or ""),
           str(got.get("reason")))

        # --- running 已过期 + 数据已追平 → fresh，但 reason 带中断提示 ---
        env.set_panel_through("2026-11-13")
        got = env.compute(now=datetime(2026, 11, 16, 16, 20))
        ok("running 过期 + lag=0 → fresh（数据确实到了）",
           got["status"] == "fresh", got["status"])
        ok("fresh 下 reason 允许非空，且写明「上次运行未正常结束」",
           "未正常结束" in (got.get("reason") or ""), str(got.get("reason")))

        # --- skipped：今天 + lag=2 → stale，reason 说「本次未开始：…」 ---
        env.set_panel_through("2026-11-11")            # lag=2
        env.write_run(ok_value=True, finished="2026-11-16T15:41:00",
                      skipped={"at": "2026-11-16T16:10:00",
                               "reason": "K 线未更新（sync_ok=false）"})
        got = env.compute(now=datetime(2026, 11, 16, 16, 20))
        ok("门禁拦住（skipped）→ state 仍按 lag 判成 stale，不压成 failed",
           got["status"] == "stale", got["status"])
        ok("reason 含「本次未开始」+ 具体原因（页面能说清为什么没跑）",
           "本次未开始" in (got.get("reason") or "")
           and "K 线未更新" in (got.get("reason") or ""),
           str(got.get("reason")))

        # --- skipped 早于 lastCalcAt → 之后已经成功算过，旧提示必须消失 ---
        env.write_run(ok_value=True, finished="2026-11-16T15:41:00",
                      skipped={"at": "2026-11-13T16:10:00",
                               "reason": "K 线未更新（sync_ok=false）"})
        got = env.compute(now=datetime(2026, 11, 16, 16, 20))
        ok("skipped 早于 lastCalcAt → 忽略（之后已成功算过，不能当墙纸）",
           "本次未开始" not in (got.get("reason") or ""), str(got.get("reason")))

        # --- skipped 是「昨天」，但 lastCalc 停在更早 → 仍然要显示原因 ---
        # 场景：11-16 16:10 门禁拦住（K 线未更新）；11-17 09:00 用户打开页面。
        # 按「是不是今天」过滤会把这条最有用的原因一起扔掉，页面只剩
        # 「已过期 N 个交易日」，用户只能自己猜「我关机了？没登录？」。
        env.write_run(ok_value=True, finished="2026-11-15T22:49:00",
                      skipped={"at": "2026-11-16T16:10:00",
                               "reason": "K 线未更新（sync_ok=false）"})
        got = env.compute(now=datetime(2026, 11, 17, 9, 0), today="2026-11-17")
        ok("跨夜的 skipped（lastCalc 在它之前）→ **仍显示**原因",
           "本次未开始" in (got.get("reason") or "")
           and "K 线未更新" in (got.get("reason") or ""),
           str(got.get("reason")))
        ok("跨夜的 skipped 带上日期（否则会被误读成「今天没跑」）",
           "2026-11-16" in (got.get("reason") or ""), str(got.get("reason")))
        ok("state 仍按 lag 判（跨夜也是 stale 而不是 failed）",
           got["status"] == "stale", "%s / lag=%d" % (got["status"],
                                                     got["lagTradingDays"]))

        # --- failed 压过 skipped ---
        env.write_run(ok_value=False, error="RuntimeError: staging 校验未通过",
                      finished="2026-11-16T16:00:00",
                      skipped={"at": "2026-11-16T16:10:00",
                               "reason": "K 线未更新（sync_ok=false）"})
        got = env.compute(now=datetime(2026, 11, 16, 16, 20))
        ok("failed 压过 skipped（算了但出错，比没开始更严重）",
           got["status"] == "failed" and "staging" in (got.get("reason") or ""),
           "%s / %s" % (got["status"], got.get("reason")))

        # --- B：begin() 清残留 running 并记 warning ---
        env.write_run(ok_value=True, finished="2026-11-16T14:00:00",
                      running={"runId": "dead", "startedAt": "2026-11-16T14:00:00"})
        FR.begin("fresh-run", ["2026-11-13"], "manual",
                 now="2026-11-16T16:20:00")
        after = FR.read()
        ok("begin() 清掉了过期的 running（状态文件本身要干净）",
           (after.get("running") or {}).get("runId") == "fresh-run",
           str((after.get("running") or {}).get("runId")))
        ok("begin() 在 warnings 里留下「清掉残留」的痕迹",
           any("残留" in str(w) for w in (after.get("warnings") or [])),
           str(after.get("warnings"))[:80])
        ok("begin() 保留了 staleRunning 现场（排障要看上一轮的 startedAt）",
           (after.get("staleRunning") or {}).get("runId") == "dead",
           str((after.get("staleRunning") or {}).get("runId")))

        # --- finish() 清 skipped ---
        FR.mark_skipped("K 线未更新（sync_ok=false）")
        ok("mark_skipped 写了 skipped 且没动 ok/finishedAt",
           (FR.read().get("skipped") or {}).get("reason", "").startswith("K 线未更新")
           and FR.read().get("ok") is True,
           str((FR.read().get("skipped") or {}).get("reason")))
        FR.finish(True, cost=1.0)
        ok("finish() 清掉 skipped（真跑过一次，旧提示必须消失）",
           FR.read().get("skipped") is None, str(FR.read().get("skipped")))
    finally:
        env.cleanup()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="freshness_test_")
    print("=" * 72)
    print("T03 / freshness 判定表验收（临时库 + 临时 factor_run.json）")
    print("=" * 72)
    before = prod_guard.snapshot()
    try:
        case_table(tmp)
        case_data_through(tmp)
        case_next_run(tmp)
        case_enums(tmp)
        case_error_and_cache(tmp)
        case_running_guard(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok("生产 data/ 的产物一个字节都没被写（隔离护栏）",
       not prod_guard.diff(before), str(prod_guard.diff(before)[:6]))

    print("\n========================================")
    print("RESULT: pass=%d fail=%d" % (PASS, FAIL))
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
    print("========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
