#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_panel.py — 因子面板（长表）落库

表结构（长表，三个维度做主键，天然幂等）：
    factor_panel(date TEXT, symbol TEXT, factor TEXT, value REAL,
                 PRIMARY KEY(date, symbol, factor))
    idx_factor_date(factor, date)      -- 评估时按因子 + 日期扫，走这个索引

为什么用长表而不是「一只票一行、每个因子一列」：
    * 因子会不断增加/废弃，长表加因子不用 ALTER TABLE；
    * 主键天然去重，重跑不会产生重复行（幂等性的根）；
    * 按 (factor, date) 建索引后，「取某因子某日横截面」是一次索引范围扫描。

幂等与增量：
    默认只补缺失行（已存在的不重算不覆盖）。加 --overwrite 才重写已有行——
    前复权数据整段除权后历史因子会变，那种情况才需要 --overwrite 全量刷新。

用法：
    python factor_panel.py                                   # 全市场全因子（慢）
    python factor_panel.py --limit 50 --start 2025-06-03     # 小样本验证
    python factor_panel.py --factors mom20,vol20 --start 2025-06-03
    python factor_panel.py --symbols 600519,000001 --overwrite
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Iterable

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from factors import compute_series, registry  # noqa: E402

DB = os.path.join(HERE, "data", "sequoia_v2.db")

CREATE_TABLE_SQL = (
    "CREATE TABLE IF NOT EXISTS factor_panel ("
    "date TEXT, symbol TEXT, factor TEXT, value REAL, "
    "PRIMARY KEY(date, symbol, factor))"
)
CREATE_INDEX_SQL = "CREATE INDEX IF NOT EXISTS idx_factor_date ON factor_panel(factor, date)"

# 每批写入行数：5000 行一次 executemany + commit，兼顾事务开销与内存
BATCH_SIZE = 5000


# ────────────────────────────────────────────────────────────
# 数据库
# ────────────────────────────────────────────────────────────
def connect(db_path: str = DB) -> sqlite3.Connection:
    """打开连接并建表建索引（幂等）。WAL 让「边写边读」不被锁住。"""
    os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=60)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    init_schema(conn)
    return conn


def init_schema(conn: sqlite3.Connection) -> None:
    """建 factor_panel 表与 (factor, date) 索引。"""
    conn.execute(CREATE_TABLE_SQL)
    conn.execute(CREATE_INDEX_SQL)
    conn.commit()


def list_symbols(conn: sqlite3.Connection, start: str | None = None,
                 end: str | None = None) -> list[str]:
    """从 stock_daily 取有行情的股票代码（按代码升序，保证 --limit 可复现）。"""
    sql = "SELECT DISTINCT symbol FROM stock_daily WHERE 1=1"
    params: list[Any] = []
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    sql += " ORDER BY symbol"
    return [row[0] for row in conn.execute(sql, params).fetchall()]


def load_ohlcv(conn: sqlite3.Connection, symbol: str, end: str | None = None,
               start: str | None = None) -> pd.DataFrame:
    """取单只股票的行情（截至 end；可选从 start 起）。

    为什么不设下界（start=None 时）
    -------------------------------
    短窗口因子（vol20 等）需要 --start 之前的数据做预热，否则 --start 之后
    最初的几十天会全是 NaN。多读的那点数据不落库，只用于计算。

    为什么要能设 start（2026-11-16 增量面板）
    -----------------------------------------
    全历史读 1566 根 K 线再算 24 个因子，逐股要多久就该读多久 —— 但日常增量
    只追加 1~5 个交易日，前面 1560 根纯属重复劳动。传 `start=预热起点` 后
    每只票只读约 320 根（WARMUP_BARS=300 + 目标区间），计算量与 IO 同步下降。

    预热起点的选取必须保证「与读全历史再切片数学等价」：最大的滚动窗口是
    250 日（dist_high250），最大的 min_bars 是 141（mom120），所以只要向前
    多取 300 根就能覆盖（见 panel_incr.WARMUP_BARS）。停牌多的票 300 个自然日
    可能不足 300 根，调用方（panel_incr）会检测到根数不足并回退到全历史。

    Args:
        conn: SQLite 连接。
        symbol: 股票代码。
        end: 截止日期（含）；None 表示不限。
        start: 起始日期（含）；None 表示从最早一根开始（全历史）。

    Returns:
        DataFrame，列固定为 date/open/high/low/close/volume/turnover，按 date 升序。
    """
    sql = ("SELECT date, open, high, low, close, volume, turnover "
           "FROM stock_daily WHERE symbol = ?")
    params: list[Any] = [symbol]
    if end:
        sql += " AND date <= ?"
        params.append(end)
    if start:
        sql += " AND date >= ?"
        params.append(start)
    sql += " ORDER BY date"
    return pd.read_sql(sql, conn, params=params)


