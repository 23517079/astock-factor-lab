#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_rolling_no_lookahead.py — 滚动验证的**前视偏差**验收测试

覆盖说明书 §6「约束 A 三套用例」与 §9 T03 验收：

  1. `test_cutoff_equals_T_minus_h`：`icCutoff == dates[idx(T)-h]`（3 个随机期）
  2. `test_future_corruption_invariance`（**主力**）：把 T 之后的收盘/开盘价整体
     ×1.37 后重建序列，断言该期因子集合与 `ic` 数值**逐位不变**
  3. `test_truncation_invariance`：数据截断到 `<= T` 重跑，集合与 ic 都相同
  4. 没有有效期时返回 `code=4`（共享知识第 6 条）
  5. `verify_lag()` 能抓住 `ts.last > panelMax - horizon`
  6. 选中集合非空；而**把 `groups` 抹成 NaN**（模拟「只存 ic」的错误实现）后
     选中集合立刻变成 0 个且不报错 —— 这是本方案最容易再犯的坑，必须有牙齿
  7. 滚动验证耗时（验收第 7 条，期望 ≤ 5 分钟）

为什么第 2 套是主力
------------------
「截断重跑」对少量行不敏感、因子集合可能照旧，抓不住 `<= T` 版本。
而「未来价格 ×1.37」会精确地只改动 `(T-h, T]` 区间的标签（分子跨过了 T、
分母没有），任何切到 `<= T` 的错误实现都必然读到这个变化。

运行：
    python tests/test_rolling_no_lookahead.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
TESTS = os.path.dirname(os.path.abspath(__file__))
for path in (HERE, TESTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import factor_ts as TS                          # noqa: E402
import rolling_eval as RE                       # noqa: E402
from factors import registry                    # noqa: E402
from synth_env import PARAMS, SynthEnv          # noqa: E402
import prod_guard                               # noqa: E402

PASS = 0
FAIL = 0
FAILS = []
PARAMS_FULL = dict(PARAMS, nwLag=4, rollStep=20, rollSelectWindow=120)


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  — %s" % detail) if detail else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  ✗ %s%s" % (name, ("  — %s" % detail) if detail else ""))


def build(env: SynthEnv, axis=None) -> dict:
    used = list(axis or env.dates)
    return TS.ensure(env.conn, env.fwd(axis=used), PARAMS,
                     panel_dates_axis=used, strict=False, verbose=False)


def period_signature(period: dict) -> dict:
    """`{factor: (icMean, tNw, verdict)}`，用于逐位比较两期是否相同。"""
    return {f["factor"]: (f["icMean"], f["tNw"], f["verdict"])
            for f in (period.get("factors") or [])}


# ────────────────────────────────────────────────────────────
# case 1：cutoff == dates[idx(T)-h]
# ────────────────────────────────────────────────────────────
def case_cutoff(tmp: str) -> None:
    print("\n[1] ic_cutoff：icCutoff == dates[idx(T)-horizon]（3 个随机期）")
    env = SynthEnv(tmp, "c1", n_dates=200)
    try:
        env.activate()
        build(env)
        rng = np.random.default_rng(11)
        bad = []
        for t_idx in rng.choice(np.arange(60, len(env.dates)), size=3, replace=False):
            period = RE.evaluate_period(env.dates, int(t_idx), PARAMS_FULL)
            want = env.dates[int(t_idx) - PARAMS["horizon"]]
            if period["icCutoff"] != want or period["asOf"] != env.dates[int(t_idx)]:
                bad.append((int(t_idx), period["icCutoff"], want))
        ok("3 个随机期的 icCutoff 都等于 dates[T-5]", not bad, str(bad))

        raised = False
        try:
            RE.ic_cutoff(env.dates, 3, 5)
        except ValueError:
            raised = True
        ok("t_idx < horizon 时抛 ValueError（不返回越界日期）", raised)

        last_period = RE.evaluate_period(env.dates, len(env.dates) - 1, PARAMS_FULL)
        ok("最新一期的 cut 正好是 panelMax-horizon",
           last_period["icCutoff"] == env.cutoff(),
           "%s vs %s" % (last_period["icCutoff"], env.cutoff()))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 2：未来价格被改 → 该期 ic 必须逐位不变（主力）
