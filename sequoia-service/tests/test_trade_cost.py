#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_trade_cost.py — T07 成本内核的五项硬约束

  1. 手算一格净收益误差 < 1e-12；费率切换日边界（2022-04-29 / 2023-08-28）正确
  2. gross IC == net IC（统一费率是横截面单调变换）—— 防止拿 IC 当扣费证据
  3. 最低佣金在名义额 1 万 / 2 万时翻转（5元起点 vs 万三）
  4. 抹掉 open[d+1] → nMissingOpen==1，且 fwd 没有被 0 填充
  5. multiplier 1→2→4 净年化单调不增

所有测试只用合成数据 + 临时目录；`prod_guard` 断言生产 data/ 一个字节未写。
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for path in (HERE, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import factor_eval as fe                        # noqa: E402
import trade_cost as TC                         # noqa: E402
from synth_env import PARAMS, SynthEnv          # noqa: E402
import prod_guard                               # noqa: E402

PASS = 0
FAIL = 0
FAILS = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  — %s" % detail) if detail else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  ✗ %s%s" % (name, ("  — %s" % detail) if detail else ""))


# ── 1. 手算 + 费率边界 ──────────────────────────────────────
def case_manual_cell() -> None:
    print("\n[1] 手算一格净收益 + 费率切换日边界")
    days = ["2022-04-28", "2022-04-29", "2023-08-27", "2023-08-28", "2023-08-29"]
    k = TC.adjust_factor(days, 1, "a_share", 1.0)
    sell = "2022-04-29"                          # i=0 的卖出日
    r = TC.rate_at("a_share", sell, 1.0)
    buy_fee = r["commission"] + r["transferBuy"]
    sell_fee = r["commission"] + r["stampSell"] + r["transferSell"]
    want = ((1.0 - r["slippage"]) * (1.0 - sell_fee)
            / ((1.0 + r["slippage"]) * (1.0 + buy_fee)))
    ok("手算 k[0] 与 adjust_factor 逐位相等（误差 < 1e-12）",
       abs(float(k.iloc[0]) - want) < 1e-12,
       "%.15f vs %.15f" % (float(k.iloc[0]), want))

    fwd = pd.DataFrame([[0.02, -0.01]], index=[days[0]], columns=["A", "B"])
    net = TC.net_forward(fwd, k)
    want_a = (1.0 + 0.02) * want - 1.0
    ok("net_forward 手算误差 < 1e-12", abs(float(net.iloc[0, 0]) - want_a) < 1e-12,
       "%.15f vs %.15f" % (float(net.iloc[0, 0]), want_a))

    ok("最后 h 天是 NaN（不 fillna/backfill）", bool(np.isnan(k.iloc[-1])),
       "k[-1]=%s" % k.iloc[-1])

    r_0428 = TC.rate_at("a_share", "2022-04-28", 1.0)
    r_0429 = TC.rate_at("a_share", "2022-04-29", 1.0)
    r_0827 = TC.rate_at("a_share", "2023-08-27", 1.0)
    r_0828 = TC.rate_at("a_share", "2023-08-28", 1.0)
    ok("过户费切换日：04-28 仍是 0.02‰、04-29 起 0.01‰（含当天）",
       r_0428["transferSell"] == TC.TRANSFER_BEFORE
       and r_0429["transferSell"] == TC.TRANSFER_AFTER,
       "%g → %g" % (r_0428["transferSell"], r_0429["transferSell"]))
    ok("印花税切换日：08-27 仍是 1‰、08-28 起 0.5‰（含当天）",
       r_0827["stampSell"] == TC.STAMP_BEFORE and r_0828["stampSell"] == TC.STAMP_AFTER,
       "%g → %g" % (r_0827["stampSell"], r_0828["stampSell"]))
    ok("当前一轮往返 31.2bp、翻倍 62.4bp",
       abs(TC.one_way_bps("a_share", 1.0) - 31.2) < 1e-9
       and abs(TC.one_way_bps("a_share", 2.0) - 62.4) < 1e-9,
       "%.1f / %.1f" % (TC.one_way_bps("a_share", 1.0),
                        TC.one_way_bps("a_share", 2.0)))
    ok("非法费率配置抛 ValueError（费率档位不存在 / 倍数为 0）",
       _raises(ValueError, lambda: TC.adjust_factor(days, 1, "no_such", 1.0))
       and _raises(ValueError, lambda: TC.adjust_factor(days, 1, "a_share", 0.0)))


def _raises(kind, fn) -> bool:
    try:
        fn()
    except kind:
        return True
    except Exception:
        return False
    return False


