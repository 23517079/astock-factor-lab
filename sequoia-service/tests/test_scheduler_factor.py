#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_scheduler_factor.py — T04 调度器接入因子重算的验收测试

覆盖 5 条验收（全部离线，**绝不真跑 pipeline、不碰 panel_cache / factor_ts**）：

  1. 连续两次 `_tick()`（模拟 30 秒后）只起 1 次子进程、只写 1 条「开始因子重算」
  2. 「重启」（新建 Scheduler 实例、从磁盘重读状态）后同日不重跑
  3. `last_sync_ok=false` 时 16:10 不执行，`last_factor_error` 含「K 线未更新」，
     且 **必须写 `last_factor_attempt_date`**（否则每 30 秒重试刷满日志）
  4. 子进程超时 → `last_factor_ok=false` 且 `last_factor_error` 含「超时」
  5. `scheduler_state.json` 新增键命名与 sync/scan 一致（统一 `last_factor_` 前缀），
     `status()` 透出 `factor_time / next_factor / last_factor_*`
  6. 失败退避：跑失败后 10 分钟内不重试，且退避状态在 `scheduler.log` 里
     **只留一行**（「静默的等待」和「静默的卡死」在日志上长得一样，必须能区分）

怎么做到「不真跑」：把 `scheduler.subprocess.run` 换成一个只记账的假函数。
所以这些用例跑完不会产生任何面板/缓存的写操作，可以随时跑。

时间也必须可控：`_tick()` 内部调 `scheduler._now()`，这里把它替换成固定时刻，
否则用例只在「恰好 16:10 之后」才通过——那种测试等于没测。

运行：
    python tests/test_scheduler_factor.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
from datetime import datetime

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import prod_guard                               # noqa: E402
import sched_time                               # noqa: E402
import scheduler as S                           # noqa: E402

PASS = 0
FAIL = 0
FAILS = []

TODAY = "2026-11-16"                            # 周一，工作日
AT_1610 = datetime(2026, 11, 16, 16, 10, 5)
AT_1620 = datetime(2026, 11, 16, 16, 20, 0)


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s%s" % (name, ("  " + extra) if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  [FAIL] %s%s" % (name, ("  " + extra) if extra else ""))


class FakeProc:
    """`subprocess.run` 的返回值替身。"""

    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


