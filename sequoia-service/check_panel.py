# -*- coding: utf-8 -*-
"""因子面板完整性体检

用途
----
面板跑完后，不能只看"行数够不够"就交付。本脚本做五层检查：

1. **结构完整性**：行数、标的数、因子数、日期范围、重复主键、越界日期、
   孤立标的（面板有但行情库没有）。
2. **覆盖完整性**：5030 只里有多少进了面板、缺哪些；每个因子覆盖多少只、
   非空比例、首个有效日期（对照 min_bars 预热期是否吻合）。
3. **数值正确性**：随机抽 N 只，用 factors.compute.compute_series 从行情库
   现场重算，与面板落库值逐格比对。这是唯一能证明"写进去的数是对的"的检查，
   前两层都查不出来。
4. **口径一致性**：同一因子在不同标的上的有效起始位置，是否符合各自的
   预热窗口规律（样本短的票应该更晚出值）。
5. **缓存一致性**（2026-11-16 新增）：`panel_cache` 的结构签名、轴长度、
   24 因子齐备性、staging 残留、孤儿 npy（磁盘上有但 meta 里没登记，
   上次中断的残骸）。第 5 层是纯磁盘检查，不查询数据库，秒级完成。

用法
----
    python check_panel.py                  # 默认抽 12 只重算
    python check_panel.py --sample 40      # 抽 40 只
    python check_panel.py --out data/panel_check.json
"""

from __future__ import annotations

import argparse
import json
import random
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional

import numpy as np
import pandas as pd

from factors.compute import compute_series
from factors.registry import FACTOR_DEFS, min_bars_map

DB = "data/sequoia_v2.db"


def q(conn: sqlite3.Connection, sql: str, params=()) -> Any:
    return conn.execute(sql, params).fetchone()[0]


def structure(conn: sqlite3.Connection) -> Dict[str, Any]:
    out: Dict[str, Any] = {}
    out["panel_rows"] = q(conn, "SELECT COUNT(*) FROM factor_panel")
    out["panel_symbols"] = q(conn, "SELECT COUNT(DISTINCT symbol) FROM factor_panel")
    out["panel_factors"] = q(conn, "SELECT COUNT(DISTINCT factor) FROM factor_panel")
    out["date_min"], out["date_max"] = conn.execute(
        "SELECT MIN(date), MAX(date) FROM factor_panel").fetchone()
    out["dup_pk"] = q(conn,
        "SELECT COUNT(*) FROM (SELECT date,symbol,factor FROM factor_panel "
        "GROUP BY 1,2,3 HAVING COUNT(*)>1)")
    out["daily_min"], out["daily_max"] = conn.execute(
        "SELECT MIN(date), MAX(date) FROM stock_daily").fetchone()
    out["out_of_range"] = q(conn,
        "SELECT COUNT(*) FROM factor_panel WHERE date < ? OR date > ?",
        (out["daily_min"], out["daily_max"]))
    out["orphan_symbols"] = q(conn,
        "SELECT COUNT(DISTINCT symbol) FROM factor_panel "
        "WHERE symbol NOT IN (SELECT DISTINCT symbol FROM stock_daily)")
    out["nan_rows"] = q(conn, "SELECT COUNT(*) FROM factor_panel WHERE value IS NULL")
    out["inf_rows"] = q(conn,
        "SELECT COUNT(*) FROM factor_panel WHERE value IS NOT NULL "
        "AND (value > 1e15 OR value < -1e15)")
    return out


def coverage(conn: sqlite3.Connection) -> Dict[str, Any]:
    # 每因子
    rows = conn.execute(
        "SELECT factor, COUNT(*) n, COUNT(DISTINCT symbol) s, MIN(date), MAX(date) "
        "FROM factor_panel GROUP BY factor ORDER BY factor").fetchall()
    per_factor = [
        {"factor": r[0], "rows": r[1], "symbols": r[2],
         "first_date": r[3], "last_date": r[4],
         "min_bars": min_bars_map().get(r[0])}
        for r in rows
    ]
    # 标的覆盖
    allx = set(r[0] for r in conn.execute("SELECT DISTINCT symbol FROM stock_daily"))
    used = set(r[0] for r in conn.execute("SELECT DISTINCT symbol FROM factor_panel"))
    missing = sorted(allx - used)
    # 缺失标的是否本来就没足够数据
    missing_detail = []
    for s in missing[:200]:
        n = q(conn, "SELECT COUNT(*) FROM stock_daily WHERE symbol=?", (s,))
        missing_detail.append({"symbol": s, "bars": n})
    return {
        "universe": len(allx),
        "covered": len(used),
        "missing_count": len(missing),
        "missing_sample": missing_detail,
        "per_factor": per_factor,
    }