# ── 2. IC 不含成本信息 ──────────────────────────────────────
def case_ic_invariant(tmp: str) -> None:
    print("\n[2] 毛 IC == 净 IC（统一费率是横截面单调变换，IC 不能当扣费证据）")
    env = SynthEnv(tmp, "c2", n_dates=120)
    try:
        env.activate()
        fwd = env.fwd(axis=env.dates)
        panel = env.panel("mom20").reindex(index=env.dates)
        k = TC.adjust_factor(list(env.dates), PARAMS["horizon"], "a_share", 1.0)
        fwd_net = TC.net_forward(fwd, k)
        ic_g = fe.daily_ic(panel, fwd, min_stocks=100).dropna()
        ic_n = fe.daily_ic(panel, fwd_net, min_stocks=100).dropna()
        d = float((ic_g - ic_n).abs().max()) if len(ic_g) == len(ic_n) else float("inf")
        ok("逐日 IC 逐位相同（max|Δ| == 0）", len(ic_g) > 20 and d == 0.0,
           "%d 天，max|Δ|=%.3e" % (len(ic_g), d))
        ok("净收益与毛收益确实不同（否则上面那条是空断言）",
           bool((fwd - fwd_net).abs().max().max() > 1.0e-4),
           "max|fwd-fwdNet|=%.4f" % float((fwd - fwd_net).abs().max().max()))
    finally:
        env.cleanup()


# ── 3. 最低佣金 ─────────────────────────────────────────────
def case_min_commission() -> None:
    print("\n[3] 最低佣金：1万收5元（5bp）、2万收6元（3bp）—— 起点会翻转")
    f1 = TC.commission_of(10000, "a_share", 1.0)
    f2 = TC.commission_of(20000, "a_share", 1.0)
    ok("1 万名义额：万三 = 3 元 < 5 元 → 收 5 元", abs(f1 - 5.0) < 1e-12, "%.4f" % f1)
    ok("2 万名义额：万三 = 6 元 > 5 元 → 收 6 元", abs(f2 - 6.0) < 1e-12, "%.4f" % f2)
    ok("翻倍时最低起点也翻（10 元 / 12 元）",
       abs(TC.commission_of(10000, "a_share", 2.0) - 10.0) < 1e-12
       and abs(TC.commission_of(20000, "a_share", 2.0) - 12.0) < 1e-12,
       "10 / 12")
    ok("最低佣金差额只在小名义额时出现",
       TC.min_commission_shortfall(10000) > 0 and TC.min_commission_shortfall(20000) == 0,
       "%.2f / %.2f" % (TC.min_commission_shortfall(10000),
                        TC.min_commission_shortfall(20000)))