# ────────────────────────────────────────────────────────────
def case_future_corruption(tmp: str) -> None:
    print("\n[2] 主力：T 之后的价格被改，该期 ic 必须逐位不变")
    clean = SynthEnv(tmp, "c2clean", n_dates=200)
    uniform = SynthEnv(tmp, "c2uniform", n_dates=200)
    ranked = SynthEnv(tmp, "c2rank", n_dates=200)
    try:
        clean.activate()
        build(clean)
        t_idx = len(clean.dates) - 40                 # T 放中后段，后面还有价格可污染
        t_date = clean.dates[t_idx]

        # (a) 说明书原文的「整体 ×1.37」
        uniform.activate()
        uniform.corrupt_future(t_date, factor=1.37)
        build(uniform)

        # (b) 逐股乘数（会改变横截面排名）
        ranked.activate()
        ranked.corrupt_future(t_date, factor=1.0, per_symbol_sigma=0.35)
        build(ranked)

        clean.activate()
        base = RE.evaluate_period(clean.dates, t_idx, PARAMS_FULL)
        base_buggy = RE.judge_factor("mom60", t_date, PARAMS_FULL,
                                     PARAMS_FULL["rollSelectWindow"])
        ok("基准期有因子入选（后面比较才有意义）", base["n_selected"] > 0,
           "%d 个" % base["n_selected"])

        for tag, other in (("统一 ×1.37", uniform), ("逐股乘数", ranked)):
            other.activate()
            got = RE.evaluate_period(other.dates, t_idx, PARAMS_FULL)
            ok("[%s] asOf / icCutoff 一致" % tag,
               base["asOf"] == got["asOf"] and base["icCutoff"] == got["icCutoff"],
               "%s / %s" % (got["asOf"], got["icCutoff"]))
            ok("[%s] 选中集合逐项相同（含顺序）" % tag,
               base["selected"] == got["selected"],
               "%d vs %d 个" % (len(base["selected"]), len(got["selected"])))
            sig_a, sig_b = period_signature(base), period_signature(got)
            diff = [(n, sig_a[n], sig_b[n]) for n in sig_a if sig_a[n] != sig_b.get(n)]
            ok("[%s] 24 个因子的 (icMean, tNw, verdict) 逐位相同" % tag, not diff,
               "差异 %s" % (diff[:2] if diff else "无"))

        # ---- 对照 1：统一乘数对 rank IC 是**单调变换**，单独用没有牙齿 ----
        uniform.activate()
        uniform_buggy = RE.judge_factor("mom60", t_date, PARAMS_FULL,
                                        PARAMS_FULL["rollSelectWindow"])
        ok("对照：统一乘数下「误用 cut=T」也看不出差别（单调变换不改变排名）",
           base_buggy["tNw"] == uniform_buggy["tNw"],
           "tNw=%s（所以这套用例单独没有牙齿）" % base_buggy["tNw"])

        # ---- 对照 2：逐股乘数会改变排名，误用 cut=T 必须被抓到 ----
        ranked.activate()
        dirty_buggy = RE.judge_factor("mom60", t_date, PARAMS_FULL,
                                      PARAMS_FULL["rollSelectWindow"])
        ok("对照：逐股乘数下「误用 cut=T」的 tNw 与正确实现不同（用例有牙齿）",
           base_buggy["tNw"] != dirty_buggy["tNw"],
           "cut=T: clean=%s dirty=%s" % (base_buggy["tNw"], dirty_buggy["tNw"]))
    finally:
        clean.cleanup()
        uniform.cleanup()
        ranked.cleanup()