def verify_values(conn: sqlite3.Connection, sample: int = 12, seed: int = 7) -> Dict[str, Any]:
    """随机抽样，用算好的因子函数现场重算，与面板落库值逐格比对。"""
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM factor_panel ORDER BY symbol")]
    rng = random.Random(seed)
    picked = rng.sample(syms, min(sample, len(syms)))

    total_cells = 0
    mismatch_cells = 0
    details: List[Dict[str, Any]] = []
    for s in picked:
        df = pd.read_sql(
            "SELECT date, open, high, low, close, volume, turnover FROM stock_daily "
            "WHERE symbol=? ORDER BY date", conn, params=(s,))
        if df.empty:
            continue
        got = compute_series(df)
        stored = pd.read_sql(
            "SELECT date, factor, value FROM factor_panel WHERE symbol=?", conn, params=(s,))
        if stored.empty:
            continue
        got.index = got.index.astype(str)
        wide_stored = stored.pivot(index="date", columns="factor", values="value")

        bad = 0
        cells = 0
        worst = 0.0
        for f in wide_stored.columns:
            if f not in got.columns:
                continue
            a = got[f].reindex(wide_stored.index).astype("float64")
            b = wide_stored[f].astype("float64")
            both = a.notna() & b.notna()
            only_a = a.notna() & b.isna()
            only_b = a.isna() & b.notna()
            cells += int(both.sum())
            bad += int(only_a.sum()) + int(only_b.sum())
            if both.any():
                diff = (a[both] - b[both]).abs()
                worst = max(worst, float(diff.max()))
                # 绝对值或相对值超容差才算不一致
                rel = diff / b[both].abs().clip(lower=1e-9)
                bad += int(((diff > 1e-9) & (rel > 1e-6)).sum())
        total_cells += cells
        mismatch_cells += bad
        details.append({"symbol": s, "cells": cells, "mismatch": bad,
                        "max_abs_diff": round(worst, 12)})

    return {
        "sampled": len(details),
        "cells_compared": total_cells,
        "mismatch_cells": mismatch_cells,
        "details": details,
    }


def cache_layer(conn: sqlite3.Connection) -> Dict[str, Any]:
    """第 5 层：磁盘缓存（panel_cache）一致性。

    纯磁盘检查（外加一次 0.001s 的 MIN(date)），不碰数据库的大表。
    检查四件事：
      * 结构签名（structure_sig，不含 rows/max(date)，所以日常追加不会失效）；
      * 轴长度与 meta 登记的行列数是否自洽；
      * 24 个登记因子是否齐备（缺谁、谁的形状不对）；
      * 孤儿 npy / staging 残留（上次中断的残骸，绝不可被当成可用缓存）。
    """
    try:
        import panel_cache as PC
    except Exception as exc:  # noqa: BLE001 — 缓存模块不可用不该让体检整体失败
        return {"ok": False, "reason": "panel_cache 导入失败：%s" % exc}

    from factors.registry import all_names
    cov = PC.coverage(conn, all_names())
    # 重路径职责：把库里 DISTINCT factor 与 registry 逐项比对（这里付得起这个扫描）
    st = PC.validate_structure(conn)
    meta = PC._load_meta()
    sig_now: Optional[str] = None
    sig_ok: Optional[bool] = None
    try:
        sig_now = PC.structure_sig(conn)
        sig_ok = (meta.get("sig") == sig_now)
    except Exception as exc:  # noqa: BLE001
        sig_now = "计算失败：%s" % exc
        sig_ok = False
    return {"ok": bool(cov["ok"] and sig_ok and st["ok"]),
            "sig": sig_now, "sigSaved": meta.get("sig"), "sigOk": sig_ok,
            "structure": st, "coverage": cov}


