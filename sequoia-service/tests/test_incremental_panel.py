#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_incremental_panel.py — T02 增量面板计算的验收测试

覆盖 5 条验收（全部在**临时库**上做，绝不写生产库）：

  1. 增量 vs 独立全量重算：随机 200 个 (symbol, factor, date) 三元组
     相对误差 ≤ 1e-4，**强制覆盖 mom120 / mom60 / dist_high250 / obv_slope20**
  2. 连跑两次，`factor_panel` 行数前后相等（幂等）
  3. `COUNT(DISTINCT date)` 增量结果 == 原有日期数 + 新增交易日数
  4. 生产规模实测单次 ≤ 10 分钟，耗时写入 `factor_run.cost`
  5. `PRAGMA index_list(factor_panel)` 名称与列序与改前完全一致

外加一个「有牙齿」的对照：把预热窗口关掉（`warmup_bars=0`），
`mom120` 这类长窗口因子的行会大面积消失 —— 证明本测试真的能抓住
「没做预热」这种实现错误，而不是无论怎么实现都通过。

`obv_slope20` 为什么必须在覆盖名单里
────────────────────────────────────
它用 `obv = (sign(ret) * volume).cumsum()` 算 OBV。cumsum 的**绝对水平**
依赖序列起点，截断预热窗口后 OBV 整体平移了一个常数。但因子取的是
「20 日 OLS 斜率 / 20 日均量」，OLS 斜率对 y 的常数偏移不变
（分子 `m*Σxy - Σx*Σy` 中 `y→y+c` 时两项同时减去 `c*m*Σx`，抵消）。
所以截断在数学上等价 —— 但这条不靠推理，靠这里的实测数字证明。

运行：
    python tests/test_incremental_panel.py
    PANEL_INCR_SKIP_PROD=1 python tests/test_incremental_panel.py   # 跳过生产规模实测