# ────────────────────────────────────────────────────────────
# 单只股票 → 待写入行
# ────────────────────────────────────────────────────────────
def build_symbol_rows(task: tuple) -> tuple[str, list[tuple], str | None]:
    """worker：算一只股票的因子面板行。

    Args:
        task: (db_path, symbol, start, end, factors)

    Returns:
        (symbol, rows, err)；rows 元素为 (date, symbol, factor, value)。
        出错时不抛异常（多进程里抛异常难排查），把 err 带回去交给主进程统计。
    """
    db_path, symbol, start, end, factors = task
    conn = None
    try:
        conn = sqlite3.connect(db_path, timeout=60)
        df = load_ohlcv(conn, symbol, end)
        if df.empty:
            return symbol, [], "no_data"
        series = compute_series(df, factors)
        if series.empty:
            return symbol, [], None

        index = pd.Index(series.index.astype(str), name="date")
        if start:
            index_mask = index >= start
        else:
            index_mask = pd.Series(True, index=series.index)
        sub = series.loc[index_mask]

        rows: list[tuple] = []
        # itertuples 比 iterrows 快一个量级，且不构造 Series
        for date, values in zip(sub.index.tolist(), sub.to_numpy()):
            for i, factor in enumerate(factors):
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


def _existing_pairs(conn: sqlite3.Connection, symbol: str, start: str | None,
                    end: str | None) -> set[tuple[str, str]]:
    """已落库的 (factor, date) 集合，用于「只补缺失」。"""
    sql = "SELECT factor, date FROM factor_panel WHERE symbol = ?"
    params: list[Any] = [symbol]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    return {(row[0], row[1]) for row in conn.execute(sql, params).fetchall()}


# ────────────────────────────────────────────────────────────
# 面板构建
# ────────────────────────────────────────────────────────────
def build_panel(db_path: str = DB,
                symbols: Iterable[str] | None = None,
                start: str | None = None,
                end: str | None = None,
                factors: Iterable[str] | None = None,
                limit: int = 0,
                workers: int = 8,
                overwrite: bool = False,
                progress: bool = True) -> dict[str, Any]:
    """构建/补齐因子面板。

    Args:
        db_path: SQLite 路径。
        symbols: 指定股票；None 表示全市场（再受 limit 约束）。
        start: 面板起始日期（含）；None 表示不限。
        end: 面板结束日期（含）；None 表示不限。
        factors: 因子名列表；None 表示全部登记因子。
        limit: 只处理前 N 只（调试用，0 表示不限）。
        workers: 进程数；1 表示主进程串行。
        overwrite: True 时不看已存在行，直接重算重写（复权重算后用）。
        progress: 是否打印进度（测试里关掉）。

    Returns:
        统计字典（symbols_total/symbols_done/symbols_skipped/rows_inserted/
        rows_skipped/errors/elapsed_sec）。
    """
    factor_names = list(factors) if factors else registry.all_names()
    for name in factor_names:
        registry.get(name)                    # 未登记直接 KeyError

    t0 = time.time()
    conn = connect(db_path)
    try:
        if symbols is None:
            symbol_list = list_symbols(conn, start, end)
        else:
            symbol_list = list(symbols)
        if limit and limit > 0:
            symbol_list = symbol_list[:limit]
        if not symbol_list:
            return {"symbols_total": 0, "symbols_done": 0, "symbols_skipped": 0,
                    "rows_inserted": 0, "rows_skipped": 0, "errors": [],
                    "factors": factor_names, "elapsed_sec": 0.0}

        if progress:
            print("[1/2] 待处理 %d 只 × %d 个因子，区间 %s ~ %s，%s，workers=%d"
                  % (len(symbol_list), len(factor_names), start or "最早", end or "最新",
                     "覆盖已有行" if overwrite else "只补缺失行", workers), flush=True)

        tasks = [(db_path, sym, start, end, factor_names) for sym in symbol_list]
        iterator: Iterable[tuple[str, list[tuple], str | None]]
        pool = None
        if workers and workers > 1 and len(tasks) > 1:
            from multiprocessing import Pool
            pool = Pool(processes=min(workers, len(tasks)))
            iterator = pool.imap_unordered(build_symbol_rows, tasks, chunksize=4)
        else:
            iterator = (build_symbol_rows(task) for task in tasks)

        insert_sql = ("INSERT OR REPLACE INTO factor_panel(date, symbol, factor, value) "
                      "VALUES(?,?,?,?)")
        buf: list[tuple] = []
        inserted = 0
        skipped = 0
        done = 0
        no_data = 0
        errors: list[dict[str, str]] = []

        def flush(force: bool = False) -> None:
            nonlocal buf, inserted
            if len(buf) >= BATCH_SIZE or (force and buf):
                conn.executemany(insert_sql, buf)
                conn.commit()
                inserted += len(buf)
                buf = []

        if progress:
            print("[2/2] 计算中…", flush=True)

        try:
            for symbol, rows, err in iterator:
                done += 1
                if err == "no_data":
                    no_data += 1
                elif err:
                    errors.append({"symbol": symbol, "error": err})
                elif rows:
                    if overwrite:
                        buf.extend(rows)
                    else:
                        have = _existing_pairs(conn, symbol, start, end)
                        for row in rows:
                            if (row[2], row[0]) in have:      # (factor, date)
                                skipped += 1
                                continue
                            buf.append(row)
                    flush()
                if progress and (done % 500 == 0 or done == len(symbol_list)):
                    elapsed = time.time() - t0
                    rate = done / elapsed if elapsed > 0 else 0.0
                    print("      %d/%d  写入 %s 行  跳过 %s 行  无数据 %d  失败 %d  "
                          "%.1f 只/秒  剩余 %.1f 分钟"
                          % (done, len(symbol_list), f"{inserted + len(buf):,}",
                             f"{skipped:,}", no_data, len(errors), rate,
                             (len(symbol_list) - done) / rate / 60 if rate else 0.0),
                          flush=True)
        finally:
            if pool is not None:
                try:
                    pool.close()
                    pool.join()
                except Exception:
                    pool.terminate()
                    pool.join()
            flush(force=True)

        stats: dict[str, Any] = {
            "symbols_total": len(symbol_list),
            "symbols_done": done,
            "symbols_skipped": no_data,
            "rows_inserted": inserted,
            "rows_skipped": skipped,
            "errors": errors[:50],
            "factors": factor_names,
            "start": start,
            "end": end,
            "elapsed_sec": round(time.time() - t0, 2),
        }
        return stats
    finally:
        conn.close()