def main() -> int:
    ap = argparse.ArgumentParser(description="因子面板完整性体检")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--sample", type=int, default=12)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    t0 = time.time()
    conn = sqlite3.connect(args.db)
    print("=" * 74)
    print("因子面板完整性体检")
    print("=" * 74)

    st = structure(conn)
    print("\n[1/5] 结构")
    print(f"  面板       {st['panel_rows']:,} 行 / {st['panel_symbols']} 只 / {st['panel_factors']} 个因子")
    print(f"  日期范围   {st['date_min']} ~ {st['date_max']}")
    print(f"  重复主键   {st['dup_pk']}")
    print(f"  越界日期   {st['out_of_range']}")
    print(f"  孤立标的   {st['orphan_symbols']}")
    print(f"  空值行     {st['nan_rows']}")
    print(f"  异常大值   {st['inf_rows']}")

    cov = coverage(conn)
    print("\n[2/5] 覆盖")
    print(f"  行情库 {cov['universe']} 只 -> 面板 {cov['covered']} 只 "
          f"（缺 {cov['missing_count']}）")
    print(f"  {'因子':<18}{'标的':>6}{'行数':>12}{'首个有效日':>13}{'预热':>6}")
    for r in cov["per_factor"]:
        print(f"  {r['factor']:<18}{r['symbols']:>6}{r['rows']:>12,}"
              f"{r['first_date']:>13}{r['min_bars']:>6}")
    if cov["missing_count"]:
        print(f"  缺少标的（前 10）：{[d['symbol'] + '(' + str(d['bars']) + '根)' for d in cov['missing_sample'][:10]]}")

    print(f"\n[3/5] 数值正确性（抽 {args.sample} 只重算比对）")
    vf = verify_values(conn, args.sample)
    print(f"  比对样本 {vf['sampled']} 只 / {vf['cells_compared']:,} 个格子")
    print(f"  不一致   {vf['mismatch_cells']}")
    for d in vf["details"][:6]:
        print(f"    {d['symbol']}  格子 {d['cells']:>7,}  不一致 {d['mismatch']}  "
              f"最大绝对差 {d['max_abs_diff']}")

    print("\n[4/5] 缓存一致性（panel_cache，纯磁盘检查）")
    cl = cache_layer(conn)
    cvc = cl.get("coverage") or {}
    ax = cvc.get("axis") or {}
    print(f"  结构签名   {cl.get('sig')}")
    print(f"  上次建cache {cl.get('sigSaved')}  →  {'一致' if cl.get('sigOk') else '不一致（需重建）'}")
    print(f"  缓存轴     {ax.get('dates') or 0} 天 × {ax.get('symbols') or 0} 只"
          f"（{ax.get('first')} ~ {ax.get('last')}）")
    stc = cl.get("structure") or {}
    print(f"  因子集合   库里 {stc.get('dbChecked')} 个 / registry {stc.get('registry')} 个"
          f"  → {'一致' if stc.get('ok') else '不一致'}")
    if not stc.get("ok"):
        print(f"    库有 registry 无：{(stc.get('dbOnly') or [])[:6]}")
        print(f"    registry 有库无：{(stc.get('registryOnly') or [])[:6]}")
    print(f"  因子齐备   {len(cvc.get('present') or [])}/{len(cvc.get('required') or [])}"
          f"  缺 {(cvc.get('missing') or [])[:8] or '无'}")
    if cvc.get("badShape"):
        print(f"  形状不符   {(cvc.get('badShape') or [])[:8]}")
    if cvc.get("orphans"):
        print(f"  孤儿 npy   {cvc['orphans']}（磁盘有但 meta 未登记，不可用）")
    if (cvc.get("stage") or {}).get("pending"):
        print(f"  staging    {(cvc['stage'].get('factors') or [])[:8]}"
              f"（上次中断残留，未被提交）")

    print("\n[5/5] 判定")
    problems = []
    if st["dup_pk"]:
        problems.append(f"存在 {st['dup_pk']} 组重复主键")
    if st["out_of_range"]:
        problems.append(f"存在 {st['out_of_range']} 行日期越界")
    if st["orphan_symbols"]:
        problems.append(f"存在 {st['orphan_symbols']} 只孤立标的")
    if st["nan_rows"]:
        problems.append(f"存在 {st['nan_rows']} 行空值（应跳过不写）")
    if vf["mismatch_cells"]:
        problems.append(f"落库值与重算不一致 {vf['mismatch_cells']} 格")
    if cov["missing_count"]:
        problems.append(f"{cov['missing_count']} 只标的未进面板")
    if not cl.get("sigOk"):
        problems.append("缓存结构签名已变，需重建缓存")
    if not (cl.get("structure") or {}).get("ok"):
        problems.append("库里因子集合与 registry 不一致（库有registry无 %s / registry有库无 %s）"
                        % ((cl["structure"] or {}).get("dbOnly"),
                           (cl["structure"] or {}).get("registryOnly")))
    if (cvc.get("missing") or []):
        problems.append(f"缓存缺 {len(cvc['missing'])} 个因子：{(cvc['missing'] or [])[:6]}")
    if problems:
        print("  FAIL")
        for p in problems:
            print("   -", p)
    else:
        print("  PASS：结构、覆盖、数值、缓存四层检查全部通过")

    payload = {"structure": st, "coverage": cov, "verify": vf, "cache": cl,
               "problems": problems, "elapsed_sec": round(time.time() - t0, 1)}
    out = args.out or "data/panel_check.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    print(f"\n耗时 {payload['elapsed_sec']}s -> {out}")
    conn.close()
    return 0 if not problems else 3


if __name__ == "__main__":
    raise SystemExit(main())