# ────────────────────────────────────────────────────────────
# case 3：截断到 <= T 不变性
# ────────────────────────────────────────────────────────────
def case_truncation(tmp: str) -> None:
    print("\n[3] 数据截断到 <= T 重跑：集合与 ic 都相同")
    full = SynthEnv(tmp, "c3full", n_dates=200)
    cut_env = SynthEnv(tmp, "c3cut", n_dates=200)
    try:
        full.activate()
        build(full)
        t_idx = len(full.dates) - 40
        t_date = full.dates[t_idx]

        cut_env.activate()
        cut_dates = [d for d in cut_env.dates if d <= t_date]
        cut_env.close = cut_env.close.loc[cut_env.close.index <= t_date]
        cut_env.open = cut_env.open.loc[cut_env.open.index <= t_date]
        cut_env._panel_cache.clear()
        build(cut_env, axis=cut_dates)

        cut_env.activate()
        b = RE.evaluate_period(cut_dates, len(cut_dates) - 1, PARAMS_FULL)
        full.activate()
        a = RE.evaluate_period(full.dates, t_idx, PARAMS_FULL)
        ok("截断后 icCutoff 相同", a["icCutoff"] == b["icCutoff"],
           "%s vs %s" % (a["icCutoff"], b["icCutoff"]))
        ok("截断后选中集合相同", a["selected"] == b["selected"],
           "%d vs %d" % (len(a["selected"]), len(b["selected"])))
        sig_a, sig_b = period_signature(a), period_signature(b)
        diff = [name for name in sig_a if sig_a[name] != sig_b.get(name)]
        ok("截断后逐因子 ic 数值相同（不只是集合）", not diff, "差异 %s" % diff[:3])
    finally:
        full.cleanup()
        cut_env.cleanup()


# ────────────────────────────────────────────────────────────
# case 4：没有有效期 → code 4
# ────────────────────────────────────────────────────────────
def case_no_period(tmp: str) -> None:
    print("\n[4] 面板天数不足 → code=4（滚动验证没有有效期）")
    env = SynthEnv(tmp, "c4", n_dates=20)
    try:
        env.activate()
        res = RE.run(env.conn, PARAMS_FULL, panel_dates_axis=env.dates, out=None,
                     verbose=False)
        ok("返回 ok=False 且 code=4", res["ok"] is False and res["code"] == 4,
           "code=%s" % res["code"])
        ok("reason 说明原因（不是空白失败）", bool(res.get("reason")),
           str(res.get("reason"))[:60])
        ok("periods 为空列表（不是 None，页面可直接遍历）",
           res["periods"] == [], str(res["periods"]))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 5：数据层探针抓越界
# ────────────────────────────────────────────────────────────
def case_lag_probe(tmp: str) -> None:
    print("\n[5] verify_lag：ts.last 越过 panelMax-horizon 必须被抓到")
    env = SynthEnv(tmp, "c5", n_dates=180)
    try:
        env.activate()
        build(env)
        ok("正常状态下探针通过", RE.verify_lag(env.dates, PARAMS["horizon"])["ok"])

        victim = "obv_slope20"
        env.set_last(victim, env.dates[-1], 0.02)
        raised = None
        try:
            RE.verify_lag(env.dates, PARAMS["horizon"], strict=True)
        except RuntimeError as exc:
            raised = exc
        ok("strict=True 时抛 RuntimeError", raised is not None,
           str(raised)[:70] if raised else "")
        soft = RE.verify_lag(env.dates, PARAMS["horizon"], strict=False)
        ok("strict=False 时列出越界因子", soft["ok"] is False
           and [v["factor"] for v in soft["violations"]] == [victim],
           str([v["factor"] for v in soft["violations"]]))
        lasts = RE.verify_lag(env.dates, PARAMS["horizon"], strict=False)["lasts"]
        others = {k: v for k, v in lasts.items() if k != victim}
        ok("其余 23 个因子的末端正好是 cutoff（滞后 == horizon 判为正常，不报错）",
           len(others) == 23 and all(v == env.cutoff() for v in others.values()),
           "%d 个因子，cutoff=%s" % (len(others), env.cutoff()))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 6：groups 必须落盘（选中集合的牙齿）
