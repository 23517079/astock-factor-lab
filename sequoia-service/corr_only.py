"""只补因子相关矩阵，不重跑 IC / 分组 / 多空 / 换手。

为什么单独做这件事
------------------
全量重跑一次评估是按因子逐个去扫 factor_panel（5 年面板 1.5 亿行），
时间几乎全花在 SQLite 的随机读上（实测 CPU 只占 15%，其余都在等 IO），
一次要几个小时。但相关矩阵回答的只是「最近这段行情里这些因子是不是在
说同一件事」，只需要最近一百多个交易日的面板（三十万行量级），
单独算几秒就够，没必要为它把全部指标重算一遍。

用法：
    python corr_only.py                          # 补到 data/factor_eval.json
    python corr_only.py --days 180 --out xxx.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import time

import pandas as pd

import factor_eval as fe

DB = "data/sequoia_v2.db"
EVAL = "data/factor_eval.json"


def load_recent_panels(conn: sqlite3.Connection, factors: list[str],
                       days: int = 60) -> dict[str, pd.DataFrame]:
    """取最近 days 个交易日的面板，返回 factor -> 宽表(date × symbol)。

    按因子**逐个**读：一批读出来是 days×5030×因子数 行（24 因子 120 天就是
    1400 万行），pandas 会直接吃光内存；逐个读每次只有几十万行，峰值内存
    只跟单个宽表有关。相关矩阵本身就是逐对计算的，分次读没有额外代价。
    """
    rows = conn.execute(
        "SELECT DISTINCT date FROM factor_panel ORDER BY date DESC LIMIT ?", (days,)
    ).fetchall()
    if not rows:
        raise SystemExit("factor_panel 是空的")
    dates = sorted(r[0] for r in rows)
    d0 = dates[0]

    out: dict[str, pd.DataFrame] = {}
    n_rows = 0
    for name in factors:
        df = pd.read_sql_query(
            "SELECT date, symbol, value FROM factor_panel WHERE date >= ? AND factor = ?",
            conn, params=[d0, name])
        if df.empty:
            continue
        n_rows += len(df)
        wide = df.pivot(index="date", columns="symbol", values="value")
        wide.index = pd.to_datetime(wide.index)
        out[name] = wide.sort_index()
    if not out:
        raise SystemExit("最近 %d 天没有可用的面板数据" % days)
    print("  载入 %d 个因子 × %d 天 × 最多 %d 只（共 %.0f 万行）"
          % (len(out), len(dates), max(p.shape[1] for p in out.values()),
             n_rows / 1e4))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="只重算因子相关矩阵并合入评估结果")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--eval", default=EVAL, dest="eval_path")
    ap.add_argument("--days", type=int, default=60, help="用最近多少个交易日估相关")
    ap.add_argument("--max-dates", type=int, default=60, help="等距抽样到多少个交易日")
    ap.add_argument("--out", default=None, help="默认原地写回 --eval")
    args = ap.parse_args()

    path = args.out or args.eval_path
    payload = json.load(open(args.eval_path, encoding="utf-8"))
    factors = [n for n, v in (payload.get("factors") or {}).items()
               if isinstance(v, dict) and "rank_ic" in v]
    if len(factors) < 2:
        raise SystemExit("评估结果里可用因子不足 2 个，无法算相关")

    t0 = time.time()
    conn = sqlite3.connect(args.db, timeout=60)
    try:
        panels = load_recent_panels(conn, factors, args.days)
    finally:
        conn.close()

    matrix = fe.corr_matrix(panels, max_dates=args.max_dates)
    payload["corr_matrix"] = matrix
    meta = payload.setdefault("meta", {})
    meta["corrWindow"] = {"days": args.days, "maxDates": args.max_dates,
                          "note": "相关矩阵只用最近这段交易日估计（相关是局部属性），"
                                  "其余指标为全样本"}
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)

    # 自检：对角线为 1、有值的格子数、极端相关对
    names = [n for n in matrix if matrix[n]]
    diag_ok = all(abs((matrix[n].get(n) or 0) - 1.0) < 1e-9 for n in names)
    vals = []
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            v = matrix[a].get(b)
            if v is not None:
                vals.append((abs(v), v, a, b))
    vals.sort(reverse=True)
    print("  对角线全为 1：%s" % ("是" if diag_ok else "否"))
    print("  有效相关对：%d / %d" % (len(vals), len(names) * (len(names) - 1) // 2))
    print("  相关性最强的 5 对（|rho| 越大越像同一个东西）：")
    for a, v, x, y in vals[:5]:
        print("    %-16s %-16s rho=%+.3f" % (x, y, v))
    print("  耗时 %.1fs，已写 %s" % (time.time() - t0, path))
    return 0 if diag_ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