class Harness:
    """一个测试用的调度器：状态/日志/配置全指到临时目录，时间固定。"""

    def __init__(self, tmp: str, now: datetime = AT_1610,
                 state: dict | None = None, factor_time: str = "16:10"):
        self.dir = os.path.join(tmp, "sched_%d" % id(self))
        os.makedirs(self.dir, exist_ok=True)
        # 三个模块级路径都要改：STATE_PATH / LOG_PATH / DATA_DIR
        # 漏改任何一个，测试就会往生产 data/ 里写状态或日志（prod_guard 会抓，
        # 但更该在这里就不给它机会）。
        self._saved = (S.STATE_PATH, S.LOG_PATH, S.DATA_DIR, S._now)
        self._saved_run_env = os.environ.get("FACTOR_RUN_PATH")
        S.STATE_PATH = os.path.join(self.dir, "scheduler_state.json")
        S.LOG_PATH = os.path.join(self.dir, "scheduler.log")
        S.DATA_DIR = self.dir
        # 门禁拦住时会调 `factor_run.mark_skipped()` 写 factor_run.json。
        # 不重定向就会写进生产 `data/` —— prod_guard 会抓，但更该在这里就不给机会。
        self.run_path = os.path.join(self.dir, "factor_run.json")
        os.environ["FACTOR_RUN_PATH"] = self.run_path
        S._now = lambda: now
        if state is not None:
            with open(S.STATE_PATH, "w", encoding="utf-8") as fh:
                json.dump(state, fh)
        self.cfg_path = os.path.join(self.dir, "scheduler_config.json")
        self.now = now
        self.sched = S.Scheduler(cfg_path=self.cfg_path)
        self.sched.cfg["factor_time"] = factor_time
        self.calls = []
        self.busy_during_call = []
        self.procs = {}
        self._saved_run = S.subprocess.run

    # ── 假子进程 ──
    def patch_run(self, action="ok", returncode=0):
        """替换 `subprocess.run`。

        Args:
            action: `"ok"` 正常返回；`"timeout"` 抛 `TimeoutExpired`。
            returncode: 正常返回时的退出码。
        """
        sched = self.sched

        def fake(cmd, **kwargs):
            self.calls.append(list(cmd))
            # 关键观测点：子进程正在跑的时候，busy 必须是 True —— 这就是
            # 「不会并发起第二个」的实现依据，比事后数进程数可靠。
            self.busy_during_call.append(bool(sched.busy.get("factor")))
            if action == "timeout":
                raise S.subprocess.TimeoutExpired(cmd, kwargs.get("timeout") or 3600)
            proc = FakeProc(returncode=returncode, stdout="[ok] 假运行\n",
                            stderr="" if returncode == 0 else "假失败：缓存因子不齐\n")
            self.procs[returncode] = proc
            return proc

        S.subprocess.run = fake

    def set_time(self, now: datetime):
        self.now = now
        S._now = lambda: now
        self.sched.state = S._load_state()

    def log_text(self) -> str:
        try:
            with open(S.LOG_PATH, encoding="utf-8") as fh:
                return fh.read()
        except OSError:
            return ""

    def log_count(self, needle: str) -> int:
        return sum(1 for line in self.log_text().splitlines() if needle in line)

    def state(self) -> dict:
        return S._load_state()

    def restart(self) -> None:
        """模拟「服务重启」：状态从磁盘重读，进程内状态（busy）全部重来。"""
        self.sched = S.Scheduler(cfg_path=self.cfg_path)
        self.sched.cfg["factor_time"] = "16:10"

    def cleanup(self) -> None:
        S.subprocess.run = self._saved_run
        S.STATE_PATH, S.LOG_PATH, S.DATA_DIR, S._now = self._saved
        if self._saved_run_env is None:
            os.environ.pop("FACTOR_RUN_PATH", None)
        else:
            os.environ["FACTOR_RUN_PATH"] = self._saved_run_env


def _sync_ready(date: str = TODAY, ok_value: bool = True) -> dict:
    return {"last_sync_date": date, "last_sync_ok": ok_value,
            "last_sync_at": "%sT15:41:00" % date}


# ────────────────────────────────────────────────────────────
# 1. 连续两次 tick 只跑一次，且运行期间 busy=true
# ────────────────────────────────────────────────────────────
def case_single_run(tmp: str) -> None:
    print("\n[1] 连续两次 _tick()（间隔 30 秒）只起 1 次子进程，且不并发")
    h = Harness(tmp, AT_1610, state=_sync_ready())
    try:
        h.patch_run("ok", returncode=0)
        h.sched._tick()
        h.set_time(AT_1620)
        h.sched._tick()

        ok("只起过 1 次子进程", len(h.calls) == 1, "调用 %d 次" % len(h.calls))
        ok("只写了 1 条「开始因子重算」日志",
           h.log_count("开始因子重算") == 1,
           "匹配 %d 条" % h.log_count("开始因子重算"))
        ok("子进程运行期间 busy['factor'] 为 True（并发不会起第二个）",
           h.busy_during_call == [True], str(h.busy_during_call))
        ok("跑完后 busy 复位", h.sched.busy["factor"] is False)
        st = h.state()
        ok("成功后写 last_factor_date / last_factor_ok / last_factor_cost",
           st.get("last_factor_date") == TODAY and st.get("last_factor_ok") is True
           and isinstance(st.get("last_factor_cost"), int),
           "%s / %s / %s" % (st.get("last_factor_date"), st.get("last_factor_ok"),
                             st.get("last_factor_cost")))
        ok("尝试级字段也落盘（last_factor_attempt_date/at）",
           st.get("last_factor_attempt_date") == TODAY
           and bool(st.get("last_factor_attempt_at")),
           "%s %s" % (st.get("last_factor_attempt_date"),
                      st.get("last_factor_attempt_at")))
        ok("子进程参数是 run_factor_pipeline.py --trigger scheduler",
           "run_factor_pipeline.py" in " ".join(h.calls[0])
           and "--trigger" in h.calls[0]
           and h.calls[0][h.calls[0].index("--trigger") + 1] == "scheduler",
           " ".join(h.calls[0][1:]))
    finally:
        h.cleanup()


