#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""T03 生产规模验收（**一次性手动脚本**，只在缓补齐 24/24 之后运行）。

做的事（全部写生产 `data/factor_ts/`，所以必须显式确认后才跑）：

  1. `panel_cache.coverage()` 断言 24/24（不齐直接 exit 2，什么都不做）
  2. 只加载一次价格宽表（`PriceBundle`），量出那 54.7 秒
  3. `factor_ts.ensure()` 初始化（预估 ~100s）
  4. `factor_ts.append()`（当天应为 0 条，因为 ensure 已建到 cutoff）
  5. `rolling_eval.run()` 首期滚动验证（预估 ~150s）
  6. 打印每一期的 `asOf / icCutoff / 可评估/考虑因子数 / n_selected / selected`
  7. 打印 `ts` 轴信息与「最后 horizon 天被跳过」的日期清单

用法：
    python t03_prod_accept.py --yes            # 真跑
    python t03_prod_accept.py                  # 只做前置检查（1）并退出
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

DB = os.path.join(HERE, "data", "sequoia_v2.db")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--yes", action="store_true", help="真的写生产 data/factor_ts")
    args = ap.parse_args(argv)

    import panel_cache as PC
    import factor_ts as TS
    import rolling_eval as RE

    conn = sqlite3.connect("file:%s?mode=ro" % DB.replace("\\", "/"),
                           uri=True, timeout=180)
    try:
        # ---- 1. 覆盖率闸门 ----
        t0 = time.time()
        cov = PC.coverage(conn)
        print("[1] coverage: %s/%s  ok=%s  (%.2fs)"
              % (len(cov.get("present") or []), len(cov.get("required") or []),
                 cov.get("ok"), time.time() - t0), flush=True)
        if not cov.get("ok"):
            print("    缺：%s\n    形状不符：%s\n→ 什么都不做，先补齐缓存。"
                  % (cov.get("missing"), cov.get("badShape")), flush=True)
            return 2
        if not args.yes:
            print("前置检查通过。真跑请加 --yes。")
            return 0

        # ---- 2. 价格宽表（只加载一次）----
        import run_factor_pipeline as RP
        config = dict(RP.DEFAULTS)
        bundle = RP.PriceBundle(conn, config)
        t0 = time.time()
        price = bundle.get()
        print("[2] 价格宽表：%s，%.1fs（fwd %s）"
              % ("loaded" if bundle.loaded else "未加载", time.time() - t0,
                 price["fwd"].shape), flush=True)

        params = {k: config[k] for k in ("horizon", "entry", "minStocks", "nGroups")}
        axis = TS.panel_dates_fast(conn)
        print("    面板轴：%d 天（%s ~ %s）" % (len(axis), axis[0], axis[-1]))

        # ---- 3. ensure ----
        t0 = time.time()
        ens = TS.ensure(conn, price["fwd"], params, panel_dates_axis=axis)
        print("[3] ensure: action=%s reasons=%s rebuilt=%d 保护式=%d  %.1fs"
              % (ens["action"], ens["reasons"], len(ens["rebuilt"]),
                 len(ens["protective"]), time.time() - t0), flush=True)
        # `ens["axis"]` 是 dict：{dates, first, last, cutoff}。
        # 原来这里用 `""` 充当 cutoff，输出会长成 `cutoff=` —— 一行没法核对的空值。
        # cutoff 就是「末端能算出 IC 的最晚一天」（axis[-1] - horizon），
        # 与 append / 第 6 条保护式重算用的是同一个 `factor_ts.cutoff_date()`。
        ax = ens["axis"]
        print("    轴：%d 天（%s ~ %s），末端可用 cutoff=%s"
              % (ax["dates"], ax["first"], ax["last"], ax["cutoff"]), flush=True)
        # commonFactorDays 是 (因子, 日) 累加，不是交易日数 —— 写「天」会让读者
        # 以为重叠了 33187 个交易日（A 股一年才 244 天）。
        print("    对账：state=%s 重叠 %d 个(因子,日) 最大差 %.2e 口径差 %d 天(%.1f%%)"
              " dataWarn=%s caliberWarn=%s rtol=%.0e"
              % (ens["reconcile"]["state"], ens["reconcile"]["commonFactorDays"],
                 ens["reconcile"]["maxAbsDiff"], ens["reconcile"]["extraDays"],
                 ens["reconcile"]["extraPct"] * 100,
                 ens["reconcile"]["dataWarn"], ens["reconcile"]["caliberWarn"],
                 ens["reconcile"]["rtol"]), flush=True)

        # ---- 4. append ----
        t0 = time.time()
        app = TS.append(conn, price["fwd"], panel_dates_axis=axis)
        print("[4] append: appended=%d 因子=%d skipped=%d 天 %s  failed=%s  %.1fs"
              % (app["appended"], app["factors"], len(app["skipped"]),
                 app["skipped"], app["failed"], time.time() - t0), flush=True)

        # ---- 5. rolling ----
        rparams = {k: config[k] for k in
                   ("horizon", "entry", "minStocks", "nwLag", "nGroups",
                    "rollStep", "rollSelectWindow")}
        t0 = time.time()
        roll = RE.run(conn, rparams, panel_dates_axis=axis)
        print("[5] rolling: ok=%s code=%s %d 期，%.1fs"
              % (roll.get("ok"), roll.get("code"), roll.get("n_periods") or 0,
                 time.time() - t0), flush=True)

        # ---- 6. 逐期明细 ----
        # 因子写成「可评估/考虑」：`n_factors` = 可评估（有 t 值且非「数据不足」），
        # `n_considered` = 进 rows 的。早期期两者不等是正常的（预热期短，窗口内天数不足）。
        for p in (roll.get("periods") or []):
            print("    T=%-3s asOf=%s cut=%s 因子 %d/%d 入选 %d  %s"
                  % (p.get("tIndex"), p.get("asOf"), p.get("icCutoff"),
                     p.get("n_evaluable") or 0, p.get("n_considered") or 0,
                     p.get("n_selected") or 0,
                     ",".join(p.get("selected") or []) or "—"), flush=True)
        print("    latest: %s" % json.dumps(roll.get("latest") or {},
                                            ensure_ascii=False), flush=True)
        print("    skippedFactors: %s" % (roll.get("skippedFactors") or []), flush=True)
        print("    lag: %s" % json.dumps(roll.get("lag") or {}, ensure_ascii=False)[:400],
              flush=True)

        # ---- 7. ts 轴 ----
        print("[7] ts 轴：%s" % json.dumps(TS.axis_info(), ensure_ascii=False), flush=True)
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