# ────────────────────────────────────────────────────────────
def case_groups_required(tmp: str) -> None:
    print("\n[6] 只存 ic（groups 全 NaN）→ 选中集合恒为 0 且不报错")
    env = SynthEnv(tmp, "c6", n_dates=200)
    try:
        env.activate()
        build(env)
        t_idx = len(env.dates) - 1
        good = RE.evaluate_period(env.dates, t_idx, PARAMS_FULL)
        ok("正常实现下本期有因子入选（信号是真的）", good["n_selected"] > 0,
           "%d / %d" % (good["n_selected"], good["n_factors"]))
        mono_ok = [f for f in good["factors"] if f["mono"] is not None]
        ok("mono 不是 None（groups 落盘有效）", len(mono_ok) > 0,
           "%d 个因子有单调性" % len(mono_ok))

        for name in registry.all_names():
            ser = TS._load_series(name)
            if ser is None:
                continue
            blank = np.full_like(ser["groups"], np.nan)
            TS._save_series(name, ser["dates"], ser["ic"], blank)
        bad = RE.evaluate_period(env.dates, t_idx, PARAMS_FULL)
        ok("抹掉 groups 后选中集合变成 0 个（且不抛异常）",
           bad["n_selected"] == 0, "%d 个" % bad["n_selected"])
        ok("所有因子退化到「仅统计显著 / 未达门槛」这一档",
           all(f["mono"] is None for f in bad["factors"])
           and all(f["verdict"] not in RE.SELECTED_VERDICTS for f in bad["factors"]),
           str(sorted({f["verdict"] for f in bad["factors"]})))
        ok("t_nw 仍然算得出来（证明问题只在 groups，不在 IC）",
           sum(1 for f in bad["factors"] if f["tNw"] is not None) > 0,
           "%d 个有 tNw" % sum(1 for f in bad["factors"] if f["tNw"] is not None))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 7：整轮耗时（验收第 7 条）