# ────────────────────────────────────────────────────────────
# 2. 「重启」后同日不重跑
# ────────────────────────────────────────────────────────────
def case_restart_no_rerun(tmp: str) -> None:
    print("\n[2] 重启（新建 Scheduler 实例）后同日不重跑")
    h = Harness(tmp, AT_1610, state=_sync_ready())
    try:
        h.patch_run("ok", returncode=0)
        h.sched._tick()
        before = len(h.calls)
        h.restart()                       # 进程内 busy 归零、状态从磁盘重读
        h.set_time(AT_1620)
        h.sched._tick()
        ok("重启后同日不再尝试", len(h.calls) == before, "调用 %d → %d"
           % (before, len(h.calls)))
        ok("状态里仍是今天已成功", h.state().get("last_factor_date") == TODAY
           and h.state().get("last_factor_ok") is True,
           "%s / %s" % (h.state().get("last_factor_date"),
                        h.state().get("last_factor_ok")))
        ok("重启后 busy['factor'] 复位为 False", h.sched.busy["factor"] is False)
    finally:
        h.cleanup()


# ────────────────────────────────────────────────────────────
# 3. K 线门禁：sync_ok=false 时 16:10 不执行，且只记一次
# ────────────────────────────────────────────────────────────
def case_sync_gate(tmp: str) -> None:
    print("\n[3] last_sync_ok=false → 不执行，error 含「K 线未更新」，且只记一次")
    h = Harness(tmp, AT_1610, state=_sync_ready(ok_value=False))
    try:
        # 预置一份「上一次真实计算」的账本：门禁拦住时**不许**改它
        with open(h.run_path, "w", encoding="utf-8") as fh:
            json.dump({"runId": "20261116T154000", "startedAt": "2026-11-16T15:40:00",
                       "finishedAt": "2026-11-16T15:41:00", "ok": True, "cost": 60.0,
                       "error": None, "skipped": None, "running": None}, fh)
        h.patch_run("ok", returncode=0)
        h.sched._tick()
        ok("一个子进程都没起", not h.calls, "调用 %d 次" % len(h.calls))
        st = h.state()
        ok("记成失败：last_factor_ok=false",
           st.get("last_factor_ok") is False, str(st.get("last_factor_ok")))
        ok("error 含「K 线未更新」",
           "K 线未更新" in str(st.get("last_factor_error") or ""),
           str(st.get("last_factor_error")))
        ok("**必须**写了 last_factor_attempt_date（否则每 30 秒重试刷日志）",
           st.get("last_factor_attempt_date") == TODAY,
           str(st.get("last_factor_attempt_date")))

        # 30 秒后再 tick 一次：同一个 sync 状态，必须静默，不再记第二条
        n_log = h.log_count("因子重算未执行")
        h.set_time(AT_1620)
        h.sched._tick()
        ok("同一个 sync 状态下第二次 tick 不重复记日志",
           h.log_count("因子重算未执行") == n_log,
           "%d → %d 条" % (n_log, h.log_count("因子重算未执行")))
        ok("第二次也没有起子进程", not h.calls)

        # --- 门禁拦住要不要告诉页面：skipped 字段，但不许动 ok/finishedAt ---
        with open(h.run_path, encoding="utf-8") as fh:
            ledger = json.load(fh)
        ok("门禁拦住时往 factor_run.json 写了 skipped（页面才说得出原因）",
           (ledger.get("skipped") or {}).get("reason") == st.get("last_factor_error"),
           str(ledger.get("skipped")))
        ok("skipped.at 是今天",
           str((ledger.get("skipped") or {}).get("at") or "")[:10] == TODAY,
           str((ledger.get("skipped") or {}).get("at")))
        ok("**没有**篡改上一次真实计算：ok / finishedAt 原样",
           ledger.get("ok") is True
           and ledger.get("finishedAt") == "2026-11-16T15:41:00",
           "%s / %s" % (ledger.get("ok"), ledger.get("finishedAt")))

        # sync 迟到成功 → 状态快照变了，应该放行（同日补跑）
        st = h.state()
        st["last_sync_ok"] = True
        st["last_sync_date"] = TODAY
        with open(S.STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        h.sched.state = S._load_state()
        h.set_time(datetime(2026, 11, 16, 16, 21, 0))
        h.sched._tick()
        ok("sync 迟到成功后允许同日补跑（起 1 次子进程）",
           len(h.calls) == 1, "调用 %d 次" % len(h.calls))
    finally:
        h.cleanup()


# ────────────────────────────────────────────────────────────
# 4. 超时
# ────────────────────────────────────────────────────────────
def case_timeout(tmp: str) -> None:
    print("\n[4] 子进程超时 → last_factor_ok=false 且 error 含「超时」")
    h = Harness(tmp, AT_1610, state=_sync_ready())
    try:
        h.patch_run("timeout")
        res = h.sched.run_factor(trigger="scheduler")
        st = h.state()
        ok("run_factor 返回 ok=false", res.get("ok") is False, str(res.get("ok")))
        ok("last_factor_ok=false", st.get("last_factor_ok") is False)
        ok("error 含「超时」",
           "超时" in str(st.get("last_factor_error") or ""),
           str(st.get("last_factor_error")))
        ok("超时也写了 last_factor_cost（便于看出卡了多久）",
           isinstance(st.get("last_factor_cost"), int), str(st.get("last_factor_cost")))
        ok("超时后 busy 复位（否则今天再也不会重试）",
           h.sched.busy["factor"] is False)
    finally:
        h.cleanup()


# ────────────────────────────────────────────────────────────
# 5. 状态键命名 + status() 透出 + next_factor 同源
# ────────────────────────────────────────────────────────────
def case_state_keys(tmp: str) -> None:
    print("\n[5] scheduler_state.json 键命名与 sync/scan 一致，status() 全量透出")
    h = Harness(tmp, AT_1610, state=_sync_ready())
    try:
        h.patch_run("ok", returncode=0)
        h.sched._tick()
        st = h.state()
        factor_keys = sorted(k for k in st if "factor" in k)
        ok("所有因子相关键都是 last_factor_ 前缀",
           all(k.startswith("last_factor_") for k in factor_keys),
           str(factor_keys))
        need = ("last_factor_date", "last_factor_at", "last_factor_ok",
                "last_factor_cost", "last_factor_error",
                "last_factor_attempt_date")
        ok("必备键齐全（与 last_sync_* / last_scan_* 命名对齐）",
           all(k in st for k in need),
           "缺 %s" % [k for k in need if k not in st])
        ok("没有 factor_ok 这类没前缀的键（会与页面契约不一致）",
           not any(k in st for k in ("factor_ok", "factor_date", "factor_at")))

        got = h.sched.status()
        for key in ("factor_time", "next_factor", "last_factor_date",
                    "last_factor_at", "last_factor_ok", "last_factor_cost",
                    "last_factor_error", "last_factor_attempt_date"):
            ok("status() 透出 %s" % key, key in got, str(got.get(key)))
        ok("busy 里有 factor 这一路", "factor" in (got.get("busy") or {}),
           str(got.get("busy")))
        ok("factor_time 取自配置", got.get("factor_time") == "16:10",
           str(got.get("factor_time")))
        # next_factor 必须与 sched_time 同源：今天已成功 → 下一次是下个工作日
        want = sched_time._next_run(AT_1610, "16:10", True)
        ok("next_factor 与 sched_time._next_run 同源（成功后指向下个工作日）",
           got.get("next_factor") == want, "%s vs %s" % (got.get("next_factor"), want))
    finally:
        h.cleanup()


# ────────────────────────────────────────────────────────────
# 6. 失败退避：同一个 sync 状态下失败后不许每 30 秒重试
# ────────────────────────────────────────────────────────────
def case_fail_backoff(tmp: str) -> None:
    print("\n[6] 跑失败后不每 30 秒重试：10 分钟内静默，之后才允许再试")
    h = Harness(tmp, AT_1610, state=_sync_ready())
    try:
        h.patch_run("ok", returncode=2)       # 模拟 exit 2（校验失败）
        h.sched._tick()
        ok("第一次尝试照常起子进程（失败也要真跑一遍才知道）",
           len(h.calls) == 1, "调用 %d 次" % len(h.calls))
        st = h.state()
        ok("失败被记下来（ok=false，error 来自子进程 stderr）",
           st.get("last_factor_ok") is False
           and "缓存因子不齐" in str(st.get("last_factor_error") or ""),
           "%s / %s" % (st.get("last_factor_ok"), st.get("last_factor_error")))
        first_calls = len(h.calls)

        # 30 秒后：不能重试（否则一次 exit 2 会演变成「每 30 秒重读 54.7 秒价格宽表」）
        h.set_time(datetime(2026, 11, 16, 16, 10, 35))
        h.sched._tick()
        ok("30 秒后不重试", len(h.calls) == first_calls, "调用 %d 次" % len(h.calls))

        # 退避必须留痕：静默的等待和静默的卡死在日志上长得一样
        ok("退避在 scheduler.log 里留了一行",
           h.log_count("退避至") == 1, "匹配 %d 条" % h.log_count("退避至"))
        ok("日志写明退避到几点（运维不用自己算 600 秒）",
           "退避至 16:20" in h.log_text(), h.log_text()[-160:].strip())

        # 5 分钟后：仍在退避窗口内
        h.set_time(datetime(2026, 11, 16, 16, 15, 0))
        h.sched._tick()
        ok("5 分钟后仍在退避窗口内", len(h.calls) == first_calls,
           "调用 %d 次" % len(h.calls))
        ok("同一个退避状态不重复记日志（30 秒一 tick 不能刷屏）",
           h.log_count("退避至") == 1, "匹配 %d 条" % h.log_count("退避至"))

        # 11 分钟后：允许再试一次
        h.set_time(datetime(2026, 11, 16, 16, 21, 5))
        h.sched._tick()
        ok("过了退避窗口（11 分钟）才允许重试", len(h.calls) == first_calls + 1,
           "调用 %d → %d" % (first_calls, len(h.calls)))

        # 退避只看「sync 状态有没有变」：把上次尝试记的 sig 改掉，
        # 等价于「这期间 sync 又跑了一轮」，此时即便是 30 秒内也该放行。
        st = h.state()
        st["last_factor_sig"] = "stale|2026-11-15"     # 与当前 sync 状态不再相同
        st["last_factor_attempt_at"] = "2026-11-16T16:21:05"
        with open(S.STATE_PATH, "w", encoding="utf-8") as fh:
            json.dump(st, fh)
        h.sched.state = S._load_state()
        n = len(h.calls)
        h.set_time(datetime(2026, 11, 16, 16, 22, 0))   # 距上次仅 55 秒
        h.sched._tick()
        ok("sync 状态变了就不受退避约束（sig 不等 → 立刻放行）",
           len(h.calls) == n + 1, "调用 %d → %d" % (n, len(h.calls)))
    finally:
        h.cleanup()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sched_factor_test_")
    print("=" * 74)
    print("T04 / scheduler 因子重算接入（离线：假子进程，不碰面板与缓存）")
    print("=" * 74)
    before = prod_guard.snapshot()
    try:
        case_single_run(tmp)
        case_restart_no_rerun(tmp)
        case_sync_gate(tmp)
        case_timeout(tmp)
        case_state_keys(tmp)
        case_fail_backoff(tmp)
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
