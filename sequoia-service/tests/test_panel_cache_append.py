#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_panel_cache_append.py — T01 缓存层改造的验收测试

覆盖 4 条验收（全部用**临时缓存目录 + 临时库**，不碰生产 `data/panel_cache`）：

  1. 人为反转 `meta.dates` → `verify().ok == False` 且 `mismatch > 0`
  2. 注入「只写了 12 个 npy 就中断」→ 旧 `meta.json` 与旧 npy 全部可 `np.load`，
     `load()` 仍返回**旧轴**数据
  3. 追加后同一 `(factor, symbol)` 的行号列号不变：逐因子
     `np.array_equal(new[:R,:C], old)`
  4. `load()` 单次耗时 < 0.5 秒

实测耗时（本机，2026-11-16，生产库 data/sequoia_v2.db、1420 天 × 5030 只、
1.536 亿行）
──────────────────────────────────────────────────────────────────────
改前（旧 `panel_cache.load`，每次读都跑一次全表 fingerprint）:
    fingerprint()                 35.31 s   （`COUNT(*)+MIN+MAX+COUNT(DISTINCT factor)`
                                            一条 SQL；架构师另测冷页缓存 66 s）
    load() 第 1 次               16.20 s   （页缓存热；冷态 ≈ 35~66 s）
    24 个因子全读一遍             ≈ 6.5 分钟 ~ 26 分钟
改后（`structure_sig` 只算一次 + `load()` 无任何全表聚合）:
    structure_sig() 第 1 次       7.44 s   （MIN(date) 0.0001 s + DISTINCT factor 7.4 s）
    structure_sig() 第 2 次起     0.0000 s  （进程内 memo）
    load()                        0.043 s  ← 见下面 A4 的实测断言
    24 个因子全读一遍            ≈ 1.0 秒
单项提速 ≈ 370 倍（16.20 s → 0.043 s），这才是「滚动验证 ≤5 分钟」的前提。

运行：
    python tests/test_panel_cache_append.py            # 含生产库 load 性能实测（只读）
    PANEL_CACHE_SKIP_PROD=1 python tests/test_panel_cache_append.py   # 跳过生产库实测
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import panel_cache as PC                          # noqa: E402
from factors import registry                      # noqa: E402

PROD_DB = os.path.join(HERE, "data", "sequoia_v2.db")
PROD_CACHE = os.path.join(HERE, "data", "panel_cache")

PASS = 0
FAIL = 0
FAILS: list[str] = []


def ok(name: str, cond: bool, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  [PASS] %s%s" % (name, ("  " + extra) if extra else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  [FAIL] %s%s" % (name, ("  " + extra) if extra else ""))


# ────────────────────────────────────────────────────────────
# 造一个临时库（schema 与生产库 factor_panel 完全一致）
# ────────────────────────────────────────────────────────────
DDL = [
    # 生产库的 factor_panel 没有内联主键，靠唯一索引 ux_factor_panel_new 去重
    'CREATE TABLE factor_panel (date TEXT, symbol TEXT, factor TEXT, value REAL)',
    'CREATE INDEX idx_factor_date ON factor_panel(factor, date)',
    'CREATE INDEX ix_factor_panel_new ON factor_panel(factor, date)',
    'CREATE UNIQUE INDEX ux_factor_panel_new ON factor_panel(date, symbol, factor)',
]

N_SYMBOLS = 60
N_DATES = 120
N_NEW_DATES = 3
N_NEW_SYMBOLS = 2
FACTORS = registry.all_names()          # 24 个，与生产一致