# ────────────────────────────────────────────────────────────
def case_timing(tmp: str) -> None:
    print("\n[7] 整轮滚动验证耗时（验收：≤ 5 分钟，实测预期 ~70s 量级）")
    env = SynthEnv(tmp, "c7", n_dates=260)
    try:
        env.activate()
        build(env)
        t0 = time.time()
        res = RE.run(env.conn, PARAMS_FULL, panel_dates_axis=env.dates, out=None,
                     verbose=False)
        elapsed = time.time() - t0
        ok("跑完且 ok=True", bool(res["ok"]), "%d 期" % res["n_periods"])
        ok("耗时 <= 300s", elapsed <= 300.0, "%.2fs（合成 260 天 × 24 因子）" % elapsed)
        ok("每期都带 icCutoff / n_selected / factors",
           all(p.get("icCutoff") and p.get("n_selected") is not None
               and p.get("factors") for p in res["periods"]),
           "%d 期" % len(res["periods"]))
        ok("最新一期就是最后一期（不会因步长丢掉最新）",
           res["periods"][-1]["asOf"] == env.dates[-1],
           res["periods"][-1]["asOf"])
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 8：因子数只数「可评估」的；IC 必须按方向归正
# ────────────────────────────────────────────────────────────
def case_counts_and_adj(tmp: str) -> None:
    print("\n[8] n_factors 只数可评估的因子；avgIcSelectedAdj 必须按方向归正")
    env = SynthEnv(tmp, "c8", n_dates=180)
    try:
        env.activate()
        build(env)
        # t_idx 要选得够晚：合成环境的序列起点在 dates[~24]（预热 141 根 K 线），
        # t_idx=40 时 cut 之前只有 16 天 IC，全部因子都会判「数据不足」，测不出差别。
        t_idx = 100
        cut = RE.ic_cutoff(env.dates, t_idx, PARAMS_FULL["horizon"])

        # 造一个「预热期长」的因子：把序列起点挪到 cut 前 10 天
        # （生产里 mom120 就是这样：序列起点 2021-08-02，而 T=2021-03-05 的 cut 在它之前）
        victim = "mom120"
        ser = TS._load_series(victim)
        kept = [i for i, d in enumerate(ser["dates"].tolist()) if str(d) <= cut]
        start = kept[-10]                        # 只留 cut 前的 10 天
        TS._save_series(victim, ser["dates"][start:], ser["ic"][start:],
                        ser["groups"][start:])

        p = RE.evaluate_period(env.dates, t_idx, PARAMS_FULL)
        row = [f for f in p["factors"] if f["factor"] == victim][0]
        ok("窗口内只有 10 天 → 该因子判「数据不足」",
           row["verdict"] == "数据不足" and row["tNw"] is None,
           "%s / n=%d / %s" % (row["verdict"], row["n"], row["note"]))
        ok("n_considered = 进 rows 的个数（24）", p["n_considered"] == 24,
           str(p["n_considered"]))
        ok("n_evaluable 手算 = 24 - 1 = 23（「数据不足」不该算有效因子）",
           p["n_evaluable"] == 23, str(p["n_evaluable"]))
        ok("n_factors 保留字段名但语义收紧成 n_evaluable（不再是 len(rows)）",
           p["n_factors"] == p["n_evaluable"] == 23, str(p["n_factors"]))
        ok("n_evaluable < n_considered（旧实现会把 24 报出去，正是这次修的 bug）",
           p["n_evaluable"] < p["n_considered"],
           "%d < %d" % (p["n_evaluable"], p["n_considered"]))

        # 方向归正：只在入选集合上取 mean(icMean × direction)
        sel = [f for f in p["factors"] if f["selected"]]
        want = sum(f["icMean"] * f["direction"] for f in sel) / len(sel)
        ok("avgIcSelectedAdj == mean(icMean × direction)（入选集合上，逐位相等）",
           abs(p["avgIcSelectedAdj"] - want) < 1e-12,
           "%+.6f vs %+.6f（%d 个入选）" % (p["avgIcSelectedAdj"], want, len(sel)))
        ok("avgIcSelected（raw）仍然单独保留（诊断用，两者不能只留一个）",
           p["avgIcSelected"] is not None and p["avgIcSelectedAdj"] is not None,
           "raw=%+.6f adj=%+.6f" % (p["avgIcSelected"], p["avgIcSelectedAdj"]))

        # 反向因子这条路径必须确定性地测到（不依赖「本次恰好选上了谁」）
        neg_name = [n for n in registry.all_names()
                    if int(registry.get(n)["direction"]) < 0][0]
        neg_row = RE.judge_factor(neg_name, cut, PARAMS_FULL, 120)
        ok("direction=-1 的因子：icAdj = -icMean（原始 IC 为负正是先验成立）",
           neg_row["icMean"] is not None
           and abs(neg_row["icAdj"] + neg_row["icMean"]) < 1e-12,
           "%s dir=%d icMean=%+.4f icAdj=%+.4f"
           % (neg_name, neg_row["direction"], neg_row["icMean"], neg_row["icAdj"]))

        # 整轮汇总也要有 Adj 版本（页面读它）
        res = RE.run(env.conn, PARAMS_FULL, panel_dates_axis=env.dates, out=None,
                     verbose=False)
        s = res["summary"]
        want_all = [q["avgIcSelectedAdj"] for q in res["periods"]
                    if q.get("avgIcSelectedAdj") is not None]
        ok("summary 增了 avgSelectedIcAdj（= 各期 Adj 的均值）",
           s.get("avgSelectedIcAdj") is not None
           and abs(s["avgSelectedIcAdj"] - sum(want_all) / len(want_all)) < 1e-12,
           "raw=%s adj=%s" % (s.get("avgSelectedIc"),
                              None if s.get("avgSelectedIcAdj") is None
                              else "%+.4f" % s["avgSelectedIcAdj"]))
        ok("summary.avgSelectedIc 原值保留（不再被页面当有效 IC 用）",
           s.get("avgSelectedIc") is not None, "%+.4f" % s["avgSelectedIc"])
    finally:
        env.cleanup()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="rolling_test_")
    t0 = time.time()
    print("=" * 72)
    print("T03 / rolling_eval 前视偏差验收（约束 A 三套用例 + 耗时）")
    print("=" * 72)
    before = prod_guard.snapshot()
    try:
        case_cutoff(tmp)
        case_future_corruption(tmp)
        case_truncation(tmp)
        case_no_period(tmp)
        case_lag_probe(tmp)
        case_groups_required(tmp)
        case_counts_and_adj(tmp)
        case_timing(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    ok("生产 data/ 的产物一个字节都没被写（隔离护栏）",
       not prod_guard.diff(before), str(prod_guard.diff(before)[:6]))

    print("\n========================================")
    print("RESULT: pass=%d fail=%d  （%.1fs）" % (PASS, FAIL, time.time() - t0))
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
    print("========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