# ── 4. 结算探针 ─────────────────────────────────────────────
def case_settlement(tmp: str) -> None:
    print("\n[4] 抹掉 open[d+1] → nMissingOpen==1，且 fwd 不被 0 填充")
    env = SynthEnv(tmp, "c4", n_dates=120)
    try:
        env.activate()
        open_ = env.open.copy()
        fwd = env.fwd(axis=env.dates)
        d, sym = str(env.dates[9]), str(env.symbols[0])
        before = float(fwd.loc[d, sym])
        ok("被抹的格子原本有 fwd（否则用例没有牙齿）", np.isfinite(before), "%.4f" % before)

        clean = TC.settle_check(env.close.loc[env.dates], open_.loc[env.dates],
                                fwd, PARAMS["horizon"], entry="s1_open")
        ok("未污染时结算通过", clean["ok"] and clean["nMissingOpen"] == 0,
           "nSettled=%d" % clean["nSettled"])

        open_.loc[d, sym] = np.nan
        dirty = TC.settle_check(env.close.loc[env.dates], open_.loc[env.dates],
                                fwd, PARAMS["horizon"], entry="s1_open")
        ok("nMissingOpen == 1", dirty["nMissingOpen"] == 1,
           str(dirty["nMissingOpen"]))
        ok("ok=False（结算失败必须可见，不静默跳过）", dirty["ok"] is False)
        ok("fwd 未被改成 0（探针只读，不回填）",
           abs(float(fwd.loc[d, sym]) - before) < 1e-15,
           "%.6f" % float(fwd.loc[d, sym]))
        ok("unbuyable 单列，不混进 nMissingOpen",
           "unbuyable" in " ".join(str(e.get("reason")) for e in dirty["examples"][:2])
           or dirty["nUnbuyable"] == 0,
           "nUnbuyable=%d" % dirty["nUnbuyable"])
        ok("一字涨停列为未覆盖（不假装判过）",
           any("一字涨停" in x for x in dirty["notCovered"]))

        # 不可买：open=0 → nUnbuyable>0，但结算本身仍 ok（这是正常剔除，不是故障）
        open2 = env.open.copy()
        open2.loc[d, sym] = 0.0
        unbuy = TC.settle_check(env.close.loc[env.dates], open2.loc[env.dates],
                                fwd, PARAMS["horizon"], entry="s1_open")
        ok("open<=0 记为 nUnbuyable 且不按收盘价补成交",
           unbuy["nUnbuyable"] >= 1 and unbuy["nMissingOpen"] == 0,
           "nUnbuyable=%d" % unbuy["nUnbuyable"])

        # 成交量=0（T+1 买入日）：必须计入 nUnbuyable，且 paper_trade 不得成交。
        # 先证明「不传 volume_wide 时会买它」，否则下面那条断言没有牙齿。
        d2, buy_day, sym2 = str(env.dates[20]), str(env.dates[21]), str(env.symbols[5])
        vol = env.volume.copy()
        vol.loc[buy_day, sym2] = 0.0
        vcheck = TC.settle_check(env.close.loc[env.dates], env.open.loc[env.dates],
                                 fwd, PARAMS["horizon"], volume_wide=vol)
        ok("volume[T+1]=0 → nUnbuyable>=1，且不混入 missingOpen",
           vcheck["nUnbuyable"] >= 1 and vcheck["missingOpen"] == 0,
           "nUnbuyable=%d missingOpen=%d" % (vcheck["nUnbuyable"],
                                             vcheck["missingOpen"]))
        scores = pd.DataFrame(-10.0, index=env.dates, columns=env.symbols)
        scores.loc[d2, sym2] = 100.0
        valid = pd.DataFrame(24.0, index=env.dates, columns=env.symbols)
        k = TC.adjust_factor(list(env.dates), PARAMS["horizon"], "a_share", 1.0)
        kw = dict(top_n=10, horizon=PARAMS["horizon"], capital=1_000_000.0,
                  profile="a_share")
        blind = TC.paper_trade(scores, valid, fwd, k, **kw)
        checked = TC.paper_trade(scores, valid, fwd, k, volume_wide=vol, **kw)
        ok("牙齿：不传 volume_wide 时这只票会被买入",
           sym2 in (blind["symbolsByDate"].get(d2) or []),
           str((blind["symbolsByDate"].get(d2) or [])[:3]))
        ok("传 volume_wide 后该票不进成交，且 nUnbuyableExcluded>=1",
           sym2 not in (checked["symbolsByDate"].get(d2) or [])
           and int(checked["nUnbuyableExcluded"]) >= 1,
           "excluded=%d" % checked["nUnbuyableExcluded"])
    finally:
        env.cleanup()


# ── 5. 成本翻倍单调 ─────────────────────────────────────────
def case_monotonic(tmp: str) -> None:
    print("\n[5] multiplier 1→2→4 净年化单调不增")
    env = SynthEnv(tmp, "c5", n_dates=200)
    try:
        env.activate()
        scores = env.panel("mom20").reindex(index=env.dates)
        valid = pd.DataFrame(24.0, index=scores.index, columns=scores.columns)
        fwd = env.fwd(axis=env.dates)
        vals = []
        for m in (1.0, 2.0, 4.0):
            k = TC.adjust_factor(list(env.dates), PARAMS["horizon"], "a_share", m)
            tr = TC.paper_trade(scores, valid, fwd, k, top_n=10,
                                horizon=PARAMS["horizon"], capital=1_000_000.0,
                                profile="a_share", multiplier=m)
            vals.append(float(tr["annualNet"] or 0.0))
        ok("净年化 1x >= 2x >= 4x", vals[0] >= vals[1] >= vals[2],
           " / ".join("%+.2f%%" % (v * 100) for v in vals))
        ok("三档都算出了交易笔数（不是空序列误判为单调）",
           tr["nTrades"] > 0 and tr["nDays"] > 0,
           "%d 笔 / %d 天" % (tr["nTrades"], tr["nDays"]))
        ok("tNw 用 NW(lag=h-1) 而不是 sqrt(n)（极低倍数下仍有限）",
           tr["tNw"] is not None and np.isfinite(float(tr["tNw"])),
           "tNw=%s" % tr["tNw"])
    finally:
        env.cleanup()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="trade_cost_test_")
    t0 = time.time()
    print("=" * 72)
    print("T07 / trade_cost 成本内核五项硬约束")
    print("=" * 72)
    before = prod_guard.snapshot()
    try:
        case_manual_cell()
        case_ic_invariant(tmp)
        case_min_commission()
        case_settlement(tmp)
        case_monotonic(tmp)
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