def make_db(path: str, seed: int = 20261116) -> tuple:
    """建临时库并灌入 60 只 × 120 天 × 24 因子（约 80% 有值）。"""
    rng = np.random.RandomState(seed)
    symbols = ["%06d" % (1 + i) for i in range(N_SYMBOLS)]
    dates = ["2025-%02d-%02d" % (1 + (i // 28) % 12, 1 + i % 28) for i in range(N_DATES)]
    dates = sorted(set(dates))
    dates = dates[:N_DATES]

    conn = sqlite3.connect(path)
    for sql in DDL:
        conn.execute(sql)

    rows = []
    for d in dates:
        for s in symbols:
            if rng.rand() < 0.20:          # 20% 缺格：模拟停牌/上市不足
                continue
            for f in FACTORS:
                rows.append((d, s, f, float(rng.standard_normal() * 0.1)))
    conn.executemany("INSERT OR REPLACE INTO factor_panel VALUES(?,?,?,?)", rows)
    conn.commit()
    return conn, dates, symbols, rng


def add_tail(conn: sqlite3.Connection, last_date: str, n_dates: int,
             symbols: list, new_symbols: list, rng: np.random.RandomState) -> list:
    """在库尾追加 n_dates 个交易日（并给新股补一段轴内旧日期的值）。"""
    new_dates = []
    y, m, d = int(last_date[:4]), int(last_date[5:7]), int(last_date[8:10])
    for _ in range(n_dates):
        d += 1
        if d > 28:
            d = 1
            m += 1
            if m > 12:
                m = 1
                y += 1
        new_dates.append("%04d-%02d-%02d" % (y, m, d))

    rows = []
    for dt in new_dates:
        for s in symbols + new_symbols:
            for f in FACTORS:
                rows.append((dt, s, f, float(rng.standard_normal() * 0.1)))
    # 新股的历史（IPO 前没有数据，从新日期开始；再补一段「轴内旧日期」验证新列回填）
    for s in new_symbols:
        for dt in ("2025-01-05", "2025-01-06"):
            for f in FACTORS:
                rows.append((dt, s, f, float(rng.standard_normal() * 0.1)))
    conn.executemany("INSERT OR REPLACE INTO factor_panel VALUES(?,?,?,?)", rows)
    conn.commit()
    return new_dates


def snapshot(meta: dict) -> dict:
    """把当前 meta 登记的全部 npy 读进内存（用于「行号不变」与「中断后仍可用」断言）。"""
    snap = {}
    for name, entry in (meta.get("factors") or {}).items():
        path = os.path.join(PC.CACHE_DIR, name + ".npy")
        arr = np.load(path)
        assert arr.shape == (entry["rows"], entry["cols"]), (name, arr.shape)
        snap[name] = arr
    return snap


# ────────────────────────────────────────────────────────────
# A0：旧 fingerprint → 新 structure_sig 的迁移（不得作废已有缓存）
# ────────────────────────────────────────────────────────────
def case_a0_legacy_migration(conn: sqlite3.Connection) -> None:
    """旧格式 meta.json 必须被「就地迁移」，而不是被判定结构变化后重建。

    这是 2026-11-16 上线的真实风险点：生产 data/panel_cache 里已有 7 个因子
    缓存（229MB），meta 里存的是旧指纹 `rows=..|min=..|max=..|nf=..`。若
    ensure() 简单地把「sig 不一致」当成「结构已变 → 清空」，这 7 个缓存会
    集体作废，重建 84 分钟，期间页面全空。所以本用例把它钉死。
    """
    print("\n[A0] 旧 fingerprint → 新 structure_sig 迁移（不得重建已有缓存）")
    good = PC._load_meta()
    legacy = json.loads(json.dumps(good))
    legacy.pop("sig", None)
    legacy.pop("migratedFrom", None)
    legacy["fingerprint"] = "rows=999999|min=%s|max=%s|nf=%d" % (
        legacy["dates"][0], legacy["dates"][-1], len(FACTORS))
    PC._save_meta(legacy)
    PC.reset_memo()

    t0 = time.time()
    res = PC.ensure(conn, FACTORS, min_rows=0, verbose=False)
    dt = time.time() - t0
    meta_after = PC._load_meta()
    print("      旧指纹 %r → 迁移后 sig=%r，built=%d，cached=%d（%.2fs）"
          % (legacy["fingerprint"], meta_after.get("sig"), len(res["built"]),
             len(res["cached"]), dt))
    ok("旧指纹被迁移成结构签名", bool(meta_after.get("sig")), str(meta_after.get("sig")))
    ok("迁移时不重建任何缓存", res["built"] == [], "built=%d" % len(res["built"]))
    ok("原有 %d 个因子仍被认定为已缓存" % len(FACTORS),
       sorted(res["cached"]) == sorted(FACTORS), "%d 个" % len(res["cached"]))
    ok("轴未被改动",
       meta_after["dates"] == legacy["dates"]
       and meta_after["symbols"] == legacy["symbols"])

    # 反例：旧指纹的 min 与库不一致 → 必须判为结构已变（不能瞎迁移）
    bad = json.loads(json.dumps(legacy))
    bad.pop("sig", None)
    bad.pop("migratedFrom", None)
    bad["fingerprint"] = "rows=1|min=1999-01-01|max=x|nf=%d" % len(FACTORS)
    PC._save_meta(bad)
    PC.reset_memo()
    res2 = PC.ensure(conn, FACTORS, min_rows=0, verbose=False)
    ok("旧指纹 min 与库不符时不迁移，按结构变化重建",
       res2["built"] == list(FACTORS) and not PC._load_meta().get("migratedFrom"),
       "built=%d" % len(res2["built"]))
    PC._save_meta(good)               # 恢复 A1 需要的干净状态
    PC.reset_memo()


# ────────────────────────────────────────────────────────────
# A1：人为反转 meta.dates → verify 必须报错
# ────────────────────────────────────────────────────────────
def case_a1_reversed_axis(conn: sqlite3.Connection) -> None:
    print("\n[A1] 人为反转 meta.dates → verify.ok == False 且 mismatch > 0")
    good = PC._load_meta()
    bad = json.loads(json.dumps(good))
    bad["dates"] = list(reversed(bad["dates"]))
    PC._save_meta(bad)
    t0 = time.time()
    res = PC.verify(conn, mode="full")
    elapsed = time.time() - t0
    print("      axis.dateMismatch=%d  values=%d  mismatch=%d  ok=%s  (%.2fs)"
          % (res["axis"].get("dateMismatch", 0), res["checked"], res["mismatch"],
             res["ok"], elapsed))
    ok("反转后 verify.ok == False", res["ok"] is False, "ok=%s" % res["ok"])
    ok("反转后 mismatch > 0", res["mismatch"] > 0, "mismatch=%d" % res["mismatch"])
    ok("轴逐项比对报出 dateMismatch > 0", res["axis"].get("dateMismatch", 0) > 0,
       "dateMismatch=%d" % res["axis"].get("dateMismatch", 0))
    # 顺手证明 load() 的首日守卫也会拒绝这套错轴
    PC.reset_memo()
    ok("反转后 load() 拒绝使用（返回 None）", PC.load(conn, FACTORS[0]) is None)
    PC._save_meta(good)          # 恢复
    PC.reset_memo()
    res2 = PC.verify(conn, mode="full")
    ok("恢复后 verify.ok == True", res2["ok"] is True,
       "mismatch=%d checked=%d" % (res2["mismatch"], res2["checked"]))


# ────────────────────────────────────────────────────────────
# A2 / A3：部分提交中断 + 行号不变
# ────────────────────────────────────────────────────────────
def case_a2_partial_commit(conn: sqlite3.Connection, new_dates: list) -> tuple:
    print("\n[A2] 注入「只写了 12 个 npy 就中断」→ 旧 meta 与旧 npy 必须完好可用")
    old_meta = PC._load_meta()
    old_snap = snapshot(old_meta)
    R, C = len(old_meta["dates"]), len(old_meta["symbols"])
    print("      旧轴 %d × %d，%d 个因子，%d MB"
          % (R, C, len(old_snap),
             sum(a.nbytes for a in old_snap.values()) // 1024 // 1024))

    st = PC.stage_append(conn, new_dates)
    print("      staging：+%d 日 / +%d 只 → 新轴 %d × %d，%d 个因子，%.2fs"
          % (st["deltaDates"], st["deltaSymbols"], st["axisDates"], st["axisSymbols"],
             len(st["factors"]), st["elapsed"]))
    ok("stage_append 纳入新列（IPO 新股）", st["deltaSymbols"] == N_NEW_SYMBOLS,
       "deltaSymbols=%d" % st["deltaSymbols"])

    # 模拟「只写了 12 个就断电」：删掉一半 staging npy
    staged = sorted(st["factors"])
    killed = staged[len(staged) // 2:]
    for name in killed:
        os.remove(os.path.join(PC._stage_dir(), name + ".npy"))
    print("      人为删除 %d/%d 个 staging npy（模拟断电）" % (len(killed), len(staged)))

    cr = PC.commit_stage()
    ok("staging 不完整时 commit_stage 拒绝提交", cr["ok"] is False,
       "ok=%s reason=%s" % (cr["ok"], cr.get("reason")))

    meta_now = PC._load_meta()
    ok("旧 meta.json 未被改动",
       meta_now.get("dates") == old_meta["dates"]
       and meta_now.get("symbols") == old_meta["symbols"],
       "轴 %d × %d" % (len(meta_now.get("dates") or []),
                       len(meta_now.get("symbols") or [])))
    loadable = 0
    shape_ok = 0
    for name, arr in old_snap.items():
        try:
            cur = np.load(os.path.join(PC.CACHE_DIR, name + ".npy"))
            loadable += 1
            if cur.shape == arr.shape and np.array_equal(cur, arr, equal_nan=True):
                shape_ok += 1
        except Exception:      # noqa: BLE001
            pass
    ok("旧 npy 全部可 np.load", loadable == len(old_snap),
       "%d/%d" % (loadable, len(old_snap)))
    ok("旧 npy 内容逐位未变", shape_ok == len(old_snap),
       "%d/%d" % (shape_ok, len(old_snap)))

    df = PC.load(conn, FACTORS[0])
    ok("中断后 load() 仍返回旧轴数据",
       df is not None and df.shape == (R, C),
       "shape=%s（期望 %s）" % (None if df is None else df.shape, (R, C)))
    if df is not None:
        ok("中断后 load() 的数值与旧快照一致",
           np.allclose(df.to_numpy(dtype="float64"),
                       old_snap[FACTORS[0]].astype("float64"),
                       rtol=0, atol=0, equal_nan=True),
           "shape=%s" % (df.shape,))

    PC.abort_stage()
    ok("abort_stage 后 staging 目录已清空",
       not [f for f in os.listdir(PC._stage_dir()) if f.endswith(".npy")]
       if os.path.isdir(PC._stage_dir()) else True)
    return old_snap, (R, C)


def case_a3_append_keeps_rows(conn: sqlite3.Connection, new_dates: list,
                              old_snap: dict, old_shape: tuple) -> None:
    print("\n[A3] 追加后同一 (factor, symbol) 的行号列号不变")
    R, C = old_shape
    st = PC.stage_append(conn, new_dates)
    vf = PC.verify_stage(conn, mode="tail")
    print("      verify_stage：抽 %d 格，最大相对误差 %.3e，mismatch=%d，ok=%s（%.2fs）"
          % (vf["checked"], vf["maxRelErr"], vf["mismatch"], vf["ok"],
             vf.get("elapsed", 0.0)))
    ok("verify_stage 通过", vf["ok"] is True,
       "mismatch=%d detail=%s" % (vf["mismatch"], vf["details"][:1]))

    cr = PC.commit_stage()
    ok("commit_stage 成功", cr["ok"] is True and len(cr["committed"]) == len(FACTORS),
       "%d 个因子 %.2fs" % (len(cr.get("committed") or []), cr.get("elapsed", 0.0)))

    same = 0
    shape_ok = 0
    worst_rows = []
    for name in FACTORS:
        new = np.load(os.path.join(PC.CACHE_DIR, name + ".npy"))
        old = old_snap[name]
        if new.shape == (R + N_NEW_DATES, C + N_NEW_SYMBOLS):
            shape_ok += 1
        else:
            worst_rows.append((name, new.shape))
        if np.array_equal(new[:R, :C], old, equal_nan=True):
            same += 1
        else:
            worst_rows.append((name, "旧块被改动"))
    print("      形状 %d×%d → %d×%d；旧块逐位相同 %d/%d"
          % (R, C, R + N_NEW_DATES, C + N_NEW_SYMBOLS, same, len(FACTORS)))
    ok("新矩阵形状 = 旧形状 + (ΔR, ΔC)", shape_ok == len(FACTORS),
       "%d/%d 不符 %s" % (shape_ok, len(FACTORS), worst_rows[:3]))
    ok("逐因子 assert np.array_equal(new[:R,:C], old, equal_nan=True)",
       same == len(FACTORS), "%d/%d 相同 %s" % (same, len(FACTORS), worst_rows[:3]))

    # 新行必须真的写了值（不是一片 NaN）
    new = np.load(os.path.join(PC.CACHE_DIR, FACTORS[0] + ".npy"))
    tail_finite = float(np.isfinite(new[R:, :C]).mean())
    ok("新增行的格子已从库回填（非全 NaN）", tail_finite > 0.5,
       "new rows 有值比例 %.1f%%" % (tail_finite * 100))
    # 新列（IPO）在旧日期上也必须回填
    if N_NEW_SYMBOLS:
        col_finite = float(np.isfinite(new[:R, C:]).mean())
        ok("新增列（IPO）在旧日期上已回填", col_finite > 0.0,
           "new cols 有值比例 %.1f%%" % (col_finite * 100))

    meta = PC._load_meta()
    ok("meta 轴已扩到新形状",
       len(meta["dates"]) == R + N_NEW_DATES
       and len(meta["symbols"]) == C + N_NEW_SYMBOLS,
       "%d × %d" % (len(meta["dates"]), len(meta["symbols"])))
    vf2 = PC.verify(conn, mode="full")
    ok("提交后 verify(mode=full) 全绿", vf2["ok"] is True,
       "checked=%d mismatch=%d tailPendingDates=%d"
       % (vf2["checked"], vf2["mismatch"], vf2["axis"].get("tailPendingDates", 0)))


# ────────────────────────────────────────────────────────────
# A4：load() 性能
# ────────────────────────────────────────────────────────────
def case_a4_load_perf(conn: sqlite3.Connection) -> None:
    print("\n[A4] load() 单次耗时 < 0.5s")
    worst = 0.0
    for name in FACTORS[:5]:
        t0 = time.time()
        df = PC.load(conn, name)
        dt = time.time() - t0
        assert df is not None, name
        worst = max(worst, dt)
    print("      临时缓存（%d×%d）单次 load() 最慢 %.4fs"
          % (df.shape[0], df.shape[1], worst))
    ok("临时缓存 load() < 0.5s", worst < 0.5, "%.4fs" % worst)

    if os.environ.get("PANEL_CACHE_SKIP_PROD"):
        print("      （PANEL_CACHE_SKIP_PROD=1，跳过生产库实测）")
        return
    if not (os.path.isfile(PROD_DB) and os.path.isfile(os.path.join(PROD_CACHE, "meta.json"))):
        print("      （生产库/生产缓存不存在，跳过）")
        return

    real_dir = PC.CACHE_DIR
    try:
        PC.CACHE_DIR = PROD_CACHE
        PC.reset_memo()
        prod = sqlite3.connect("file:%s?mode=ro" % PROD_DB.replace("\\", "/"),
                               uri=True, timeout=120)
        try:
            name = sorted(f for f in os.listdir(PROD_CACHE) if f.endswith(".npy"))[0][:-4]
            t0 = time.time()
            df = PC.load(prod, name)
            dt = time.time() - t0
            shape = None if df is None else df.shape
            print("      生产缓存 %s：load() %.4fs  shape=%s" % (name, dt, shape))
            ok("生产缓存 load() < 0.5s", df is not None and dt < 0.5,
               "%.4fs（改前实测 16.20s）" % dt)
        finally:
            prod.close()
    finally:
        PC.CACHE_DIR = real_dir
        PC.reset_memo()


# ────────────────────────────────────────────────────────────
def main() -> int:
    global PASS, FAIL
    PASS = FAIL = 0
    FAILS.clear()
    real_cache = PC.CACHE_DIR
    tmp = tempfile.mkdtemp(prefix="panel_cache_t01_")
    print("=" * 74)
    print("T01 缓存层改造验收（临时目录 %s）" % tmp)
    print("=" * 74)
    try:
        PC.CACHE_DIR = os.path.join(tmp, "panel_cache")
        os.makedirs(PC.CACHE_DIR, exist_ok=True)
        db_path = os.path.join(tmp, "t01.db")
        conn, dates, symbols, rng = make_db(db_path)
        print("\n[建库] %d 只 × %d 天 × %d 因子（约 80%% 有值）"
              % (len(symbols), len(dates), len(FACTORS)))

        PC.reset_memo()
        t0 = time.time()
        res = PC.ensure(conn, FACTORS, min_rows=0, verbose=False)
        print("[建缓存] %d 个因子，%.2fs，sig=%s"
              % (len(res["built"]), time.time() - t0, res["sig"]))
        assert len(res["built"]) == len(FACTORS), res

        case_a0_legacy_migration(conn)
        case_a1_reversed_axis(conn)
        new_dates = add_tail(conn, dates[-1], N_NEW_DATES, symbols,
                             ["%06d" % (900 + i) for i in range(N_NEW_SYMBOLS)], rng)
        print("\n[追加] 库里新增 %d 个交易日：%s；新增 %d 只（IPO）"
              % (len(new_dates), new_dates, N_NEW_SYMBOLS))
        td = PC.tail_delta(conn)
        ok("tail_delta 识别出尾部增量",
           td["newDates"] == new_dates and len(td["newSymbols"]) == N_NEW_SYMBOLS,
           "newDates=%s newSymbols=%s" % (td["newDates"], td["newSymbols"]))

        old_snap, old_shape = case_a2_partial_commit(conn, new_dates)
        case_a3_append_keeps_rows(conn, new_dates, old_snap, old_shape)
        case_a4_load_perf(conn)
        conn.close()
    finally:
        PC.CACHE_DIR = real_cache
        PC.reset_memo()
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n========================================")
    print("RESULT: pass=%d fail=%d" % (PASS, FAIL))
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
    print("========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