def main() -> int:
    ap = argparse.ArgumentParser(description="构建因子面板（factor_panel 长表）")
    ap.add_argument("--start", default=None, help="面板起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="面板结束日期 YYYY-MM-DD")
    ap.add_argument("--factors", default=None,
                    help="逗号分隔的因子名；默认全部（%d 个）" % len(registry.all_names()))
    ap.add_argument("--symbols", default=None, help="逗号分隔的股票代码；默认全市场")
    ap.add_argument("--limit", type=int, default=0, help="只处理前 N 只（调试用）")
    ap.add_argument("--workers", type=int, default=8, help="进程数；1=串行")
    ap.add_argument("--overwrite", action="store_true",
                    help="重算并覆盖已有行（前复权数据变更后使用）")
    ap.add_argument("--db", default=DB, help="数据库路径")
    args = ap.parse_args()

    factors = [x.strip() for x in args.factors.split(",") if x.strip()] if args.factors else None
    symbols = [x.strip() for x in args.symbols.split(",") if x.strip()] if args.symbols else None

    stats = build_panel(db_path=args.db, symbols=symbols, start=args.start, end=args.end,
                        factors=factors, limit=args.limit, workers=args.workers,
                        overwrite=args.overwrite, progress=True)

    conn = sqlite3.connect(args.db)
    try:
        total = conn.execute("SELECT COUNT(*) FROM factor_panel").fetchone()[0]
        n_sym = conn.execute("SELECT COUNT(DISTINCT symbol) FROM factor_panel").fetchone()[0]
        rng = conn.execute("SELECT MIN(date), MAX(date) FROM factor_panel").fetchone()
    finally:
        conn.close()

    print("\n完成：%d 只（无数据 %d，失败 %d）/ 本次写入 %s 行 / 跳过（已存在）%s 行 / 耗时 %.1fs"
          % (stats["symbols_done"], stats["symbols_skipped"], len(stats["errors"]),
             f'{stats["rows_inserted"]:,}', f'{stats["rows_skipped"]:,}',
             stats["elapsed_sec"]), flush=True)
    print("面板现状：%d 只 / %s 行 / %s ~ %s"
          % (n_sym, f"{total:,}", rng[0], rng[1]), flush=True)
    if stats["errors"]:
        print("失败样例：%s" % stats["errors"][:5], flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
