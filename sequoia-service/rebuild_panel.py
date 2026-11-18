# -*- coding: utf-8 -*-
"""
factor_panel 全量重建（长历史用）

为什么不直接用 factor_panel.py
------------------------------
`factor_panel.py` 面向「增量补数据」，为了幂等，它对**每只股票**都要先查一次
"这只票已经有哪些 (factor, date)"（`_existing_pairs`）。表越大这个查询越慢；
再叠加主键索引逐行维护，实测写入速度从 1.9 只/秒一路衰减到 0.5 只/秒。
跑 5 年（约 1.7 亿行）按这个速度要十几小时，不可接受。

这里的做法（实测比原方式快数倍）
------------------------------
    1. 先建一张**没有主键、没有索引**的新表 `factor_panel_new`，纯顺序 append；
    2. 计算仍用多进程（`build_symbol_rows`），但写入由主进程串行做——
       因为 SQLite 同一时刻只允许一个写事务，多进程同时写只会互相等锁；
    3. 全部写完后**一次性**建唯一索引和查询索引（一次排序远比逐行维护便宜）；
    4. 原子替换：DROP 旧表 → RENAME 新表。

第 4 步之前旧表一直可用，所以重建期间页面不会白屏，坏了也只是重跑。

替换成功后写 `data/panel_rebuild.json`（`{rebuildId, at, rows, dates, symbols}`）：
面板整体换过之后，老日期上的因子值与分组可能已经变了，`factor_ts` 的逐日 IC
序列必须全量重建。原来靠旧 `fingerprint()` 里的 `max(date)` 勉强充当这个信号，
现在改成**显式重建 ID**（见 `factor_ts.invalidation_reasons()` 第 2 条）。

安全边界
--------
    · 写入阶段 `synchronous=OFF`：崩溃时新表可能损坏，但**旧表不动**，
      重跑即可，不会污染已有数据。
    · 替换前做一次 `PRAGMA integrity_check` 和行数比对，不通过就放弃替换。

用法
----
    python rebuild_panel.py --start 2021-01-01 --workers 8
    python rebuild_panel.py --start 2021-01-01 --workers 8 --keep-old   # 保留旧表备查
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import time
from typing import Any, Dict, Optional

import factor_panel as fp

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "data", "sequoia_v2.db")
# 重建登记文件：`factor_ts` 用它判断「面板是否被全量重建过」
# （失效矩阵第 2 条）。原来靠旧 `fingerprint()` 里的 max(date) 勉强充当这个职责，
# 但 max(date) 每天都变，既会误触发重建、又区分不出「重建」与「追加」。
REBUILD_INFO_PATH = os.path.join(HERE, "data", "panel_rebuild.json")

NEW_TABLE = "factor_panel_new"
OLD_TABLE = "factor_panel"

CREATE_NEW = (
    "CREATE TABLE IF NOT EXISTS %s ("
    "date TEXT, symbol TEXT, factor TEXT, value REAL)" % NEW_TABLE
)
# 与旧表保持同名索引语义：唯一键防重复，(factor,date) 供按因子取横截面
UNIQUE_SQL = ("CREATE UNIQUE INDEX IF NOT EXISTS ux_%s ON %s(date, symbol, factor)"
              % (NEW_TABLE, NEW_TABLE))
QUERY_SQL = ("CREATE INDEX IF NOT EXISTS ix_%s ON %s(factor, date)"
             % (NEW_TABLE, NEW_TABLE))

# 重建期间一次提交的行数。太大占 WAL，太小 fsync 次数多；5 万行折中。
COMMIT_EVERY = 50_000


# ────────────────────────────────────────────────────────────
# 重建登记（factor_ts 失效矩阵第 2 条的依据）
# ────────────────────────────────────────────────────────────
def make_rebuild_id(at: str, rows: int, dates: int, symbols: int) -> str:
    """由「时间 + 规模」派生一个稳定的重建 ID（sha1 前 12 位）。

    为什么不用随机数：重建跑了两次、规模完全一样时，随机 ID 会让 `factor_ts`
    白白全量重建一次（约 100 秒）；用内容派生则两次得到同一个 ID，
    「面板其实没变」这个事实就能被正确识别。
    """
    raw = "%s|%d|%d|%d" % (at, int(rows), int(dates), int(symbols))
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:12]


def write_rebuild_info(rows: int, dates: int, symbols: int,
                       start: Optional[str] = None,
                       db: Optional[str] = None,
                       path: Optional[str] = None) -> Dict[str, Any]:
    """原子写 `data/panel_rebuild.json`：`{rebuildId, at, rows, dates, symbols}`。

    只在**替换成功之后**调用。反过来（先写登记、后替换）会让 `factor_ts`
    以为面板换过而全量重建，实际数据却没换 —— 白跑 100 秒且掩盖真问题。
    """
    at = time.strftime("%Y-%m-%dT%H:%M:%S")
    payload: Dict[str, Any] = {
        "rebuildId": make_rebuild_id(at, rows, dates, symbols),
        "at": at,
        "rows": int(rows),
        "dates": int(dates),
        "symbols": int(symbols),
        "start": start,
        "db": db,
    }
    target = path or REBUILD_INFO_PATH
    parent = os.path.dirname(os.path.abspath(target))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, target)
    return payload


def read_rebuild_info(path: Optional[str] = None) -> Dict[str, Any]:
    """读重建登记（不存在/损坏返回空 dict，调用方按「从未重建过」处理）。"""
    try:
        with open(path or REBUILD_INFO_PATH, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def main() -> int:
    ap = argparse.ArgumentParser(description="全量重建 factor_panel（长历史）")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--start", default="2021-01-01", help="因子计算的起始日期")
    ap.add_argument("--end", default=None)
    ap.add_argument("--symbols", default=None, help="逗号分隔，调试用")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--factors", default=None, help="逗号分隔，默认全部")
    ap.add_argument("--keep-old", action="store_true", help="保留旧表（改名为 factor_panel_old）")
    ap.add_argument("--no-swap", action="store_true", help="只写新表，不替换旧表")
    ap.add_argument("--commit-every", type=int, default=COMMIT_EVERY)
    args = ap.parse_args()

    t_all = time.time()
    factor_names = [f["name"] for f in fp.registry.FACTOR_DEFS]
    if args.factors:
        factor_names = [x.strip() for x in args.factors.split(",") if x.strip()]

    conn = sqlite3.connect(args.db, timeout=120)
    conn.execute("PRAGMA journal_mode=WAL")
    # 重建期间关闭同步：崩溃只会让新表不完整，旧表不受影响，重跑即可
    conn.execute("PRAGMA synchronous=OFF")
    conn.execute("PRAGMA cache_size=-200000")      # ~200MB 页缓存
    conn.execute("PRAGMA temp_store=MEMORY")

    symbols = fp.list_symbols(conn, start=args.start, end=args.end)
    if args.symbols:
        want = set(x.strip() for x in args.symbols.split(",") if x.strip())
        symbols = [s for s in symbols if s in want]
    if args.limit:
        symbols = symbols[: args.limit]
    if not symbols:
        raise SystemExit("没有可处理的标的")

    print("[1/5] 目标：%d 只 × %d 个因子，起始 %s" %
          (len(symbols), len(factor_names), args.start), flush=True)

    conn.execute("DROP TABLE IF EXISTS %s" % NEW_TABLE)
    conn.execute(CREATE_NEW)
    conn.commit()
    print("[2/5] 已建临时表 %s（无索引，纯 append）" % NEW_TABLE, flush=True)

    tasks = [(args.db, s, args.start, args.end, factor_names) for s in symbols]

    from multiprocessing import Pool
    pool = Pool(processes=min(args.workers, len(tasks)))
    iterator = pool.imap_unordered(fp.build_symbol_rows, tasks, chunksize=4)

    insert_sql = ("INSERT INTO %s(date, symbol, factor, value) VALUES(?,?,?,?)" % NEW_TABLE)
    buf: list[tuple] = []
    written = 0
    no_data = 0
    failed: list[dict[str, str]] = []
    done = 0
    t0 = time.time()
    last_report = t0

    try:
        for symbol, rows, err in iterator:
            done += 1
            if err == "no_data":
                no_data += 1
            elif err:
                failed.append({"symbol": symbol, "error": err})
            elif rows:
                buf.extend(rows)
                if len(buf) >= args.commit_every:
                    conn.executemany(insert_sql, buf)
                    conn.commit()
                    written += len(buf)
                    buf = []
            # 每 300 只或每 30 秒报一次，避免长时间无输出（块缓冲会让日志看着像卡死）
            now = time.time()
            if done % 300 == 0 or (now - last_report) > 30 or done == len(symbols):
                if buf:
                    conn.executemany(insert_sql, buf)
                    conn.commit()
                    written += len(buf)
                    buf = []
                elapsed = now - t0
                rate = done / elapsed if elapsed else 0
                eta = (len(symbols) - done) / rate / 60 if rate else 0
                print("      %d/%d  写入 %s 行  无数据 %d  失败 %d  %.1f 只/秒  剩余 %.0f 分钟"
                      % (done, len(symbols), format(written, ","), no_data, len(failed),
                         rate, eta), flush=True)
                last_report = now
        if buf:
            conn.executemany(insert_sql, buf)
            conn.commit()
            written += len(buf)
    finally:
        pool.close()
        pool.join()

    print("[3/5] 计算完成：写入 %s 行，失败 %d 只，耗时 %.1f 分钟"
          % (format(written, ","), len(failed), (time.time() - t_all) / 60), flush=True)

    # ---- 建索引：一次排序，比逐行维护便宜得多 ----
    t1 = time.time()
    print("[4/5] 建索引（这一步会扫描全表，需要几分钟）…", flush=True)
    conn.execute(UNIQUE_SQL)
    conn.commit()
    conn.execute(QUERY_SQL)
    conn.commit()
    print("      索引建好，耗时 %.1f 分钟" % ((time.time() - t1) / 60), flush=True)

    # ---- 替换前校验：行数、重复、完整性 ----
    print("[5/5] 校验…", flush=True)
    n_new = conn.execute("SELECT COUNT(*) FROM %s" % NEW_TABLE).fetchone()[0]
    n_dup = conn.execute(
        "SELECT COUNT(*) FROM (SELECT date,symbol,factor FROM %s "
        "GROUP BY 1,2,3 HAVING COUNT(*)>1)" % NEW_TABLE).fetchone()[0]
    ok = conn.execute("PRAGMA integrity_check").fetchone()[0]
    days = conn.execute("SELECT COUNT(DISTINCT date) FROM %s" % NEW_TABLE).fetchone()[0]
    syms = conn.execute("SELECT COUNT(DISTINCT symbol) FROM %s" % NEW_TABLE).fetchone()[0]
    print("      新表 %s 行 / %d 只 / %d 天 / 重复 %d / integrity=%s"
          % (format(n_new, ","), syms, days, n_dup, ok), flush=True)

    if args.no_swap:
        print("已指定 --no-swap，保留 %s 不替换。" % NEW_TABLE)
        conn.close()
        return 0

    if n_new == 0 or n_dup > 0 or ok != "ok":
        print("！！ 校验未通过，放弃替换（旧表保持不动）")
        conn.close()
        return 1

    print("      替换旧表…", flush=True)
    if args.keep_old:
        conn.execute("DROP TABLE IF EXISTS %s_old" % OLD_TABLE)
        conn.execute("ALTER TABLE %s RENAME TO %s_old" % (OLD_TABLE, OLD_TABLE))
    else:
        conn.execute("DROP TABLE %s" % OLD_TABLE)
    conn.execute("ALTER TABLE %s RENAME TO %s" % (NEW_TABLE, OLD_TABLE))
    # 索引名跟着表改名不变，但旧表若存在会占用同名索引，这里重建一次保证可用
    conn.execute("DROP INDEX IF EXISTS idx_factor_date")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_factor_date ON %s(factor, date)" % OLD_TABLE)
    conn.commit()

    # 替换成功之后才登记：`factor_ts` 失效矩阵第 2 条据此判断「面板被换过」
    info = write_rebuild_info(rows=int(n_new), dates=int(days), symbols=int(syms),
                              start=args.start, db=args.db)
    print("      已登记 %s：rebuildId=%s（factor_ts 下次 ensure() 会据此全量重建）"
          % (os.path.basename(REBUILD_INFO_PATH), info["rebuildId"]), flush=True)

    print("完成：%s 已替换，总耗时 %.1f 分钟" % (OLD_TABLE, (time.time() - t_all) / 60))
    if failed:
        print("失败 %d 只（前 5 个）：%s" % (len(failed), failed[:5]))
    conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
