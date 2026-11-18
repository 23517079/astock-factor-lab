# -*- coding: utf-8 -*-
"""分板块因子体检（跨板块稳健性对照）

为什么要这个脚本
----------------
全市场跑一个因子的 RankIC，只能得到一个总体数字。但 A 股各板块的
市值结构、涨跌幅限制、投资者构成差异很大（科创板/创业板 20% 涨跌停、
主板 10%），一个因子可能在 688 强、在 600 弱，合并成一条 IC 后
两种相反的效果会互相抵消，看起来"不显著"，实际上是在两个板块里
各自显著但方向相反 —— 这种因子直接上生产会亏钱。

本脚本把同一份面板按代码段切开，对每个板块分别算 RankIC 与
Newey-West t，再看符号是否一致。判据：
  - 各板块 IC 同号且多数显著 -> 跨板块稳健，可进下一步
  - 符号不一致              -> 板块依赖，不能全市场通用（要么分板块用，要么丢弃）
  - 只有单板块显著          -> 样本内噪声风险高，需扩样本再判

复用 factor_eval 的实现，确保与主评估口径完全一致（同一 entry、同一 NW lag）。

性能约定
--------
价格宽表按板块缓存一次、因子面板按因子加载一次（全市场），再按板块切列。
避免「因子 × 板块」的重复查询 —— 38M 行面板上重复查 60 次会慢一个数量级。

用法
----
    python segment_ic.py                          # 默认 10 个代表因子, h=5
    python segment_ic.py --factors vol20,rev5     # 指定因子
    python segment_ic.py --all                    # 全部 24 个因子
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
from typing import Dict, List, Optional

import pandas as pd

import factor_eval as fe
from factors.registry import all_names, get as get_factor

# 板块划分：按代码前缀。北交所（8/4 开头）当前库里没有数据，不列。
SEGMENTS: Dict[str, List[str]] = {
    "深主板": ["000", "001"],
    "深中小": ["002", "003"],
    "创业板": ["300", "301"],
    "沪主板": ["600", "601", "603", "605"],
    "科创板": ["688"],
}

# 默认检查的因子：覆盖各主要类别，且优先看上一轮 NW 口径下显著的候选
DEFAULT_FACTORS = [
    "rev10", "vol20", "atr_pct14", "max_ret20", "turn_cv20",
    "ma_gap20", "ma_gap60", "slope20", "corr_pv20", "intraday_ret20",
]

DB = "data/sequoia_v2.db"


def load_segment_symbols(conn: sqlite3.Connection) -> Dict[str, List[str]]:
    allx = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM stock_daily ORDER BY symbol")]
    return {
        name: [s for s in allx if any(s.startswith(p) for p in prefixes)]
        for name, prefixes in SEGMENTS.items()
    }


def prepare_forward(
    conn: sqlite3.Connection,
    pools: Dict[str, List[str]],
    horizon: int,
    entry: str,
) -> Dict[str, pd.DataFrame]:
    """每个板块只加载一次价格宽表并算好未来收益标签。"""
    out: Dict[str, pd.DataFrame] = {}
    for name, syms in pools.items():
        close_wide = fe.load_price_wide(conn, "close", syms)
        if close_wide.empty:
            continue
        open_wide = fe.load_price_wide(conn, "open", syms)
        out[name] = fe.load_forward_returns(close_wide, horizon, entry=entry, open_wide=open_wide)
    return out


def ic_one(panel_wide: pd.DataFrame, fwd: pd.DataFrame, horizon: int,
           min_stocks: int) -> Optional[dict]:
    if panel_wide.empty or fwd.empty:
        return None
    ic = fe.daily_ic(panel_wide, fwd, min_stocks=min_stocks).dropna()
    n = len(ic)
    if n < 20:
        return None
    mean = float(ic.mean())
    std = float(ic.std(ddof=1))
    lag = max(0, horizon - 1)
    return {
        "ic_mean": mean,
        "ic_std": std,
        "n_days": n,
        "t_naive": (mean / std * (n ** 0.5)) if std > 0 else None,
        "t_nw": fe.newey_west_t(ic, lag) if std > 0 else None,
        "lag": lag,
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="分板块因子体检")
    ap.add_argument("--factors", default="", help="逗号分隔；为空用内置 10 个代表因子")
    ap.add_argument("--all", action="store_true", help="全部因子")
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--entry", default="s1_open", choices=["s1_open", "s1_close", "close"])
    ap.add_argument("--min-stocks", type=int, default=30)
    ap.add_argument("--out", default="data/segment_ic.json")
    ap.add_argument("--db", default=DB)
    args = ap.parse_args()

    if args.horizon < 1:
        print("--horizon 必须 >= 1")
        return 1
    if args.all:
        names = all_names()
    else:
        names = [s.strip() for s in args.factors.split(",") if s.strip()] or DEFAULT_FACTORS
    for n in names:
        get_factor(n)

    conn = sqlite3.connect(args.db)
    seg_syms = load_segment_symbols(conn)
    allx = sorted({s for v in seg_syms.values() for s in v})
    print("板块规模：", {k: len(v) for k, v in seg_syms.items()})
    print(f"合计 {len(allx)} 只 / 因子 {len(names)} 个 / horizon={args.horizon} / entry={args.entry}\n")

    t0 = time.time()
    pools_all = {"全市场": allx, **seg_syms}
    fwd_by_pool = prepare_forward(conn, pools_all, args.horizon, args.entry)
    print(f"未来收益标签已就绪（{time.time() - t0:.0f}s），开始逐因子计算\n")

    payload = {
        "meta": {
            "generated_at": time.strftime("%Y-%m-%d %H:%M:%S"),
            "horizon": args.horizon,
            "entry": args.entry,
            "min_stocks": args.min_stocks,
            "segments": {k: len(v) for k, v in seg_syms.items()},
            "n_symbols": len(allx),
            "factors": names,
        },
        "rows": [],
    }

    hdr = f"{'因子':<16}{'池':<8}{'IC均值':>10}{'t_NW':>9}{'天数':>7}"
    print(hdr)
    print("-" * len(hdr))

    for f in names:
        panel_all = fe.load_panel_wide(conn, f)  # 全市场一次
        if panel_all.empty:
            print(f"{f:<16}面板为空，跳过")
            continue
        for pool_name in ["全市场"] + list(seg_syms.keys()):
            fwd = fwd_by_pool.get(pool_name)
            if fwd is None:
                continue
            if pool_name == "全市场":
                panel = panel_all
            else:
                cols = [c for c in seg_syms[pool_name] if c in panel_all.columns]
                if len(cols) < args.min_stocks:
                    continue
                panel = panel_all[cols]
            r = ic_one(panel, fwd, args.horizon, args.min_stocks)
            if r is None:
                print(f"{f:<16}{pool_name:<8}{'样本不足':>10}")
                continue
            r.update({"factor": f, "pool": pool_name,
                      "direction": get_factor(f)["direction"],
                      "n_symbols": int(panel.shape[1])})
            payload["rows"].append(r)
            tnw = f"{r['t_nw']:+.2f}" if r["t_nw"] is not None else "-"
            print(f"{f:<16}{pool_name:<8}{r['ic_mean']:>+10.4f}{tnw:>9}{r['n_days']:>7}")

        rs = [x for x in payload["rows"] if x["factor"] == f and x["pool"] != "全市场"]
        if rs:
            signs = {1 if x["ic_mean"] > 0 else -1 for x in rs}
            sig = [x for x in rs if x["t_nw"] is not None and abs(x["t_nw"]) >= 2]
            verdict = (f"符号一致（{len(sig)}/{len(rs)} 个板块显著）" if len(signs) == 1
                       else f"符号不一致（{len(signs)} 种方向）")
            print(f"{'':16}-> {verdict}")
        print()

    print("=" * 72)
    print("稳健性汇总（符号一致性 + 显著板块数；判定以 NW t 为准）")
    print("=" * 72)
    summary = []
    for f in names:
        rs = [x for x in payload["rows"] if x["factor"] == f and x["pool"] != "全市场"]
        if not rs:
            continue
        signs = {1 if x["ic_mean"] > 0 else -1 for x in rs}
        sig_pools = [x["pool"] for x in rs if x["t_nw"] is not None and abs(x["t_nw"]) >= 2]
        s = {
            "factor": f,
            "direction": get_factor(f)["direction"],
            "sign_consistent": len(signs) == 1,
            "n_significant": len(sig_pools),
            "significant_pools": sig_pools,
        }
        summary.append(s)
        flag = ("稳健" if len(signs) == 1 and len(sig_pools) >= 3
                else ("板块依赖" if len(signs) > 1 else "证据弱"))
        print(f"{f:<16}{flag:<10}显著 {len(sig_pools)}/{len(rs)}  {','.join(sig_pools) or '-'}")

    payload["summary"] = summary
    payload["elapsed_sec"] = round(time.time() - t0, 1)
    with open(args.out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\n耗时 {payload['elapsed_sec']}s -> {args.out}")
    print("提醒：板块内样本更小，t 值更易受噪声影响；符号不一致比不显著更值得警惕。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