"""

from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for _p in (HERE, TESTS_DIR):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import factor_panel as FP                       # noqa: E402
import factor_run as FR                         # noqa: E402
import panel_incr as PI                         # noqa: E402
from factors.compute import compute_series      # noqa: E402
from factors.registry import all_names          # noqa: E402
import prod_guard                               # noqa: E402

PROD_DB = os.path.join(HERE, "data", "sequoia_v2.db")

PASS = 0
FAIL = 0
FAILS = []


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s%s" % (name, ("  " + extra) if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  [FAIL] %s%s" % (name, ("  " + extra) if extra else ""))


# ────────────────────────────────────────────────────────────
# 造临时库：schema 与生产库 factor_panel / stock_daily 完全一致
# ────────────────────────────────────────────────────────────
STOCK_DAILY_DDL = (
    "CREATE TABLE stock_daily (id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, "
    "date TEXT, open REAL, high REAL, low REAL, close REAL, volume REAL, "
    "turnover REAL, UNIQUE(symbol,date))"
)
FACTOR_PANEL_DDL = [
    "CREATE TABLE factor_panel (date TEXT, symbol TEXT, factor TEXT, value REAL)",
    "CREATE INDEX idx_factor_date ON factor_panel(factor, date)",
    "CREATE INDEX ix_factor_panel_new ON factor_panel(factor, date)",
    "CREATE UNIQUE INDEX ux_factor_panel_new ON factor_panel(date, symbol, factor)",
]
INSERT_PANEL = ("INSERT OR REPLACE INTO factor_panel(date, symbol, factor, value) "
                "VALUES(?,?,?,?)")

N_SYMBOLS = 200
N_DATES = 620
INIT_FROM, INIT_TO = 560, 580        # 初始面板覆盖 dates[560:580]
TARGET_FROM, TARGET_TO = 580, 620    # 本次增量目标 dates[580:620]


def gen_dates(n: int, start_year: int = 2023) -> list:
    """生成 n 个「工作日」（不含周末），足够当交易日历用。"""
    out, y, m, d = [], start_year, 1, 2
    while len(out) < n:
        dt = "%04d-%02d-%02d" % (y, m, d)
        # 2023-01-02 是周一；用序数判周末
        import datetime
        wd = datetime.date(y, m, d).weekday()
        if wd < 5:
            out.append(dt)
        d += 1
        if d > 28:
            d = 1
            m += 1
            if m > 12:
                m = 1
                y += 1
    return out


def make_db(path: str, n_symbols: int = N_SYMBOLS, n_dates: int = N_DATES,
            seed: int = 20261116) -> tuple:
    """建临时库并灌入 n_symbols 只 × n_dates 天的合成行情（几何随机游走）。"""
    rng = np.random.RandomState(seed)
    symbols = ["%06d" % (1 + i) for i in range(n_symbols)]
    dates = gen_dates(n_dates)

    conn = sqlite3.connect(path)
    conn.execute("PRAGMA journal_mode=WAL")     # 边写边读，多进程 worker 不会被写事务挡住
    conn.execute(STOCK_DAILY_DDL)
    conn.execute("CREATE INDEX idx_symbol_date ON stock_daily(symbol, date)")
    for sql in FACTOR_PANEL_DDL:
        conn.execute(sql)

    rows = []
    for s in symbols:
        close = 10.0
        for dt in dates:
            ret = float(rng.standard_normal() * 0.025)
            open_ = close * (1 + float(rng.standard_normal() * 0.005))
            close = max(0.5, close * (1 + ret))
            high = max(open_, close) * (1 + abs(float(rng.standard_normal()) * 0.008))
            low = min(open_, close) * (1 - abs(float(rng.standard_normal()) * 0.008))
            vol = float(abs(rng.standard_normal()) * 1e6 + 2e5)
            turnover = vol * close
            rows.append((s, dt, open_, high, low, close, vol, turnover))
    conn.executemany(
        "INSERT OR IGNORE INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) "
        "VALUES(?,?,?,?,?,?,?,?)", rows)
    conn.commit()
    return conn, symbols, dates


def seed_initial_panel(conn: sqlite3.Connection, symbols: list, dates: list,
                       frm: int, to: int) -> int:
    """用「全量重算」把 dates[frm:to] 的因子写进面板，当作「上次已经算好的状态」。"""
    factors = all_names()
    end = dates[to - 1]
    rows = []
    for s in symbols:
        df = FP.load_ohlcv(conn, s, end=end)
        series = compute_series(df, factors)
        keep = [d for d in dates[frm:to] if d in series.index]
        if not keep:
            continue
        sub = series.loc[keep]
        for date, values in zip(sub.index.tolist(), sub.to_numpy()):
            for i, f in enumerate(factors):
                v = values[i]
                if v is None or v != v:
                    continue
                rows.append((str(date), s, f, float(v)))
    conn.executemany(INSERT_PANEL, rows)
    conn.commit()
    return len(rows)


def index_signature(conn: sqlite3.Connection, table: str = "factor_panel") -> list:
    """(序号, 索引名, unique 标志, 列名列表) —— 用于证明索引定义没被动过。"""
    out = []
    for seq, name, unique, *_rest in conn.execute("PRAGMA index_list(%s)" % table):
        cols = [r[2] for r in conn.execute("PRAGMA index_info(%s)" % name)]
        out.append((seq, name, int(unique), tuple(cols)))
    return out


# ────────────────────────────────────────────────────────────
# A5：索引不变（先测，因为它是「改前」状态）
# ────────────────────────────────────────────────────────────
def case_a5_index_unchanged(conn: sqlite3.Connection) -> list:
    print("\n[A5] PRAGMA index_list(factor_panel) 名称与列序与改前完全一致")
    before = index_signature(conn)
    print("      改前：%s" % (before,))
    src = open(os.path.join(HERE, "panel_incr.py"), encoding="utf-8").read().upper()
    ok("panel_incr.py 里没有 CREATE INDEX", "CREATE INDEX" not in src)
    ok("panel_incr.py 里没有 DROP INDEX", "DROP INDEX" not in src)
    ok("panel_incr.py 里没有 ALTER TABLE", "ALTER TABLE" not in src)
    return before


# ────────────────────────────────────────────────────────────
# A1：增量 vs 独立全量重算
# ────────────────────────────────────────────────────────────
REQUIRED = ["mom120", "mom60", "dist_high250", "obv_slope20"]


def case_a1_equivalence(conn: sqlite3.Connection, symbols: list, dates: list,
                        target_dates: list, rtol: float = 1e-4) -> dict:
    print("\n[A1] 增量 vs 独立全量重算（随机 200 个三元组，相对误差 ≤ %.0e）" % rtol)
    factors = all_names()
    others = [f for f in factors if f not in REQUIRED]

    triples = []
    for f in REQUIRED:                       # 每个必需因子各 25 个
        for _ in range(25):
            triples.append((f, None))
    for f in others:                         # 其余 20 个因子各 5 个
        for _ in range(5):
            triples.append((f, None))
    rng = random.Random(20261116)
    triples = [(f, rng.choice(symbols), rng.choice(target_dates)) for f, _ in triples]
    print("      计划 %d 个三元组（4 个必需因子各 25 个 + 其余 20 因子各 5 个）"
          % len(triples))

    series_cache: dict = {}
    checked = 0
    worst = 0.0
    worst_at = None
    bad = []
    per_factor = {}
    skipped_missing = 0
    for factor, sym, date in triples:
        if sym not in series_cache:
            series_cache[sym] = compute_series(FP.load_ohlcv(conn, sym), factors)
        ref_series = series_cache[sym]
        if date not in ref_series.index:
            skipped_missing += 1
            continue
        ref = float(ref_series.loc[date, factor])
        if not np.isfinite(ref):
            skipped_missing += 1
            continue
        row = conn.execute(
            "SELECT value FROM factor_panel WHERE date=? AND symbol=? AND factor=?",
            (date, sym, factor)).fetchone()
        if row is None or row[0] is None:
            bad.append((factor, sym, date, "增量没写这一格", ref))
            continue
        got = float(row[0])
        err = abs(got - ref) / max(abs(ref), 1e-12)
        checked += 1
        stat = per_factor.setdefault(factor, [0, 0.0])
        stat[0] += 1
        stat[1] = max(stat[1], err)
        if err > worst:
            worst, worst_at = err, (factor, sym, date, ref, got)
        if err > rtol:
            bad.append((factor, sym, date, err, ref, got))

    print("      实比 %d 格（参考值为 NaN 或缺失而跳过 %d 格），最大相对误差 %.3e"
          % (checked, skipped_missing, worst))
    for f in REQUIRED:
        stat = per_factor.get(f)
        print("        %-14s 比对 %3d 格，最大相对误差 %s"
              % (f, stat[0] if stat else 0, ("%.3e" % stat[1]) if stat else "—"))
    ok("比对格子数 ≥ 150", checked >= 150, "%d 格" % checked)
    ok("最大相对误差 ≤ %.0e" % rtol, worst <= rtol,
       "%.3e @ %s" % (worst, worst_at))
    ok("没有不一致的三元组", not bad, "%d 个：%s" % (len(bad), bad[:3]))
    for f in REQUIRED:
        stat = per_factor.get(f)
        ok("强制覆盖 %s 且误差达标" % f,
           stat is not None and stat[0] > 0 and stat[1] <= rtol,
           "%d 格 最大 %.3e" % (stat[0], stat[1]) if stat else "0 格")
    return {"checked": checked, "worst": worst, "perFactor": per_factor}


def case_warmup_teeth(conn: sqlite3.Connection, symbols: list, dates: list,
                      target_dates: list) -> None:
    """反向对照：把预热窗口拿掉，长窗口因子会大面积消失（证明测试有牙齿）。

    直接对 `compute_series` 做对照，不掺 `build_incremental_rows` 里的兜底回退
    （那个回退本身是正确性保险，会把「没预热」悄悄救回来，反而看不出差别）：
      A. 全历史重算（参考）
      B. 从目标首日截断（无预热）→ mom120 / dist_high250 这类需要 141/250 根
         的因子会整列 NaN，可用格子数断崖式下跌
      C. 从 calc_start（前 300 个交易日）起截断 → 与 A 在目标区间上**逐位相同**
    """
    print("\n[对照] 无预热 vs 有预热（证明本测试能抓住「没做预热」的实现）")
    factors = all_names()
    sym = symbols[0]
    end = target_dates[-1]
    full = FP.load_ohlcv(conn, sym, end=end)
    pos = int(full.index[full["date"] == target_dates[0]][0])
    ref = compute_series(full, factors).loc[target_dates]

    trunc = full.iloc[pos:].reset_index(drop=True)          # 无预热
    tr = compute_series(trunc, factors).loc[target_dates]

    calc_start = PI.calc_window(conn, target_dates[0], end, PI.WARMUP_BARS)
    wpos = int(full.index[full["date"] == calc_start][0])
    warm = full.iloc[wpos:].reset_index(drop=True)          # 有预热
    wm = compute_series(warm, factors).loc[target_dates]

    cap = len(target_dates) * len(factors)
    print("      上限 %d 格（%d 日 × %d 因子）" % (cap, len(target_dates), len(factors)))
    print("      A 全历史重算     可用 %4d 格" % int(ref.notna().to_numpy().sum()))
    print("      B 无预热（%3d 根） 可用 %4d 格；mom120 非空 %d 格，dist_high250 非空 %d 格"
          % (len(trunc), int(tr.notna().to_numpy().sum()),
             int(tr["mom120"].notna().sum()), int(tr["dist_high250"].notna().sum())))
    # 截断起点不同 → 浮点累加顺序不同 → 末位可能有 1e-12 量级差异，
    # 所以这里用「相对误差 ≤1e-4」判定，而不是逐位相等（逐位相等是 A3 里
    # 「同一份字节原样拷贝」才成立的断言）。
    a_np, b_np = ref.to_numpy(), wm.to_numpy()
    diff = np.abs(a_np - b_np)
    denom = np.maximum(np.abs(a_np), 1e-12)
    rel = np.where(np.isfinite(diff) & np.isfinite(denom), diff / denom, 0.0)
    rel = np.nan_to_num(rel, nan=0.0, posinf=0.0, neginf=0.0)
    worst_rel = float(rel.max())
    print("      C 有预热（%3d 根） 可用 %4d 格；与 A 的最大相对误差 %.3e"
          % (len(warm), int(wm.notna().to_numpy().sum()), worst_rel))

    n_ref = int(ref.notna().to_numpy().sum())
    n_tr = int(tr.notna().to_numpy().sum())
    n_wm = int(wm.notna().to_numpy().sum())
    ok("无预热时长窗口因子整列丢失（mom120 / dist_high250）",
       int(tr["mom120"].notna().sum()) == 0 and int(tr["dist_high250"].notna().sum()) == 0)
    ok("无预热的可用格子数显著少于全量", n_tr < n_ref * 0.8,
       "%d vs %d" % (n_tr, n_ref))
    ok("有预热与全量重算一致（相对误差 ≤1e-4）", worst_rel <= 1e-4,
       "%d 格，最大相对误差 %.3e" % (n_wm, worst_rel))
    ok("有预热时长窗口因子全部有值",
       int(wm["mom120"].notna().sum()) == len(target_dates)
       and int(wm["dist_high250"].notna().sum()) == len(target_dates),
       "mom120 %d 格 / dist_high250 %d 格"
       % (int(wm["mom120"].notna().sum()), int(wm["dist_high250"].notna().sum())))


# ────────────────────────────────────────────────────────────
# A2 / A3：幂等 + 日期数
# ────────────────────────────────────────────────────────────
def case_a2_a3(conn: sqlite3.Connection, dates: list, target_dates: list,
               init_days: int) -> None:
    print("\n[A2] 连跑两次，factor_panel 行数前后相等（幂等）")
    total = conn.execute("SELECT COUNT(*) FROM factor_panel").fetchone()[0]
    in_target = PI.count_rows(conn, target_dates)
    print("      第 1 次之后：总行 %s，目标日期 %d 日共 %s 行"
          % (f"{total:,}", len(target_dates), f"{in_target:,}"))

    t0 = time.time()
    res2 = PI.run_incremental(conn=conn, target_dates=target_dates, workers=1,
                              progress=False)
    dt = time.time() - t0
    total2 = conn.execute("SELECT COUNT(*) FROM factor_panel").fetchone()[0]
    in_target2 = PI.count_rows(conn, target_dates)
    print("      第 2 次：写入 %s 行（覆盖写），%.1fs；总行 %s，目标日期共 %s 行"
          % (f"{res2.get('plainRows', 0):,}", dt, f"{total2:,}", f"{in_target2:,}"))
    ok("总行数不变", total2 == total, "%s → %s" % (total, total2))
    ok("目标日期行数不变", in_target2 == in_target, "%s → %s" % (in_target, in_target2))
    ok("第二次仍然写出同样多的行（说明是真覆盖不是跳过）",
       res2.get("plainRows", 0) == in_target,
       "%s vs %s" % (res2.get("plainRows"), in_target))

    print("\n[A3] COUNT(DISTINCT date) 增量结果 == 原有日期数 + 新增交易日数")
    n_distinct = conn.execute(
        "SELECT COUNT(DISTINCT date) FROM factor_panel").fetchone()[0]
    expect = init_days + len(target_dates)
    print("      distinct date = %d，期望 %d（原有 %d + 新增 %d）"
          % (n_distinct, expect, init_days, len(target_dates)))
    ok("distinct date 等于期望值", n_distinct == expect,
       "%d vs %d" % (n_distinct, expect))


# ────────────────────────────────────────────────────────────
# A4a：编排骨架（plan → compute → cache → ts → rolling → score → export → finish）
# ────────────────────────────────────────────────────────────
def _prod_stamp() -> dict:
    """生产 `data/` 下「pipeline 会写」的产物快照，用来证明测试没写生产。

    实现统一走 `tests/prod_guard.py`（四个测试共用一份清单与对比逻辑），
    避免各写各的导致清单漂移——「漏看一个文件」就等于这条断言失效。
    """
    return prod_guard.snapshot()


def _write_eval(path: str, names, dates) -> str:
    """写一份结构合法的 `factor_eval.json`（打分步骤的输入）。

    字段必须按真实 schema 嵌套：`rank_ic.{mean,t_nw}` + `monotonicity`。
    写成平铺的 `{"icMean":…, "tNw":…}` 时 `load_candidates()` 会一个都挑不出来，
    报的是「没有可用于合成的因子」——这种「看着像配置错、其实是 schema 错」
    的失败，在没有端到端测试时极难定位。
    """
    payload = {"meta": {"horizon": 5, "entry": "s1_open"}, "generatedAt":
               "2026-11-16 09:00", "factors": {}}
    for i, name in enumerate(names):
        payload["factors"][name] = {
            "rank_ic": {"mean": 0.05 - 0.001 * i, "t_nw": 4.0 - 0.1 * i,
                        "std": 0.02, "n_days": 200, "nw_lag": 4},
            "monotonicity": 0.8,
            "ic_series": [[str(d), 0.05] for d in list(dates)[-60:]],
        }
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    return path


def _isolate(tmp: str, tag: str) -> dict:
    """把一次 pipeline 运行要落盘的所有路径都指到临时目录。

    为什么要逐个指：`factor_ts` 走环境变量（`FACTOR_TS_DIR` /
    `PANEL_REBUILD_PATH` / `FACTOR_EVAL_PATH`），而 `factor_score` /
    `export_factor_api` / `rolling_eval` 走模块常量。漏指一个，
    这个测试就会在**生产 `data/` 里写一份合法的 `factor_score.json`** ——
    事后极难发现，因为写出来的文件本身完全正常，只是数据来自合成库。
    """
    import export_factor_api as EFA
    import factor_score as FS
    import panel_cache as PC
    import rolling_eval as RE

    base = os.path.join(tmp, "iso_" + tag)
    os.makedirs(base, exist_ok=True)
    paths = {
        "base": base,
        "run": os.path.join(base, "factor_run.json"),
        "lock": os.path.join(base, "factor_run.lock"),
        "ts": os.path.join(base, "factor_ts"),
        "rebuild": os.path.join(base, "panel_rebuild.json"),
        "eval": os.path.join(base, "factor_eval.json"),
        "rolling": os.path.join(base, "factor_rolling.json"),
        "score": os.path.join(base, "factor_score.json"),
        "export": os.path.join(base, "factor_api.json"),
        "seg": os.path.join(base, "factor_segments.json"),
        "cache": os.path.join(base, "panel_cache"),
    }
    os.makedirs(paths["cache"], exist_ok=True)
    saved = {
        "env": {k: os.environ.get(k) for k in
                ("FACTOR_RUN_PATH", "FACTOR_RUN_LOCK_PATH", "FACTOR_TS_DIR",
                 "PANEL_REBUILD_PATH", "FACTOR_EVAL_PATH")},
        "PC": PC.CACHE_DIR,
        "RE": RE.OUT_DEFAULT,
        "FS": (FS.EVAL, FS.OUT),
        "EFA": (EFA.EVAL, EFA.SEG, EFA.OUT),
    }
    os.environ["FACTOR_RUN_PATH"] = paths["run"]
    os.environ["FACTOR_RUN_LOCK_PATH"] = paths["lock"]
    os.environ["FACTOR_TS_DIR"] = paths["ts"]
    os.environ["PANEL_REBUILD_PATH"] = paths["rebuild"]
    os.environ["FACTOR_EVAL_PATH"] = paths["eval"]
    PC.CACHE_DIR = paths["cache"]
    PC.reset_memo()
    RE.OUT_DEFAULT = paths["rolling"]
    FS.EVAL, FS.OUT = paths["eval"], paths["score"]
    EFA.EVAL, EFA.SEG, EFA.OUT = paths["eval"], paths["seg"], paths["export"]
    return {"base": base, "paths": paths, "saved": saved}


def _restore(saved: dict) -> None:
    """还原 `_isolate()` 改过的一切（包括 `PC.reset_memo()`，否则下一个 case 读到错缓存）。"""
    import export_factor_api as EFA
    import factor_score as FS
    import panel_cache as PC
    import rolling_eval as RE

    for key, old in saved["env"].items():
        if old is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = old
    PC.CACHE_DIR = saved["PC"]
    PC.reset_memo()
    RE.OUT_DEFAULT = saved["RE"]
    FS.EVAL, FS.OUT = saved["FS"]
    EFA.EVAL, EFA.SEG, EFA.OUT = saved["EFA"]


# ────────────────────────────────────────────────────────────
# A4a：编排骨架（plan → compute → cache → ts → rolling → score → export → finish）
# ────────────────────────────────────────────────────────────
def case_a4_pipeline(tmp: str) -> None:
    print("\n[A4a] 编排骨架跑通 step1~9（含 finish），T03 四步产物全部落盘")
    import factor_ts as TS
    import panel_cache as PC
    import run_factor_pipeline as RP

    db = os.path.join(tmp, "pipe.db")
    conn, symbols, dates = make_db(db, n_symbols=40, n_dates=N_DATES, seed=7)
    iso = _isolate(tmp, "ok")
    paths = iso["paths"]
    prod_before = _prod_stamp()
    try:
        # 面板先算到 dates[0]（放 24 行做「已算到哪天」的标记）
        conn.executemany(INSERT_PANEL,
                         [(dates[0], symbols[0], f, 0.0) for f in all_names()])
        conn.commit()
        PC.reset_memo()
        # 一次性建全量缓存（模拟上线前手动补齐），轴 = 1 日 × 1 只
        PC.ensure(conn, all_names(), min_rows=0, verbose=False)
        _write_eval(paths["eval"], all_names(), dates)

        # 小样本库只有 40 只/日：残日门槛降到 10 只，逐日 IC 的最小样本也降到 10 只。
        # minStocks 不降的话 24 个因子逐日横截面全是 NaN → factor_ts 一个序列都建不出来，
        # 那测的就不是编排，而是 "小样本必然没 IC" 这个常识。
        old_min = RP.DEFAULTS["minSymbolsPerDay"]
        old_stk = RP.DEFAULTS["minStocks"]
        RP.DEFAULTS["minSymbolsPerDay"] = 10
        RP.DEFAULTS["minStocks"] = 10
        t0 = time.time()
        res = RP.run_pipeline(db=db, trigger="manual", workers=1, verbose=False)
        RP.DEFAULTS["minSymbolsPerDay"] = old_min
        RP.DEFAULTS["minStocks"] = old_stk
        dt = time.time() - t0
        run = FR.read()
        tsd = res.get("ts") or {}
        roll = res.get("rolling") or {}
        score = res.get("score") or {}
        exp = res.get("export") or {}
        ens = tsd.get("ensure") or {}
        axis = ens.get("axis") or {}
        print("      exit=%s  cost=%.1fs  文件里的 cost=%s"
              % (res["exit"], dt, run.get("cost")))
        print("      ts：ensure=%s，追加 %d 日 / %d 因子，skipped %d 日"
              "（面板轴 %s ~ %s，cutoff %s）"
              % (ens.get("action"), tsd.get("appended") or 0,
                 tsd.get("factors") or 0, len(tsd.get("skipped") or []),
                 axis.get("first"), axis.get("last"), axis.get("cutoff")))
        print("      rolling：%d 期，最新 asOf=%s cut=%s，入选 %d 个，%.1fs"
              % (roll.get("n_periods") or 0,
                 (roll.get("latest") or {}).get("asOf"),
                 (roll.get("latest") or {}).get("icCutoff"),
                 (roll.get("latest") or {}).get("n_selected") or 0,
                 roll.get("elapsed") or 0))
        print("      score：%s 个因子 / 推荐 %s 只 | export：dataThrough=%s"
              " evalAsOf=%s | price：loaded=%s cost=%.1fs"
              % (score.get("factors"), score.get("picks"),
                 exp.get("dataThrough"), exp.get("evalAsOf"),
                 (res.get("price") or {}).get("loaded"),
                 float((res.get("price") or {}).get("cost") or 0)))

        ok("pipeline 退出码为 0", res["exit"] == 0,
           "exit=%s error=%s" % (res["exit"], res.get("error")))
        ok("factor_run.json 顶层已结算（running 为空且 ok=true）",
           run.get("running") is None and run.get("ok") is True,
           "ok=%s running=%s" % (run.get("ok"), run.get("running")))
        ok("耗时写入 factor_run.cost", isinstance(run.get("cost"), (int, float))
           and run["cost"] > 0, "cost=%s 秒" % run.get("cost"))

        # ---- step5 ts：24 因子齐备 + ic/groups 同时落盘（底线 1）----
        ok("step5 ts 成功（24 个因子序列齐备）",
           tsd.get("ok") is True and (tsd.get("factors") or 0) == len(all_names()),
           "%s / %s 个" % (tsd.get("ok"), tsd.get("factors")))
        ok("ts 声明并实际持久化 ic 与 groups（缺 groups = 滚动验证静默失效）",
           tsd.get("willPersist") == ["ic", "groups"],
           str(tsd.get("willPersist")))
        bad = []
        for name in all_names():
            ser = TS._load_series(name)                       # noqa: SLF001
            if ser is None:
                bad.append(name + ":缺文件")
                continue
            if len(ser["dates"]) != len(ser["ic"]) or len(ser["ic"]) == 0:
                bad.append(name + ":长度不符")
            elif ser["groups"].shape != (len(ser["dates"]), 10):
                bad.append(name + ":%s" % (ser["groups"].shape,))
        ok("24 个 npz 都含 dates/ic/groups(10 组) 且长度自洽", not bad,
           str(bad[:4]))
        over = [n for n in all_names()
                if (TS.last_date(n) or "") > str(axis.get("cutoff") or "9999")]
        ok("没有序列越过 `panelMax - horizon`（约束 A 第一道防线）",
           not over and bool(axis.get("cutoff")), "越界 %s" % over[:4])
        skipped = list(tsd.get("skipped") or [])
        ok("ts.skipped 只表示「> panelMax-horizon 算不出 IC」的日期",
           len(skipped) == RP.DEFAULTS["horizon"]
           and all(str(d) > str(axis.get("cutoff")) for d in skipped),
           "%d 日：%s" % (len(skipped), skipped))
        ok("首次 ensure 已建到 cutoff，故本次 append 追加 0 日（不是「跳过」）",
           (tsd.get("appended") or 0) == 0
           and not (tsd.get("failed") or []),
           "appended=%s failed=%s" % (tsd.get("appended"),
                                      tsd.get("failed")))

        # ---- step6 rolling：纯切片，不二次读价格 ----
        ok("step6 rolling 成功且至少 1 期",
           roll.get("ok") is True and (roll.get("n_periods") or 0) >= 1,
           "ok=%s n_periods=%s" % (roll.get("ok"), roll.get("n_periods")))
        ok("rolling.latest.icCutoff 与 ts.axis.cutoff 完全相同（同一规则两处实现）",
           (roll.get("latest") or {}).get("icCutoff") == axis.get("cutoff"),
           "%s vs %s" % ((roll.get("latest") or {}).get("icCutoff"),
                         axis.get("cutoff")))
        ok("rolling 的滞后探针通过（约束 A 第三道防线）",
           (roll.get("lag") or {}).get("ok") is True
           and not (roll.get("lag") or {}).get("violations"),
           str((roll.get("lag") or {}).get("violations"))[:80])
        ok("rolling 不访问价格宽表（价格那 54.7s 只由 step5 付一次）",
           roll.get("willUsePrice") is False, str(roll.get("willUsePrice")))

        # ---- step7/8：产物写在哪、写了什么 ----
        ok("step7 score 成功", score.get("ok") is True,
           "%s" % (score.get("reason") or "factors=%s picks=%s"
                   % (score.get("factors"), score.get("picks"))))
        ok("step8 export 成功", exp.get("ok") is True,
           str(exp.get("reason") or "factors=%s" % exp.get("factors")))
        for key in ("rolling", "score", "export"):
            ok("产物落在临时目录：%s" % os.path.basename(paths[key]),
               os.path.isfile(paths[key]), paths[key])
        contract = {}
        if os.path.isfile(paths["export"]):
            with open(paths["export"], encoding="utf-8") as fh:
                contract = json.load(fh)
        need = ("evalAsOf", "dataThrough", "lastCalcAt", "lastCalcOk",
                "lastCalcError", "nextRunAt", "freshness", "rolling")
        ok("factor_api.json 契约字段齐全（页面不自己算日期）",
           all(k in contract for k in need),
           "缺 %s" % [k for k in need if k not in contract])
        ok("契约里的 dataThrough 等于库里的全表 MAX(date)",
           contract.get("dataThrough") == max(dates),
           "%s vs %s" % (contract.get("dataThrough"), max(dates)))
        after = _prod_stamp()
        ok("生产 data/ 的产物一个字节都没被写（隔离有效）",
           after == prod_before,
           "%s" % [k for k in after if after[k] != prod_before.get(k)])

        # ---- R1：价格宽表「全流程只加载一次」的接口验证 ----
        pr = res.get("price") or {}
        ok("step5 真的加载了一次价格宽表（T03 填实后必须为 true）",
           pr.get("loaded") is True and float(pr.get("cost") or 0) < 5.0,
           "loaded=%s cost=%s" % (pr.get("loaded"), pr.get("cost")))
        ok("verify 里记录了 price / ts / rolling / export 四段",
           all(isinstance((run.get("verify") or {}).get(k), dict)
               for k in ("price", "ts", "rolling", "export")),
           json.dumps({k: type((run.get("verify") or {}).get(k)).__name__
                       for k in ("price", "ts", "rolling", "export")},
                      ensure_ascii=False))
        ok("verify.ts 同时记了 appended 与 skipped（两个语义不能混用）",
           isinstance((run.get("verify") or {}).get("ts", {}).get("appended"), int)
           and isinstance((run.get("verify") or {}).get("ts", {}).get("skipped"), list),
           json.dumps((run.get("verify") or {}).get("ts"), ensure_ascii=False)[:110])

        # 真加载一次，证明 PriceBundle 是单例：两次 get() 返回同一对象、只付一次时间
        b = RP.PriceBundle(conn, RP.DEFAULTS)
        t0 = time.time()
        d1 = b.get()
        cost1 = b.cost
        d2 = b.get()
        cost2 = b.cost
        wall = time.time() - t0
        print("      PriceBundle：close %s / open %s / fwd %s；"
              "首次 %.2fs，两次 get() 合计墙钟 %.2fs"
              % (d1["close"].shape, d1["open"].shape, d1["fwd"].shape, cost1, wall))
        ok("PriceBundle 两次 get() 返回同一份对象", d1 is d2)
        ok("price 只加载一次（第二次不再计时）", cost2 == cost1 and b.loaded,
           "cost1=%.2f cost2=%.2f" % (cost1, cost2))
        ok("fwd / close / open 形状一致",
           d1["fwd"].shape == d1["close"].shape == d1["open"].shape,
           str(d1["fwd"].shape))
        ok("entry / horizon 取自 config",
           d1["horizon"] == RP.DEFAULTS["horizon"]
           and d1["entry"] == RP.DEFAULTS["entry"],
           "h=%s entry=%s" % (d1["horizon"], d1["entry"]))
    finally:
        _restore(iso["saved"])
        conn.close()


# ────────────────────────────────────────────────────────────
# A4b：覆盖不全必须吐退出码 2（底线 3），且不许留下半份产物
# ────────────────────────────────────────────────────────────
def case_a4_failfast(tmp: str) -> None:
    print("\n[A4b] 缓存覆盖不全 → 立即 exit 2，且不写 score / export 产物")
    import panel_cache as PC
    import run_factor_pipeline as RP

    db = os.path.join(tmp, "pipe_gap.db")
    conn, symbols, dates = make_db(db, n_symbols=40, n_dates=N_DATES, seed=11)
    iso = _isolate(tmp, "gap")
    paths = iso["paths"]
    try:
        conn.executemany(INSERT_PANEL,
                         [(dates[0], symbols[0], f, 0.0) for f in all_names()])
        conn.commit()
        PC.reset_memo()
        PC.ensure(conn, all_names(), min_rows=0, verbose=False)
        _write_eval(paths["eval"], all_names(), dates)

        # 故意**不**降 minStocks：40 只/日 < 100 → 24 个因子逐日横截面全为 NaN
        old_min = RP.DEFAULTS["minSymbolsPerDay"]
        RP.DEFAULTS["minSymbolsPerDay"] = 10
        t0 = time.time()
        res = RP.run_pipeline(db=db, trigger="manual", workers=1, verbose=False)
        RP.DEFAULTS["minSymbolsPerDay"] = old_min
        dt = time.time() - t0
        run = FR.read()
        tsd = res.get("ts") or {}
        print("      exit=%s（%.1fs）reason=%s"
              % (res["exit"], dt, (res.get("error") or "")[:70]))
        ok("覆盖不全时退出码为 2（不是 0、不是 1）", res["exit"] == 2,
           "exit=%s" % res["exit"])
        msg = str(res.get("error") or "")
        ok("错误信息指出真正的病因（可用序列 0 个 / minStocks），不报成行情缺失",
           ("没有任何可用序列" in msg or "一个都建不出来" in msg
            or "面板无数据" in msg or "ensure" in msg)
           and "stock_daily" not in msg and "行情缺失" not in msg,
           msg[:80])
        ok("ts 之后的三步根本没执行（不产出半成品）",
           res.get("rolling") is None and res.get("score") is None
           and res.get("export") is None,
           "rolling=%s score=%s export=%s"
           % (res.get("rolling"), res.get("score"), res.get("export")))
        ok("临时目录里没有 score / export / rolling 产物",
           not any(os.path.exists(paths[k]) for k in ("score", "export",
                                                      "rolling")),
           "%s" % [k for k in ("score", "export", "rolling")
                   if os.path.exists(paths[k])])
        ok("失败也把 factor_run 结算掉（lastCalcOk=false 才有页面提示）",
           run.get("running") is None and run.get("ok") is False
           and bool(run.get("error")),
           "ok=%s error=%s" % (run.get("ok"), str(run.get("error"))[:60]))
        ok("ts 步骤自己声明了退出码（不乱猜）", int(tsd.get("exit") or 0) == 2,
           "ts.exit=%s" % tsd.get("exit"))
    finally:
        _restore(iso["saved"])
        conn.close()


# ────────────────────────────────────────────────────────────
# A4c：滚动「本次无有效期」→ partial，不是失败
# ────────────────────────────────────────────────────────────
def case_a4_partial(tmp: str) -> None:
    print("\n[A4c] 滚动无有效期 → partial（不亮红灯），产物照写，freshness 仍是 fresh")
    import panel_cache as PC
    import run_factor_pipeline as RP

    db = os.path.join(tmp, "pipe_partial.db")
    conn, symbols, dates = make_db(db, n_symbols=40, n_dates=N_DATES, seed=13)
    iso = _isolate(tmp, "partial")
    paths = iso["paths"]
    try:
        conn.executemany(INSERT_PANEL,
                         [(dates[0], symbols[0], f, 0.0) for f in all_names()])
        conn.commit()
        PC.reset_memo()
        PC.ensure(conn, all_names(), min_rows=0, verbose=False)
        _write_eval(paths["eval"], all_names(), dates)

        old = {k: RP.DEFAULTS[k] for k in
               ("minSymbolsPerDay", "minStocks", "maxDays")}
        RP.DEFAULTS["minSymbolsPerDay"] = 10
        RP.DEFAULTS["minStocks"] = 10
        # 关键：只留 24 个目标日 → 面板共 25 个日期 → `period_indices(25,5,20)`
        # 的 first=25 > last=24 → 返回 []。「本次无有效期」是**真算出来**的，
        # 不是打桩，所以下游每一步走的都是真实代码路径。
        RP.DEFAULTS["maxDays"] = 24
        t0 = time.time()
        res = RP.run_pipeline(db=db, trigger="manual", workers=1, verbose=False)
        dt = time.time() - t0
        for key, val in old.items():
            RP.DEFAULTS[key] = val

        run = FR.read()
        roll = res.get("rolling") or {}
        score = res.get("score") or {}
        exp = res.get("export") or {}
        print("      exit=%s（%.1fs）partial=%s warnings=%s"
              % (res["exit"], dt, run.get("partial"), run.get("warnings")))
        print("      rolling：no_period=%s n_periods=%s reason=%s"
              % (roll.get("no_period"), roll.get("n_periods"),
                 (roll.get("reason") or "")[:46]))
        print("      score：ok=%s 因子 %s 推荐 %s | export：ok=%s dataThrough=%s"
              % (score.get("ok"), score.get("factors"), score.get("picks"),
                 exp.get("ok"), exp.get("dataThrough")))

        ok("滚动无有效期时退出码仍是 0（不判失败）", res["exit"] == 0,
           "exit=%s error=%s" % (res["exit"], res.get("error")))
        ok("factor_run 结算为 ok=true 且 running 为空",
           run.get("ok") is True and run.get("running") is None,
           "ok=%s running=%s" % (run.get("ok"), run.get("running")))
        ok("factor_run.partial=true（页面据此不亮红灯）",
           run.get("partial") is True, "partial=%s" % run.get("partial"))
        ok("warnings 里有一条明确写到 rolling",
           any("rolling" in str(w) for w in (run.get("warnings") or [])),
           str(run.get("warnings"))[:100])
        ok("rolling 段如实报告「无有效期」（ok=false / n_periods=0）",
           roll.get("ok") is False and roll.get("no_period") is True
           and int(roll.get("n_periods") or 0) == 0
           and bool(roll.get("reason")),
           "ok=%s no_period=%s n=%s" % (roll.get("ok"), roll.get("no_period"),
                                        roll.get("n_periods")))
        fresh_files = []
        for key in ("score", "export"):
            path = paths[key]
            fresh_files.append(os.path.isfile(path)
                               and os.path.getmtime(path) >= t0 - 1)
        ok("产物仍然写出去了（score / export 都更新）", all(fresh_files),
           "%s" % ["%s=%s" % (k, os.path.isfile(paths[k]))
                   for k in ("score", "export")])
        contract = {}
        if os.path.isfile(paths["export"]):
            with open(paths["export"], encoding="utf-8") as fh:
                contract = json.load(fh)
        fresh = contract.get("freshness") or {}
        ok("页面状态是 fresh 而不是 failed（产物确实是新的）",
           fresh.get("status") == "fresh",
           "status=%s label=%s" % (fresh.get("status"), fresh.get("statusLabel")))
        ok("freshness 把人话标签给成「数据已更新」",
           fresh.get("statusLabel") == "数据已更新",
           str(fresh.get("statusLabel")))
        ok("partial 一路透到导出文件（页面拿它写灰字）",
           contract.get("partial") is True
           and bool(contract.get("warnings")) and fresh.get("partial") is True,
           "partial=%s warnings=%s" % (contract.get("partial"),
                                       str(contract.get("warnings"))[:60]))
        ok("score 产物里也带 partial（两个页面口径一致）",
           os.path.isfile(paths["score"])
           and json.load(open(paths["score"], encoding="utf-8")).get("partial") is True,
           "score.partial")
    finally:
        _restore(iso["saved"])
        conn.close()


# ────────────────────────────────────────────────────────────
# A4b：生产规模实测（真实 K 线 × 全部 5030 只）
# ────────────────────────────────────────────────────────────
def case_a4_prod(tmp: str) -> None:
    print("\n[A4b] 生产规模实测（真实 K 线，全部标的 × 24 因子 × 5 个交易日）")
    if os.environ.get("PANEL_INCR_SKIP_PROD"):
        print("      （PANEL_INCR_SKIP_PROD=1，跳过）")
        return
    if not os.path.isfile(PROD_DB):
        print("      （生产库不存在，跳过）")
        return

    src = sqlite3.connect("file:%s?mode=ro" % PROD_DB.replace("\\", "/"),
                          uri=True, timeout=180)
    try:
        all_syms = [r[0] for r in src.execute(
            "SELECT DISTINCT symbol FROM stock_daily")]
        cal = [r[0] for r in src.execute(
            "SELECT DISTINCT date FROM stock_daily ORDER BY date ASC")]
        w0 = cal[max(0, len(cal) - 340)]
        n_stock = src.execute(
            "SELECT COUNT(*) FROM stock_daily WHERE date >= ?", (w0,)).fetchone()[0]
        print("      复制近 %d 个交易日 × %d 只 = %s 行 K 线到临时库…"
              % (len(cal) - cal.index(w0), len(all_syms), f"{n_stock:,}"))

        db = os.path.join(tmp, "prodscale.db")
        dst = sqlite3.connect(db)
        dst.execute("PRAGMA journal_mode=WAL")   # 8 个 worker 同时读，必须 WAL
        try:
            # 大批量灌数据时先不要 UNIQUE（逐行维护索引会慢好几倍），灌完再建
            dst.execute(
                "CREATE TABLE stock_daily (symbol TEXT, date TEXT, open REAL, "
                "high REAL, low REAL, close REAL, volume REAL, turnover REAL)")
            for sql in FACTOR_PANEL_DDL:
                dst.execute(sql)
            dst.commit()
            # 批量复制（用 fetchmany 分块，避免几百万行一次性进内存）
            t0 = time.time()
            cur = src.execute(
                "SELECT symbol,date,open,high,low,close,volume,turnover "
                "FROM stock_daily WHERE date >= ?", (w0,))
            copied = 0
            while True:
                chunk = cur.fetchmany(200_000)
                if not chunk:
                    break
                dst.executemany(
                    "INSERT INTO stock_daily"
                    "(symbol,date,open,high,low,close,volume,turnover) "
                    "VALUES(?,?,?,?,?,?,?,?)", chunk)
                copied += len(chunk)
            dst.commit()
            t_idx = time.time()
            dst.execute("CREATE UNIQUE INDEX ux_stock_daily ON stock_daily(symbol,date)")
            dst.commit()
            print("      复制完成 %s 行，%.1fs（含建索引 %.1fs）"
                  % (f"{copied:,}", time.time() - t0, time.time() - t_idx))

            # 标记面板「已算到倒数第 6 个交易日」，让 plan 只挑最后 5 天
            marker = cal[-6]
            dst.executemany(INSERT_PANEL,
                            [(marker, "999999", f, 0.0) for f in all_names()])
            dst.commit()

            pl = PI.plan(dst)
            print("      plan：面板到 %s，行情到 %s，缺口 %d 日 → 目标 %s"
                  % (pl["dataThrough"], pl["expectedThrough"], pl["lagDays"],
                     pl["targetDates"]))
            if len(pl["targetDates"]) != 5:
                print("      ！plan 目标数不是 5（%d），实测按实际目标数继续"
                      % len(pl["targetDates"]))

            run_path = os.path.join(tmp, "factor_run_prodscale.json")
            os.environ["FACTOR_RUN_PATH"] = run_path
            FR.begin("PRODSCALE", pl["targetDates"], "manual")
            t0 = time.time()
            res = PI.run_incremental(conn=dst, target_dates=pl["targetDates"],
                                     workers=8, progress=True)
            cost = time.time() - t0
            FR.finish(bool(res.get("ok")), cost=cost,
                      verify={"rows": res.get("plainRows"),
                              "dates": len(res.get("targetDates") or []),
                              "symbols": res.get("symbolsDone")})
            run = FR.read()
            print("      实测：%d 只 × %d 因子 × %d 日 → 写入 %s 行，%.1fs（%.2f 分钟）"
                  % (res.get("symbolsDone", 0), len(res.get("factors") or []),
                     len(res.get("targetDates") or []),
                     f"{res.get('plainRows', 0):,}", cost, cost / 60))
            print("      factor_run.json：ok=%s cost=%s"
                  % (run.get("ok"), run.get("cost")))
            ok("生产规模单次 ≤ 10 分钟", cost <= 600, "%.2f 分钟" % (cost / 60))
            ok("耗时已写入 factor_run.cost", run.get("cost") and run["cost"] > 0,
               "cost=%s 秒" % run.get("cost"))
            ok("生产规模下写入行数 > 0", res.get("plainRows", 0) > 0,
               "%s 行" % f"{res.get('plainRows', 0):,}")
            # 抽查一格：与独立全量重算比对（生产规模下也做一次正确性抽检）
            factor = "obv_slope20"
            sym = all_syms[0]
            date = pl["targetDates"][-1]
            full = compute_series(FP.load_ohlcv(dst, sym))
            ref = float(full.loc[date, factor])
            got = float(dst.execute(
                "SELECT value FROM factor_panel WHERE date=? AND symbol=? AND factor=?",
                (date, sym, factor)).fetchone()[0])
            err = abs(got - ref) / max(abs(ref), 1e-12)
            print("      抽检 %s/%s/%s：库 %.10g vs 全量重算 %.10g，相对误差 %.3e"
                  % (sym, factor, date, got, ref, err))
            ok("生产规模下 obv_slope20 抽检一致（≤1e-4）", err <= 1e-4, "%.3e" % err)
        finally:
            dst.close()
    finally:
        src.close()


# ────────────────────────────────────────────────────────────
def main() -> int:
    global PASS, FAIL
    PASS = FAIL = 0
    FAILS.clear()
    tmp = tempfile.mkdtemp(prefix="panel_incr_t02_")
    print("=" * 74)
    print("T02 增量面板验收（临时目录 %s）" % tmp)
    print("=" * 74)
    before_all = prod_guard.snapshot()
    try:
        db = os.path.join(tmp, "t02.db")
        conn, symbols, dates = make_db(db)
        print("\n[建库] %d 只 × %d 天（合成几何随机游走，seed 固定）"
              % (len(symbols), len(dates)))

        idx_before = case_a5_index_unchanged(conn)

        t0 = time.time()
        n0 = seed_initial_panel(conn, symbols, dates, INIT_FROM, INIT_TO)
        print("[初始面板] dates[%d:%d] 共 %d 日 → %s 行，%.1fs"
              % (INIT_FROM, INIT_TO, INIT_TO - INIT_FROM, f"{n0:,}", time.time() - t0))
        data_through = PI.data_through(conn)
        print("[面板已算到] %s（逐因子 MAX(date) 的最小值）" % data_through)

        target_dates = dates[TARGET_FROM:TARGET_TO]
        print("[目标] dates[%d:%d] 共 %d 日：%s … %s"
              % (TARGET_FROM, TARGET_TO, len(target_dates),
                 target_dates[0], target_dates[-1]))

        # ---- plan() 的边界：残日与 MAX_DAYS ----
        pl = PI.plan(conn, min_symbols=PI.MIN_SYMBOLS_PER_DAY)
        ok("残日剔除生效（%d 只 < %d 只 → 全部判残日）"
           % (len(symbols), PI.MIN_SYMBOLS_PER_DAY),
           pl["targetDates"] == [] and len(pl["residualDates"]) == len(target_dates),
           "target=%d residual=%d" % (len(pl["targetDates"]),
                                      len(pl["residualDates"])))
        pl2 = PI.plan(conn, min_symbols=100, max_days=10)
        ok("MAX_DAYS 上限生效（缺口 %d 日，上限 10 日）" % pl2["lagDays"],
           len(pl2["targetDates"]) == 10
           and len(pl2["skippedByMaxDays"]) == len(target_dates) - 10,
           "target=%d skipped=%d" % (len(pl2["targetDates"]),
                                     len(pl2["skippedByMaxDays"])))

        # ---- 真跑增量 ----
        t0 = time.time()
        res = PI.run_incremental(conn=conn, target_dates=target_dates,
                                 workers=4, progress=True)
        print("[增量] %s 行，%d 只，%.1fs%s"
              % (f"{res['plainRows']:,}", res["symbolsDone"], time.time() - t0,
                 "；残日 %s" % res["residualDates"] if res["residualDates"] else ""))
        ok("增量计算成功", res["ok"] is True, str(res.get("reason")))
        ok("使用预热窗口（calcStart 早于目标首日）",
           str(res.get("calcStart")) < target_dates[0],
           "calcStart=%s target=%s" % (res.get("calcStart"), target_dates[0]))

        case_a1_equivalence(conn, symbols, dates, target_dates)
        case_warmup_teeth(conn, symbols, dates, target_dates)
        case_a2_a3(conn, dates, target_dates, INIT_TO - INIT_FROM)

        idx_after = index_signature(conn)
        ok("索引定义与改前完全一致", idx_after == idx_before,
           "%s vs %s" % (idx_after, idx_before))
        got = {n: (u, c) for _s, n, u, c in idx_after}
        want = {"idx_factor_date": (0, ("factor", "date")),
                "ix_factor_panel_new": (0, ("factor", "date")),
                "ux_factor_panel_new": (1, ("date", "symbol", "factor"))}
        ok("三个索引的名称 / unique 标志 / 列序完全正确", got == want,
           "%s vs %s" % (got, want))
        if os.path.isfile(PROD_DB):
            pc = sqlite3.connect("file:%s?mode=ro" % PROD_DB.replace("\\", "/"),
                                 uri=True, timeout=60)
            try:
                prod_idx = index_signature(pc)
            finally:
                pc.close()
            ok("与生产库 factor_panel 的索引定义逐项相同",
               sorted((n, u, c) for _s, n, u, c in prod_idx)
               == sorted((n, u, c) for _s, n, u, c in idx_after),
               "生产 %s / 临时 %s"
               % ([(n, c) for _s, n, _u, c in prod_idx],
                  [(n, c) for _s, n, _u, c in idx_after]))
        conn.close()

        case_a4_pipeline(tmp)
        case_a4_failfast(tmp)
        case_a4_partial(tmp)
        case_a4_prod(tmp)
    finally:
        os.environ.pop("FACTOR_RUN_PATH", None)
        os.environ.pop("FACTOR_RUN_LOCK_PATH", None)
        shutil.rmtree(tmp, ignore_errors=True)

    ok("全程没有写过生产 data/（隔离护栏，覆盖 A1~A4c 所有用例）",
       not prod_guard.diff(before_all), str(prod_guard.diff(before_all)[:6]))

    print("\n========================================")
    print("RESULT: pass=%d fail=%d" % (PASS, FAIL))
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
    print("========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
