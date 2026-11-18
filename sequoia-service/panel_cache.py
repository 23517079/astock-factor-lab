"""因子面板的磁盘缓存（npy + mmap），把「等半小时」变成「秒开」。

要解决的问题
------------
factor_panel 在 5 年口径下有 1.5 亿行。取单个因子的宽表（646 万格）实测：
cursor.fetchall 73 秒、Python 构矩阵再 40 秒，合计 116 秒；而 pandas pivot
本身只要 1.5 秒 —— 时间几乎全花在「把行搬出 SQLite」上，且是重复劳动：
因子评估、因子选股、分板块体检三个脚本读的是同一份数据，每跑一次就重付一次。

做法
----
把每个因子导成 float32 的 .npy（1420×5030 约 28MB），日期轴和代码轴全因子共用，
之后用 np.load(mmap_mode="r") 读。内存映射意味着不占常驻内存，
只有真正被访问的页才会读盘。

为什么 float32 够用
-------------------
因子值量级都在 1e-3 ~ 1e3 之间，float32 有 7 位有效数字；下游用的 RankIC
和横截面排名对末位误差完全不敏感（排名只看相对大小）。真要核对原始数值，
仍然可以回数据库查。

一致性（2026-11-16 改造，替代旧的 fingerprint 机制）
────────────────────────────────────────────────────
旧实现用一条 SQL 同时取 `COUNT(*) + MIN(date) + MAX(date) + COUNT(DISTINCT factor)`
做指纹，实测 35~66 秒/次，而 `load()` 每读一个因子都调一次 → 24 因子光算指纹
就 26 分钟。这是「滚动验证 ≤5 分钟」不可能成立的根本原因。

现在拆成两层：

* **结构签名（structure_sig）** —— 只在真正需要判断「缓存是否还有效」时算：
      sig = "min=<最早日期>|nf=<因子数>|fh=<因子名排序后 sha1 前 8 位>"
  成本 = `SELECT MIN(date)`（0.001s，走索引）+ `SELECT DISTINCT factor`
  （7.7s，**进程内 memo，全进程只算一次**）。刻意**不含** rows / max(date) /
  symbol 数：max(date) 每天都会变，把它放进签名会让缓存天天整体失效。
* **轴（append-only）** —— meta.dates / meta.symbols 只追加、永不重排、永不重算。
  尾部增量用 `SELECT DISTINCT date|symbol FROM factor_panel WHERE date > 轴末日`
  （实测 0.0s / 0.2s）。

因此 `load()` 里**不再有任何全表聚合**：只读 meta.json + 做形状校验 + 一条
0.001s 的 `MIN(date)` 首日守卫。实测单次 `load()` 从 16.2 秒降到 0.02~0.05 秒
（详见 tests/test_panel_cache_append.py 的注释）。

写入路径（staging 双写 + 原子发布）
----------------------------------
npy 是裸矩阵，`open_memmap` 改不了 shape 头，就地追加中断会把唯一一份缓存写坏。
所以追加一律先写到 `data/panel_cache/_stage/`：

    _stage/<factor>.npy   新形状 (R+ΔR) × (C+ΔC)
                          旧数据整块 memcpy 到 [0:R, 0:C]（行号列号完全不变）
                          + 新行由 DB 回读 targetDates 的切片填在 [R:, :]
    _stage/meta.new.json  新轴 = dates + new_dates, symbols + new_symbols
    _stage/manifest.json  本次应写入的因子清单与目标形状（供 verify/commit 判断完整性）

提交序列（只有 verify_stage().ok is True 才进）：

    verify_stage().ok == True
      → 逐个 os.replace(_stage/x.npy → x.npy)
      → 最后 os.replace(meta.new.json → meta.json)   # meta 是唯一「提交指针」

崩在提交前：正式缓存与 meta.json 完好，旧轴继续可用。`load()` 用 meta 的轴长度
做形状校验，长度不符的孤儿 npy 一律返回 None，绝不会出现「新矩阵被旧轴解释」
这种静默错位。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

CACHE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                         "data", "panel_cache")

# staging 子目录名（与 meta.json 同级，便于整体 rename/删除）
STAGE_NAME = "_stage"
# meta 里因子条目的默认 dtype（全部因子统一 float32，见模块 docstring）
DTYPE = "float32"
# 签名里 sha1 取前几位：8 位 hex = 32 bit，24 个因子名的碰撞概率可忽略
SIG_HASH_LEN = 8

# 进程内 memo：key = main 库的绝对路径。
# 存 min_date / 因子名集合 / 结构签名，避免「读一个因子算一次 DISTINCT factor」。
_MEMO: Dict[str, Dict[str, Any]] = {}


# ────────────────────────────────────────────────────────────
# 路径与小工具
# ────────────────────────────────────────────────────────────
def _meta_path() -> str:
    return os.path.join(CACHE_DIR, "meta.json")


def _stage_dir() -> str:
    return os.path.join(CACHE_DIR, STAGE_NAME)


def _stage_npy(name: str) -> str:
    return os.path.join(_stage_dir(), name + ".npy")


def _stage_meta_path() -> str:
    return os.path.join(_stage_dir(), "meta.new.json")


def _stage_manifest_path() -> str:
    return os.path.join(_stage_dir(), "manifest.json")


def _npy_path(name: str) -> str:
    return os.path.join(CACHE_DIR, name + ".npy")


def _db_key(conn: sqlite3.Connection) -> str:
    """用 main 库的绝对路径当 memo 的 key（一个进程可能连多个库）。"""
    try:
        for row in conn.execute("PRAGMA database_list"):
            # row = (seq, name, file)
            if row[1] == "main":
                return os.path.abspath(row[2] or "")
    except sqlite3.Error:
        pass
    return "<unknown>"


def _memo(conn: sqlite3.Connection) -> Dict[str, Any]:
    key = _db_key(conn)
    slot = _MEMO.get(key)
    if slot is None:
        slot = _MEMO[key] = {}
    return slot


def reset_memo() -> None:
    """清空进程内 memo。

    面板被**重建**（而不是追加）后必须调用一次，否则 `structure_sig()` 会返回
    重建前的旧签名。测试里也用它来强制重新取一次 `DISTINCT factor`。
    """
    _MEMO.clear()


def _read_json(path: str) -> Dict[str, Any]:
    """读 JSON；文件不存在/损坏一律返回空 dict（缓存不可用绝不抛异常）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    """`.tmp → os.replace`。目录不存在则先建（staging 首次调用时目录还没有）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _load_meta() -> Dict[str, Any]:
    return _read_json(_meta_path())


def _save_meta(meta: Dict[str, Any]) -> None:
    _write_json_atomic(_meta_path(), meta)


def _worth_it(n_rows: int, min_rows: int) -> bool:
    """小面板不值得落盘（导出的时间比省下来的还多）。"""
    return n_rows >= min_rows


def _shape_of(meta: Dict[str, Any]) -> Tuple[int, int]:
    """meta 里登记的轴长度 (行数=日期数, 列数=代码数)。"""
    return len(meta.get("dates") or []), len(meta.get("symbols") or [])


# ────────────────────────────────────────────────────────────
# 结构签名（替代旧的 fingerprint）
# ────────────────────────────────────────────────────────────
def _min_date(conn: sqlite3.Connection) -> Optional[str]:
    """面板最早日期。走索引，实测 0.001s。进程内 memo。"""
    slot = _memo(conn)
    if "min_date" not in slot:
        slot["min_date"] = conn.execute("SELECT MIN(date) FROM factor_panel").fetchone()[0]
    return slot["min_date"]


def factor_names(conn: sqlite3.Connection) -> List[str]:
    """**查库**取因子名（升序）。实测 7.4s（热）/ 31s（冷页缓存），进程内只算一次。

    ⚠ 这是**体检/排查用途**，不要放在每日路径上。原因：`SELECT DISTINCT factor`
    在 1.536 亿条上是覆盖索引全扫（`SCAN … USING COVERING INDEX idx_factor_date`），
    不是 loose index scan；冷态 31s 会直接挤爆 plan 的 15s 预算。

    因子集合的**权威来源是 `factors/registry.py`，不是数据库**（见
    `registry_factor_names()`）。查库只用于**发现不一致**（`validate_structure()`）——
    那是体检职责，不是每日路径职责。
    """
    slot = _memo(conn)
    if "factor_names" not in slot:
        slot["factor_names"] = sorted(
            r[0] for r in conn.execute("SELECT DISTINCT factor FROM factor_panel"))
    return list(slot["factor_names"])


_REGISTRY_NAMES: Optional[List[str]] = None


def registry_factor_names() -> List[str]:
    """`factors/registry.py` 里登记的因子名（升序）。**因子集合的权威来源**。

    不查库，成本 O(24)，进程内只导入一次。
    """
    global _REGISTRY_NAMES
    if _REGISTRY_NAMES is None:
        from factors import registry          # 延迟导入：避免 panel_cache 一 import 就拉起因子计算栈
        _REGISTRY_NAMES = sorted(registry.all_names())
    return list(_REGISTRY_NAMES)


def factors_hash(names: Sequence[str]) -> str:
    """因子名排序后取 sha1 前 8 位（名字顺序不影响结果）。"""
    joined = "\n".join(sorted(names))
    return hashlib.sha1(joined.encode("utf-8")).hexdigest()[:SIG_HASH_LEN]


def fast_sig(conn: sqlite3.Connection) -> str:
    """**日常结构签名**：`min=<MIN(date)>|nf=<registry 因子数>|fh=<registry 名哈希>`。

    成本 **< 1ms**：
      * `SELECT MIN(date)` 0.0001s（走 ux(date,symbol,factor) 的首列）；
      * `nf` / `fh` 来自 registry（O(24)），**不查库**。

    为什么可以用 registry 当权威：库里有「哪些因子」本来就该由 registry 决定。
    如果两者不一致（多一个 / 少一个 / 改名），说明库被绕过 registry 写坏了 ——
    那是 `validate_structure()` 要在**体检路径**上报的错，而不是让每日任务
    每次都花 7~31 秒去全表扫一遍确认。

    它同样刻意**不含** `rows` / `max(date)` / symbol 数（理由见 `structure_sig`）。
    """
    names = registry_factor_names()
    return "min=%s|nf=%d|fh=%s" % (_min_date(conn), len(names), factors_hash(names))


def structure_sig(conn: sqlite3.Connection) -> str:
    """结构签名：`min=<最早日期>|nf=<因子数>|fh=<因子名哈希>`（= `fast_sig()`）。

    刻意**不含** `COUNT(*)` / `MAX(date)` / symbol 数：
      * COUNT(*) 要全表扫（23.7s），且每次追加都变 → 缓存天天整体失效；
      * MAX(date) 每个交易日都变，同理；
      * symbol 数是「尾部可追加」的量，不该触发重建。

    这三样由**轴**（meta.dates / meta.symbols，append-only）负责，不参与签名。
    返回的是字符串，可直接写进 meta.json 做「上次建缓存时的结构」。
    """
    slot = _memo(conn)
    if not slot.get("sig"):
        slot["sig"] = fast_sig(conn)
    return str(slot["sig"])


def validate_structure(conn: sqlite3.Connection) -> Dict[str, Any]:
    """**重路径**校验：库里 `DISTINCT factor` 与 registry 逐项比对（约 7~31s）。

    只在 `ensure()` / `check_panel` / `--migrate` 这类低频、且**本来就要付磁盘
    代价**的路径上调用。不一致就明确报出来，绝不静默 —— 库里出现 registry
    没登记的因子，意味着有人绕过 registry 写了数据，后续所有判定都会错。

    Returns:
        {"ok", "dbOnly": [...], "registryOnly": [...],
         "dbChecked": 库里因子数, "registry": registry 因子数}
    """
    db = factor_names(conn)
    reg = registry_factor_names()
    db_only = sorted(set(db) - set(reg))
    reg_only = sorted(set(reg) - set(db))
    return {"ok": bool(not db_only and not reg_only),
            "dbOnly": db_only, "registryOnly": reg_only,
            "dbChecked": len(db), "registry": len(reg)}


def fingerprint(conn: sqlite3.Connection) -> str:
    """**已废弃**，仅为向后兼容保留；等价于 `structure_sig()`。

    旧实现（`rows=..|min=..|max=..|nf=..`）会让缓存每个交易日整体失效，
    且单次成本 35~66 秒。新代码请一律用 `structure_sig()`。
    """
    return structure_sig(conn)


# ────────────────────────────────────────────────────────────
# 轴
# ────────────────────────────────────────────────────────────
def _global_axis(conn: sqlite3.Connection) -> Tuple[List[str], List[str]]:
    """全库统一的日期轴与代码轴（升序）。

    轴必须**一次定死、永不改变**：npy 是裸矩阵，没有列名，行的含义完全由
    meta 里的轴决定。如果建缓存的过程中轴被重排（比如后来的因子列顺序不同，
    顺手 sorted 了一下），先前写好的 npy 就会被用新顺序去解释 —— 结果是
    整列错位、数据张冠李戴，而且不会报任何错。
    """
    dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM factor_panel ORDER BY date ASC")]
    syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM factor_panel ORDER BY symbol ASC")]
    return dates, syms


def tail_delta(conn: sqlite3.Connection) -> Dict[str, Any]:
    """算出「相对现有轴，尾部多了哪些日期 / 哪些新代码」。

    成本：尾部日期 `SELECT DISTINCT date ... WHERE date > 轴末日`（实测 0.0s，
    走 ux(date,symbol,factor) 范围扫描）、尾部代码同理（0.2s）。

    Returns:
        {"ok": bool, "maxDate": 轴末日, "newDates": [...], "newSymbols": [...],
         "axisDates": 轴长度, "axisSymbols": 轴长度, "cells": 新增行×列}
        `ok=False` 表示没有可追加的轴（需要先 `ensure()` 建缓存）。
    """
    meta = _load_meta()
    dates = list(meta.get("dates") or [])
    syms = list(meta.get("symbols") or [])
    if not meta.get("axisFixed") or not dates or not syms:
        return {"ok": False, "reason": "没有可用的缓存轴，请先 ensure() 建缓存",
                "maxDate": None, "newDates": [], "newSymbols": [],
                "axisDates": len(dates), "axisSymbols": len(syms), "cells": 0}

    max_date = dates[-1]
    new_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM factor_panel WHERE date > ? ORDER BY date ASC",
        (max_date,))]
    have = set(syms)
    tail_syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM factor_panel WHERE date > ? ORDER BY symbol ASC",
        (max_date,))]
    new_syms = [s for s in tail_syms if s not in have]
    return {"ok": True, "reason": None, "maxDate": max_date,
            "newDates": new_dates, "newSymbols": new_syms,
            "axisDates": len(dates), "axisSymbols": len(syms),
            "cells": len(new_dates) * (len(syms) + len(new_syms)) + len(new_syms) * len(dates)}


# ────────────────────────────────────────────────────────────
# 建缓存（全量导出单个因子）
# ────────────────────────────────────────────────────────────
def _write_one(name: str, wide: pd.DataFrame, meta: Dict[str, Any]) -> None:
    """把因子宽表按固定轴重排后写盘。缺的格子在矩阵里就是 NaN，不是 0。"""
    aligned = wide.reindex(index=pd.Index(meta["dates"]),
                           columns=pd.Index(meta["symbols"]))
    mat = aligned.to_numpy(dtype=DTYPE, copy=True)
    path = _npy_path(name)
    tmp = path + ".tmp.npy"
    np.save(tmp, mat)
    os.replace(tmp, path)
    meta.setdefault("factors", {})[name] = {
        "rows": int(mat.shape[0]),
        "cols": int(mat.shape[1]),
        "dtype": DTYPE,
        "coverage": round(float(np.isfinite(mat).mean()), 6),
    }


def _migrate_legacy_meta(conn: sqlite3.Connection, meta: Dict[str, Any],
                         sig: str) -> bool:
    """把旧格式的 `fingerprint` 就地迁移成 `sig`，**不重建任何缓存**。

    为什么必须有这一步
    ------------------
    T01 之前的 meta.json 里存的是旧指纹
    `rows=<行数>|min=<最早>|max=<最新>|nf=<因子数>`，没有 `sig` 键。如果
    `ensure()` 只是简单地「sig 不一致 → 清空重来」，那么 2026-11-16 那 7 个
    已经建好、共 229MB 的因子缓存会在第一次跑 `panel_cache.py` 时被集体作废，
    重建要 84 分钟，这期间因子页/选股页全是空白。

    旧指纹里只有 `min` 和 `nf` 参与新签名（`rows`/`max` 是设计性丢弃的）——
    所以只要旧指纹里的 `min` 与 `nf` 和当前库一致，就可以判定「结构没变」，
    直接把 sig 补进 meta 即可。两个都校验，不做任何猜测。

    Returns:
        True 表示迁移成功（meta 已被改写并落盘）；False 表示不该迁移，调用方
        应按「结构已变」处理（清空重建）。
    """
    if meta.get("sig") or not meta.get("axisFixed"):
        return False
    legacy = str(meta.get("fingerprint") or "")
    if not legacy:
        return False
    kv: Dict[str, str] = {}
    for part in legacy.split("|"):
        if "=" in part:
            key, value = part.split("=", 1)
            kv[key.strip()] = value.strip()
    if kv.get("min") != str(_min_date(conn)) or kv.get("nf") != str(len(factor_names(conn))):
        return False
    meta["sig"] = sig
    meta.pop("fingerprint", None)
    meta["migratedFrom"] = legacy
    _save_meta(meta)
    return True


def ensure(conn: sqlite3.Connection, factors: Iterable[str] = (),
           min_rows: int = 200_000, verbose: bool = True) -> Dict[str, Any]:
    """确保指定因子的缓存存在；缺的补上，结构签名不一致则整体重建。

    Args:
        factors: 需要缓存的因子名；为空表示「库里全部因子」。
        min_rows: 小于这个行数的因子不缓存（预热期长、数据少的因子不值得）。
        verbose: 是否打印进度。

    Returns:
        {"sig":..., "fingerprint":..., "cached": [...], "built": [...], "skipped": [...],
         "structure": {...}}

    Raises:
        RuntimeError: 库里的因子集合与 `factors/registry.py` 不一致（多/少/改名）。
            这是**建缓存**路径，宁可停下来让人排查，也不能悄悄按错的集合建。
    """
    sig = structure_sig(conn)

    # 重路径校验：建缓存前先确认「库里的因子集合」与 registry 完全一致。
    # 不一致意味着有人绕过 registry 写了数据 —— 报错退出，不静默。
    structure = validate_structure(conn)
    if not structure["ok"]:
        raise RuntimeError(
            "库里的因子集合与 factors/registry.py 不一致，拒绝建缓存："
            "库里有但 registry 没有 %s；registry 有但库里没有 %s。"
            "请先排查是谁绕过 registry 写了 factor_panel。"
            % (structure["dbOnly"], structure["registryOnly"]))

    meta = _load_meta()
    if meta.get("sig") != sig or not meta.get("axisFixed"):
        if _migrate_legacy_meta(conn, meta, sig):
            if verbose:
                print("  已把旧指纹迁移为结构签名（%s），现有缓存全部保留" % sig, flush=True)
        else:
            if verbose and meta:
                print("  面板或轴已变化，重建缓存（%s）" % sig, flush=True)
            meta = {"sig": sig, "factors": {}}

    # 轴固定为全库并集：任何因子都只是在这套轴上填格子，绝不会改变轴
    if not meta.get("dates") or not meta.get("symbols"):
        t0 = time.time()
        meta["dates"], meta["symbols"] = _global_axis(conn)
        meta["axisFixed"] = True
        _save_meta(meta)
        if verbose:
            print("  固定轴：%d 天 × %d 只（%.0fs）"
                  % (len(meta["dates"]), len(meta["symbols"]), time.time() - t0), flush=True)

    want = list(factors) or registry_factor_names()

    n_dates, n_syms = _shape_of(meta)
    os.makedirs(CACHE_DIR, exist_ok=True)

    built: List[str] = []
    skipped: List[str] = []
    cached: List[str] = []
    stale: List[str] = []

    for name in want:
        entry = (meta.get("factors") or {}).get(name) or {}
        path = _npy_path(name)
        if (entry and os.path.exists(path)
                and int(entry.get("rows", -1)) == n_dates
                and int(entry.get("cols", -1)) == n_syms):
            cached.append(name)
            continue
        stale.append(name)

    # 只在真要建的因子上付 COUNT(*) 的钱（每个走 (factor,date) 索引，约 1s）
    if stale:
        from factor_eval import load_panel_wide  # 延迟导入，避免循环依赖
        for name in stale:
            t0 = time.time()
            n_rows = int(conn.execute(
                "SELECT COUNT(*) FROM factor_panel WHERE factor = ?", (name,)).fetchone()[0])
            if not _worth_it(n_rows, min_rows):
                skipped.append(name)
                continue
            wide = load_panel_wide(conn, name)
            if wide.empty:
                skipped.append(name)
                continue
            _write_one(name, wide, meta)
            # 每导完一个就落盘索引：中途被打断（换参数、机器被关）时，
            # 已经花掉的那几分钟不会白费，下次接着导剩下的。
            _save_meta(meta)
            built.append(name)
            if verbose:
                print("    缓存 %-16s %5d 天 × %4d 只  %5.0fs"
                      % (name, wide.shape[0], wide.shape[1], time.time() - t0), flush=True)

    return {"sig": sig, "fingerprint": sig, "built": built, "skipped": skipped,
            "cached": cached + built, "structure": structure}


# ────────────────────────────────────────────────────────────
# 读缓存
# ────────────────────────────────────────────────────────────
def _open_matrix(meta: Dict[str, Any], name: str) -> Optional[np.ndarray]:
    """按 meta 的轴打开一个因子的矩阵（**不做**任何数据库查询）。

    形状与轴长度不符 → 视为「缓存不完整」，返回 None。
    这条校验是「绝不允许新矩阵被旧轴解释」的唯一实现点：进程在提交途中被打断时，
    磁盘上可能既有旧 npy，也可能有已 replace 的新 npy，形状不符的直接被拒。
    """
    entry = (meta.get("factors") or {}).get(name)
    if not entry:
        return None
    path = _npy_path(name)
    if not os.path.exists(path):
        return None
    try:
        mat = np.load(path, mmap_mode="r")
    except (OSError, ValueError):
        return None
    want = (int(entry.get("rows", -1)), int(entry.get("cols", -1)))
    if tuple(mat.shape) != want:
        return None
    n_dates, n_syms = _shape_of(meta)
    if want != (n_dates, n_syms):
        return None
    return mat


def load(conn: sqlite3.Connection, name: str) -> Optional[pd.DataFrame]:
    """读一个因子的缓存宽表；不可用时返回 None。

    耗时（实测，1420×5030 缓存，见 tests/test_panel_cache_append.py 注释）:
        改前 load() ≈ 16.2s（因为每次都跑一次全表 fingerprint，约 35s，页缓存热后 16s）
        改后 load() ≈ 0.02s（本机 SSD）
    改后的成本只包含：读 meta.json + 一条 `SELECT MIN(date)`（0.001s，走索引）
    + mmap 打开 + float64 转换。**没有任何全表聚合**。

    唯一的结构守卫是「轴首日 == 库最早日期」（0.001s）：
      * 若有人重建了面板（MIN(date) 变了）→ 缓存立即作废；
      * 若因子集合变了 → 由 `structure_sig()` / `coverage()` 在 pipeline 启动时拦；
      * 若只有尾部多了日期 → 正常追加场景，缓存继续可用（这正是 append-only 的意义）。
    """
    meta = _load_meta()
    if not meta or not meta.get("axisFixed"):
        return None
    dates = list(meta.get("dates") or [])
    syms = list(meta.get("symbols") or [])
    if not dates or not syms:
        return None
    mat = _open_matrix(meta, name)
    if mat is None:
        return None

    # 0.001s 首日守卫：面板整体重建（MIN(date) 变）则缓存作废
    if _min_date(conn) != dates[0]:
        return None

    # 若本次进程已经知道因子集合（memo 命中，零成本），顺手核对一次结构。
    # 注意方向：只拦「缓存里有、库里没有」的因子（因子被删/改名 → 缓存已过期）；
    # 「库里有、缓存里没有」是正常的（冷启动只补了部分因子），不构成失效。
    slot = _memo(conn)
    known = slot.get("factor_names")
    if known is not None and (set((meta.get("factors") or {}).keys()) - set(known)):
        return None

    idx = pd.Index(dates, name="date")
    cols = pd.Index(syms, name="symbol")
    return pd.DataFrame(np.asarray(mat, dtype="float64"), index=idx, columns=cols)


# ────────────────────────────────────────────────────────────
# 尾部追加：staging 双写
# ────────────────────────────────────────────────────────────
def stage_append(conn: sqlite3.Connection, target_dates: Sequence[str],
                 factors: Optional[Iterable[str]] = None,
                 progress: Optional[Any] = None) -> Dict[str, Any]:
    """把 `target_dates` 的数据追加到 staging 目录（**不动正式缓存**）。

    新矩阵形状 (R+ΔR) × (C+ΔC)：
      * 旧数据整块拷到 [0:R, 0:C] —— 已有行号/列号完全不变，这是「append-only」的本体；
      * 新行 [R:, :] 由 DB 回读 target_dates 的切片，reindex 到新轴后填入；
      * 新列 [:, C:]（IPO 新股）由 DB 回读该代码的全部历史填入——新股的列在旧行上
        也有值，不能留 NaN，否则「上市第一天的因子」会凭空缺失。

    轴铁律（不满足直接抛 RuntimeError，绝不静默错位）：
      * `target_dates` 必须**恰好等于**库里 `date > 轴末日` 的全部日期。
        理由：轴只允许「尾部追加」，如果尾部有日期没纳入本次目标，新矩阵就会
        在轴上留一个全 NaN 的行，而库里有值 —— 那是错的缓存，必须在入口拦下。
      * 轴末日之后不允许出现「中间日期」，只能接在轴尾。

    Args:
        conn: 已写入目标日期的库连接（pipeline 里「写库」必须早于本步）。
        target_dates: 本次追加的日期（YYYY-MM-DD）。
        factors: 要追加的因子；None 表示 meta 里已有的全部因子。
        progress: 可调用对象 `progress(i, total, name)`，用于打印进度。

    Returns:
        {"ok":..., "axisDates":..., "axisSymbols":..., "deltaDates":...,
         "deltaSymbols":..., "factors": [...], "stageDir":..., "elapsed": 秒}
    """
    t0 = time.time()
    meta = _load_meta()
    old_dates = list(meta.get("dates") or [])
    old_syms = list(meta.get("symbols") or [])
    if not meta.get("axisFixed") or not old_dates or not old_syms:
        raise RuntimeError("没有可追加的缓存轴：请先跑 panel_cache.ensure() 建好全量缓存")
    rows, cols = len(old_dates), len(old_syms)
    max_date = old_dates[-1]

    want_dates = sorted({str(d) for d in target_dates})
    db_tail = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM factor_panel WHERE date > ? ORDER BY date ASC",
        (max_date,))]
    if [d for d in want_dates if d <= max_date]:
        raise RuntimeError("target_dates 里含不晚于轴末日的日期（%s），轴只允许尾部追加"
                           % [d for d in want_dates if d <= max_date][:5])
    if db_tail != want_dates:
        raise RuntimeError(
            "轴尾部与目标日期不一致：库里 date > %s 的日期有 %d 个，目标有 %d 个；"
            "缺 %s、多 %s。先把这些日期算进 factor_panel 再追加（轴不允许留空洞）。"
            % (max_date, len(db_tail), len(want_dates),
               [d for d in db_tail if d not in set(want_dates)][:5],
               [d for d in want_dates if d not in set(db_tail)][:5]))

    # 新代码（IPO）：只可能出现在尾部
    have_syms = set(old_syms)
    tail_syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM factor_panel WHERE date > ? ORDER BY symbol ASC",
        (max_date,))]
    new_syms = [s for s in tail_syms if s not in have_syms]

    new_dates = old_dates + want_dates
    new_symbols = old_syms + new_syms
    delta_rows, delta_cols = len(want_dates), len(new_syms)
    new_rows, new_cols = rows + delta_rows, cols + delta_cols

    names = list(factors) if factors else sorted((meta.get("factors") or {}).keys())
    if not names:
        raise RuntimeError("meta 里没有任何因子缓存，无法追加；请先 ensure()")

    sdir = _stage_dir()
    # 用 mkdir(exist_ok) 而不是 rmtree：万一上一次 abort 失败，残留文件会被本次覆盖，
    # 但 manifest 会重写，绝不会把上次的残留当成这次的产物。
    os.makedirs(sdir, exist_ok=True)

    # 日期 → 新轴行号（含旧行与新行，新代码回填旧日期时要用到）
    date_pos = {d: i for i, d in enumerate(new_dates)}
    sym_pos = {s: i for i, s in enumerate(new_symbols)}

    staged: List[Dict[str, Any]] = []
    for i, name in enumerate(names):
        if progress is not None:
            try:
                progress(i, len(names), name)
            except Exception:  # noqa: BLE001 — 进度回调出错不该毁掉追加
                pass
        old = _open_matrix(meta, name)
        if old is None:
            # 该因子没有完整旧缓存 → 不在本次追加范围（由 coverage() 负责报缺）
            continue

        buf = np.full((new_rows, new_cols), np.nan, dtype=DTYPE)
        buf[:rows, :cols] = old[:]          # 行号列号完全不变

        # 1) 新行：回读 target_dates 的切片，reindex 到新轴
        if delta_rows:
            sql = ("SELECT date, symbol, value FROM factor_panel WHERE factor = ? "
                   "AND date IN (%s)" % ",".join("?" * len(want_dates)))
            for d, s, v in conn.execute(sql, [name] + list(want_dates)):
                di = date_pos.get(d)
                si = sym_pos.get(s)
                if di is None or si is None or v is None:
                    continue
                buf[di, si] = v

        # 2) 新列（IPO）：回读该代码全部历史，填进 [:, cols:]
        for s in new_syms:
            si = sym_pos[s]
            for d, v in conn.execute(
                    "SELECT date, value FROM factor_panel "
                    "WHERE factor = ? AND symbol = ?", (name, s)):
                di = date_pos.get(d)
                if di is None or v is None:
                    continue
                buf[di, si] = v

        np.save(_stage_npy(name), buf)
        finite = float(np.isfinite(buf).mean())
        staged.append({"factor": name, "rows": int(new_rows), "cols": int(new_cols),
                       "coverage": round(finite, 6)})
        del buf, old

    if not staged:
        raise RuntimeError("没有任何因子被写入 staging（缓存可能不完整），已中止追加")

    new_meta = {
        "sig": meta.get("sig") or structure_sig(conn),
        "axisFixed": True,
        "dates": new_dates,
        "symbols": new_symbols,
        "factors": {k: v for k, v in (meta.get("factors") or {}).items()
                    if k in set(names)},
        "stagedAt": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "delta": {"rows": delta_rows, "cols": delta_cols},
    }
    for item in staged:
        new_meta["factors"][item["factor"]] = {
            "rows": item["rows"], "cols": item["cols"], "dtype": DTYPE,
            "coverage": item["coverage"]}

    _write_json_atomic(_stage_meta_path(), new_meta)
    _write_json_atomic(_stage_manifest_path(), {
        "createdAt": new_meta["stagedAt"],
        "deltaRows": delta_rows, "deltaCols": delta_cols,
        "rows": new_rows, "cols": new_cols,
        "oldRows": rows, "oldCols": cols,
        "maxDate": max_date,
        "targetDates": want_dates,
        "newSymbols": new_syms,
        "factors": staged,
    })

    return {"ok": True, "axisDates": new_rows, "axisSymbols": new_cols,
            "deltaDates": delta_rows, "deltaSymbols": delta_cols,
            "factors": [s["factor"] for s in staged],
            "stageDir": sdir, "elapsed": round(time.time() - t0, 2)}


def _stage_state() -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """读 staging 的 (meta.new.json, manifest.json)。"""
    return _read_json(_stage_meta_path()), _read_json(_stage_manifest_path())


def verify_stage(conn: sqlite3.Connection, mode: str = "tail", rtol: float = 1e-4,
                 sample: int = 120) -> Dict[str, Any]:
    """核对 staging 里的新矩阵，只有全绿才能 commit。

    检查项（任一不通过 → ok=False，绝不提交）：
      1. manifest / meta.new 齐全，且 manifest 里每个因子的 npy 都存在、形状正确；
      2. 轴逐项比对：meta.new.dates[:R] == meta.dates（顺序也必须一致）、symbols 同理；
         meta.new.dates[R:] 必须**逐项等于**库里 `date > 轴末日` 的日期；
      3. 数值抽样：从新行里取 `sample` 个格子回查 DB，相对误差 ≤ rtol；
      4. 旧块保真（mode="tail" 也做）：随机若干行断言 staged[:R,:C] 与旧 npy 一致；
      5. mode="full" 时额外做全量轴校验（DISTINCT date / DISTINCT symbol 全扫，约 25s）。

    Returns:
        {"ok", "checked", "maxRelErr", "mismatch", "axis": {...}, "details": [...]}
        `mismatch` = 数值不一致数 + 轴逐项不一致数（两者都会让 ok=False）。
    """
    import random

    t0 = time.time()
    new_meta, manifest = _stage_state()
    details: List[Dict[str, Any]] = []
    axis = {"ok": False, "dates": False, "symbols": False, "mode": mode,
            "dateMismatch": 0, "symbolMismatch": 0,
            "tailPendingDates": 0, "tailPendingSymbols": 0}
    result: Dict[str, Any] = {"ok": False, "checked": 0, "maxRelErr": 0.0,
                              "mismatch": 0, "axis": axis, "details": details}

    if not new_meta or not manifest:
        details.append({"reason": "staging 不完整（缺 meta.new.json 或 manifest.json）"})
        result["mismatch"] = 1
        return result

    old_meta = _load_meta()
    old_dates = list(old_meta.get("dates") or [])
    old_syms = list(old_meta.get("symbols") or [])
    rows, cols = len(old_dates), len(old_syms)
    new_dates = list(new_meta.get("dates") or [])
    new_syms = list(new_meta.get("symbols") or [])
    if int(manifest.get("oldRows", -1)) != rows or int(manifest.get("oldCols", -1)) != cols:
        details.append({"reason": "staging 生成后正式轴发生了变化（旧轴 %d×%d，manifest 记 %s×%s）"
                                  % (rows, cols, manifest.get("oldRows"), manifest.get("oldCols"))})
        result["mismatch"] = 1
        return result

    # ---- 1) 轴逐项比对 ----
    head_dates = new_dates[:rows]
    head_syms = new_syms[:cols]
    d_bad = sum(1 for i in range(min(len(head_dates), rows)) if head_dates[i] != old_dates[i])
    s_bad = sum(1 for i in range(min(len(head_syms), cols)) if head_syms[i] != old_syms[i])
    axis["dates"] = (d_bad == 0 and head_dates == old_dates)
    axis["symbols"] = (s_bad == 0 and head_syms == old_syms)
    axis["dateMismatch"] = d_bad
    axis["symbolMismatch"] = s_bad
    if not axis["dates"]:
        details.append({"reason": "轴首部与旧轴不一致", "dateMismatch": d_bad})
    if not axis["symbols"]:
        details.append({"reason": "代码轴首部与旧轴不一致", "symbolMismatch": s_bad})

    # 尾部日期必须与库逐项一致（轴不允许留空洞）
    max_date = old_dates[-1] if old_dates else None
    db_tail = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM factor_panel WHERE date > ? ORDER BY date ASC",
        (max_date,))]
    tail_dates = new_dates[rows:]
    if db_tail != tail_dates:
        axis["dates"] = False
        axis["tailPendingDates"] = abs(len(db_tail) - len(tail_dates))
        details.append({"reason": "尾部日期与库不一致", "dbTail": db_tail[:5],
                        "stagedTail": tail_dates[:5],
                        "dbTailN": len(db_tail), "stagedTailN": len(tail_dates)})

    # ---- 2) 每个因子的 npy 是否存在、形状是否正确 ----
    staged_factors = [f.get("factor") for f in (manifest.get("factors") or [])]
    missing: List[str] = []
    for name in staged_factors:
        path = _stage_npy(name)
        if not os.path.exists(path):
            missing.append(name)
            continue
        try:
            mat = np.load(path, mmap_mode="r")
        except (OSError, ValueError):
            missing.append(name)
            continue
        if tuple(mat.shape) != (len(new_dates), len(new_syms)):
            details.append({"factor": name, "reason": "staging 形状不对",
                            "shape": list(mat.shape),
                            "want": [len(new_dates), len(new_syms)]})
            missing.append(name)
    if missing:
        details.append({"reason": "staging 缺因子或形状不对", "factors": missing[:10],
                        "count": len(missing)})
    axis["ok"] = bool(axis["dates"] and axis["symbols"] and not missing)

    # ---- 3) 数值抽样：新行 vs DB ----
    rng = random.Random(20261116)
    total = 0
    worst = 0.0
    val_bad = 0
    delta_rows = int(manifest.get("deltaRows") or 0)
    if delta_rows > 0 and staged_factors:
        per_factor = max(1, sample // max(1, len(staged_factors)))
        for name in staged_factors:
            if name in missing:
                continue
            mat = np.load(_stage_npy(name), mmap_mode="r")
            sql = ("SELECT date, symbol, value FROM factor_panel WHERE factor = ? "
                   "AND date IN (%s)" % ",".join("?" * len(tail_dates)))
            rows_db = conn.execute(sql, [name] + list(tail_dates)).fetchall()
            if not rows_db:
                continue
            picks = rng.sample(range(len(rows_db)), min(per_factor, len(rows_db)))
            sym_pos = {s: i for i, s in enumerate(new_syms)}
            date_pos = {d: rows + i for i, d in enumerate(tail_dates)}
            for i in picks:
                d, s, want = rows_db[i]
                di, si = date_pos.get(d), sym_pos.get(s)
                if di is None or si is None:
                    continue
                got = float(mat[di, si])
                total += 1
                if not np.isfinite(got):
                    val_bad += 1
                    details.append({"factor": name, "date": d, "symbol": s,
                                    "reason": "缓存是 NaN 但库里有值", "db": want})
                    continue
                err = abs(got - float(want)) / max(abs(float(want)), 1e-12)
                worst = max(worst, err)
                if err > rtol:
                    val_bad += 1
                    details.append({"factor": name, "date": d, "symbol": s,
                                    "db": float(want), "cache": got, "relErr": err})
            del mat

    # ---- 4) 旧块保真（抽样行 + 抽样列） ----
    old_bad = 0
    if old_dates and staged_factors:
        for name in staged_factors:
            if name in missing:
                continue
            old = _open_matrix(old_meta, name)
            if old is None:
                continue
            new = np.load(_stage_npy(name), mmap_mode="r")
            n_probe = min(8, rows)
            for ri in rng.sample(range(rows), n_probe):
                # equal_nan=True 是必须的：矩阵里大量格子本来就是 NaN（停牌/上市不足），
                # np.array_equal 默认认为 NaN != NaN，会把「未改动」误判成「被改动」。
                if not np.array_equal(np.asarray(new[ri, :cols]),
                                      np.asarray(old[ri, :]), equal_nan=True):
                    old_bad += 1
                    details.append({"factor": name, "row": ri,
                                    "reason": "旧块被改动（该行与旧缓存不一致）"})
                    break
            del new, old

    result["checked"] = total
    result["maxRelErr"] = worst
    result["mismatch"] = int(val_bad + old_bad + d_bad + s_bad
                             + (0 if db_tail == tail_dates else 1)
                             + len(missing))
    result["ok"] = bool(axis["ok"] and val_bad == 0 and old_bad == 0 and total > 0)
    result["elapsed"] = round(time.time() - t0, 2)

    if mode == "full":
        full = _axis_check(conn, new_meta, mode="full")
        result["axisFull"] = full
        if not full.get("ok"):
            result["ok"] = False
            result["mismatch"] = int(result["mismatch"] + full.get("dateMismatch", 0)
                                     + full.get("symbolMismatch", 0))
            details.append({"reason": "全量轴校验未通过", "axisFull": full})
    return result


def commit_stage() -> Dict[str, Any]:
    """把 staging 发布为正式缓存。

    顺序硬约束：**先 24 个 npy 各自 os.replace，最后 meta.json**。
    meta 是唯一的「提交指针」—— 在它被替换之前，任何读缓存的进程看到的都是
    旧轴 + 旧 npy 的完整组合；npy 已换新而 meta 尚旧的窗口里，`load()` 的
    形状校验会拒绝这些新矩阵（新轴更长），返回 None 而不是错位数据。

    staging 不完整（manifest 缺因子 / 文件缺失）时**拒绝提交**并返回 ok=False。
    """
    t0 = time.time()
    new_meta, manifest = _stage_state()
    if not new_meta or not manifest:
        return {"ok": False, "reason": "staging 不存在或不完整", "committed": []}

    want = [f.get("factor") for f in (manifest.get("factors") or [])]
    miss = [n for n in want if not os.path.exists(_stage_npy(n))]
    if miss:
        return {"ok": False, "reason": "staging 文件缺失，拒绝提交",
                "missing": miss, "committed": []}

    committed: List[str] = []
    for name in want:
        os.replace(_stage_npy(name), _npy_path(name))
        committed.append(name)
    # meta 最后替换：它是唯一提交指针
    os.replace(_stage_meta_path(), _meta_path())
    for extra in (_stage_manifest_path(),):
        try:
            os.remove(extra)
        except OSError:
            pass
    return {"ok": True, "committed": committed,
            "elapsed": round(time.time() - t0, 2)}


def abort_stage() -> Dict[str, Any]:
    """丢弃 staging（正式缓存与 meta.json 完全不动）。"""
    sdir = _stage_dir()
    existed = os.path.isdir(sdir)
    if existed:
        shutil.rmtree(sdir, ignore_errors=True)
    return {"ok": True, "removed": existed, "stageDir": sdir}


# ────────────────────────────────────────────────────────────
# 校验 / 概览 / 清理
# ────────────────────────────────────────────────────────────
def _axis_check(conn: sqlite3.Connection, meta: Dict[str, Any],
                mode: str = "full") -> Dict[str, Any]:
    """轴逐项比对（`mode="full"` 才查库的 DISTINCT，约 25s）。

    为什么必须**逐项**比而不是比集合：集合相等无法发现「轴被重排/反转」。
    轴一旦错位，npy 里的每一个数都被错误的日期解释，而且不会报任何错。
    """
    dates = list(meta.get("dates") or [])
    syms = list(meta.get("symbols") or [])
    out: Dict[str, Any] = {"ok": False, "dates": False, "symbols": False, "mode": mode,
                           "dateMismatch": 0, "symbolMismatch": 0,
                           "tailPendingDates": 0, "tailPendingSymbols": 0}
    if not dates or not syms:
        out["reason"] = "meta 里没有轴"
        return out

    # 单调性（纯内存，0s）：反转/重排立刻被抓
    mono_d = all(dates[i] < dates[i + 1] for i in range(len(dates) - 1))
    mono_s = all(syms[i] < syms[i + 1] for i in range(len(syms) - 1))
    if not mono_d:
        out["dateMismatch"] = sum(1 for i in range(len(dates) - 1) if dates[i] >= dates[i + 1])
    if not mono_s:
        out["symbolMismatch"] = sum(1 for i in range(len(syms) - 1) if syms[i] >= syms[i + 1])

    if mode != "full":
        out["dates"] = mono_d
        out["symbols"] = mono_s
        out["ok"] = bool(mono_d and mono_s)
        return out

    db_dates = [r[0] for r in conn.execute(
        "SELECT DISTINCT date FROM factor_panel ORDER BY date ASC")]
    db_syms = [r[0] for r in conn.execute(
        "SELECT DISTINCT symbol FROM factor_panel ORDER BY symbol ASC")]

    d_bad = 0
    for i in range(min(len(db_dates), len(dates))):
        if db_dates[i] != dates[i]:
            d_bad += 1
    if len(dates) > len(db_dates):
        d_bad += len(dates) - len(db_dates)          # 轴里有库里没有的日期
    tail_dates = db_dates[len(dates):] if len(db_dates) > len(dates) else []

    s_bad = 0
    for i in range(min(len(db_syms), len(syms))):
        if db_syms[i] != syms[i]:
            s_bad += 1
    if len(syms) > len(db_syms):
        s_bad += len(syms) - len(db_syms)
    tail_syms = db_syms[len(syms):] if len(db_syms) > len(syms) else []

    out["dateMismatch"] = int(out["dateMismatch"] + d_bad)
    out["symbolMismatch"] = int(out["symbolMismatch"] + s_bad)
    out["tailPendingDates"] = len(tail_dates)
    out["tailPendingSymbols"] = len(tail_syms)
    out["dates"] = bool(mono_d and d_bad == 0)
    out["symbols"] = bool(mono_s and s_bad == 0)
    out["ok"] = bool(out["dates"] and out["symbols"])
    return out


def verify(conn: sqlite3.Connection, names: Optional[Iterable[str]] = None,
           n: int = 120, rtol: float = 1e-4, mode: str = "tail") -> Dict[str, Any]:
    """抽格子回查数据库，确认缓存里的数值真的对得上；并做轴逐项比对。

    这道检查不是可选项。.npy 里没有列名，行列的含义完全靠 meta 的轴来解释，
    轴一旦错位不会报任何错 —— 它只会安静地给出错误的因子值，下游据此算出
    一个更漂亮的 IC，然后被当成真的。2026-11-15 就发生过一次：轴被重排后
    缓存与库的相对差中位数到 0.85，而整条链路毫无察觉。

    Args:
        names: 要校验的因子；None 表示 meta 里已有的全部因子。
        n: 每个因子抽多少个格子回查。
        rtol: 相对误差容忍度。
        mode: "tail" 只做内存单调性 + 抽样回查（约 1~2s，日常用）；
              "full" 额外全扫 DISTINCT date/symbol 做轴逐项比对（约 25s，每周/上线前用）。

    Returns:
        {"ok": bool, "checked": 格子数, "maxRelErr": 最大相对误差,
         "mismatch": 数值不一致数 + 轴逐项不一致数, "axis": {...}, "details": [...]}
    """
    import random

    t0 = time.time()
    rng = random.Random(20261115)
    meta = _load_meta()
    if not meta or not meta.get("axisFixed"):
        return {"ok": False, "checked": 0, "maxRelErr": 0.0, "mismatch": 1,
                "reason": "没有可用的缓存轴", "axis": {"ok": False, "mode": mode},
                "details": [{"reason": "没有可用的缓存轴"}]}

    axis = _axis_check(conn, meta, mode=mode)
    want = list(names) if names else sorted((meta.get("factors") or {}).keys())

    total, worst, bad = 0, 0.0, []
    for name in want:
        # 刻意用 _open_matrix（不做 MIN(date) 守卫）：verify 的职责是「如实比对」，
        # 即使缓存已经不该被使用，也要把不一致**量化**出来，而不是简单返回 None。
        mat = _open_matrix(meta, name)
        if mat is None:
            bad.append({"factor": name, "reason": "缓存缺失或形状不符"})
            continue
        finite = np.isfinite(np.asarray(mat))
        n_finite = int(finite.sum())
        if n_finite == 0:
            bad.append({"factor": name, "reason": "缓存全空"})
            continue
        flat_idx = np.flatnonzero(finite)
        picks = rng.sample(range(len(flat_idx)), min(n, len(flat_idx)))
        dates = meta["dates"]
        syms = meta["symbols"]
        for i in picks:
            pos = int(flat_idx[i])
            ri, ci = divmod(pos, mat.shape[1])
            date, sym = dates[ri], syms[ci]
            want_v = float(mat[ri, ci])
            row = conn.execute(
                "SELECT value FROM factor_panel WHERE date=? AND symbol=? AND factor=?",
                (date, sym, name)).fetchone()
            if row is None:
                bad.append({"factor": name, "date": str(date), "symbol": sym,
                            "reason": "库里查不到"})
                continue
            got = float(row[0])
            total += 1
            err = abs(got - want_v) / max(abs(got), 1e-12)
            worst = max(worst, err)
            if err > rtol:
                bad.append({"factor": name, "date": str(date), "symbol": sym,
                            "db": got, "cache": want_v, "relErr": err})

    mismatch = len(bad) + int(axis.get("dateMismatch") or 0) + int(axis.get("symbolMismatch") or 0)
    return {"ok": bool(axis.get("ok") and not bad and total > 0),
            "checked": total, "maxRelErr": worst, "mismatch": mismatch,
            "axis": axis, "details": bad[:10], "elapsed": round(time.time() - t0, 2)}


def coverage(conn: Optional[sqlite3.Connection] = None,
             required: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """24 个因子的缓存齐备性断言（生产任务启动时先跑这一步）。

    不齐就 `exit 2`（见 run_factor_pipeline.py）：宁可当天不更新，也不能让页面
    拿着「19 个因子的滚动验证结果」当「24 个因子的」用。

    Args:
        conn: 可传 None（纯看磁盘）。传了也不会做任何写操作。
        required: 必需因子名单；None 表示用 meta 里已有的名单（不假设 24 个）。

    Returns:
        {"ok", "required": [...], "present": [...], "missing": [...],
         "orphans": [...], "axis": {"dates": N, "symbols": M}, "stage": {...}}
    """
    meta = _load_meta()
    entries = meta.get("factors") or {}
    n_dates, n_syms = _shape_of(meta)
    need = list(required) if required else registry_factor_names()

    present: List[str] = []
    missing: List[str] = []
    bad_shape: List[str] = []
    for name in need:
        entry = entries.get(name)
        if not entry:
            missing.append(name)
            continue
        path = _npy_path(name)
        if not os.path.exists(path):
            missing.append(name)
            continue
        if int(entry.get("rows", -1)) != n_dates or int(entry.get("cols", -1)) != n_syms:
            bad_shape.append(name)
            continue
        present.append(name)

    # 磁盘上存在但 meta 里没有登记的 npy = 上次中断残留的孤儿，一律不算可用
    on_disk = sorted(f[:-4] for f in os.listdir(CACHE_DIR) if f.endswith(".npy")) \
        if os.path.isdir(CACHE_DIR) else []
    orphans = [f for f in on_disk if f not in entries]

    staged = sorted(f[:-4] for f in os.listdir(_stage_dir()) if f.endswith(".npy")) \
        if os.path.isdir(_stage_dir()) else []

    return {"ok": bool(not missing and not bad_shape and n_dates > 0 and n_syms > 0),
            "required": need, "present": present, "missing": missing,
            "badShape": bad_shape, "orphans": orphans,
            "axis": {"dates": n_dates, "symbols": n_syms,
                     "first": (meta.get("dates") or [None])[0],
                     "last": (meta.get("dates") or [None])[-1]},
            "sig": meta.get("sig"),
            "stage": {"dir": _stage_dir(), "factors": staged, "pending": bool(staged)}}


def info() -> Dict[str, Any]:
    """缓存现状概览（不做任何全表查询，纯看磁盘 + meta.json）。"""
    meta = _load_meta()
    total = 0
    for name in (meta.get("factors") or {}):
        p = _npy_path(name)
        if os.path.exists(p):
            total += os.path.getsize(p)
    n_dates, n_syms = _shape_of(meta)
    on_disk = sorted(f[:-4] for f in os.listdir(CACHE_DIR) if f.endswith(".npy")) \
        if os.path.isdir(CACHE_DIR) else []
    return {"sig": meta.get("sig"),
            "fingerprint": meta.get("sig") or meta.get("fingerprint"),
            "legacyMeta": bool(not meta.get("sig") and meta.get("fingerprint")),
            "factors": len(meta.get("factors") or {}),
            "dates": n_dates,
            "symbols": n_syms,
            "sizeMB": round(total / 1e6, 1),
            "orphans": [f for f in on_disk if f not in (meta.get("factors") or {})],
            "stage": {"dir": _stage_dir(),
                      "pending": os.path.isdir(_stage_dir())},
            "dir": CACHE_DIR}


def clear() -> None:
    """删掉 meta 登记的全部 npy + meta.json + staging。

    **不要在生产目录上随手跑**：24 个因子重建约 84 分钟。
    """
    meta = _load_meta()
    for name in (meta.get("factors") or {}):
        p = _npy_path(name)
        if os.path.exists(p):
            os.remove(p)
    if os.path.exists(_meta_path()):
        os.remove(_meta_path())
    abort_stage()


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────
def _cli_append(db: str, dates: str, factors: str, commit: bool) -> int:
    """手动尾部追加（冷启动后的补做路径，日常由 run_factor_pipeline.py 调用）。"""
    conn = sqlite3.connect(db, timeout=120)
    try:
        target = [d.strip() for d in dates.split(",") if d.strip()]
        fnames = [f.strip() for f in factors.split(",") if f.strip()] or None
        st = stage_append(conn, target, fnames,
                          progress=lambda i, n, name: print("    staging %-16s (%d/%d)"
                                                            % (name, i + 1, n), flush=True))
        print("staging 完成：轴 %d 天 × %d 只（+%d 天 / +%d 只），%d 个因子，%.1fs"
              % (st["axisDates"], st["axisSymbols"], st["deltaDates"], st["deltaSymbols"],
                 len(st["factors"]), st["elapsed"]))
        vf = verify_stage(conn, mode="tail")
        print("verify_stage：抽 %d 格，最大相对误差 %.2e，不一致 %d → %s"
              % (vf["checked"], vf["maxRelErr"], vf["mismatch"],
                 "通过" if vf["ok"] else "不通过"))
        if not vf["ok"] or not commit:
            print(json.dumps(vf, ensure_ascii=False, indent=2)[:2000])
            abort_stage()
            print("未提交，staging 已丢弃（正式缓存未改动）")
            return 0 if not vf["ok"] else 0
        cr = commit_stage()
        print("提交：%d 个因子已替换，meta.json 最后替换，%.1fs"
              % (len(cr["committed"]), cr["elapsed"]))
        return 0 if cr["ok"] else 2
    finally:
        conn.close()


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="因子面板缓存管理")
    ap.add_argument("--db", default="data/sequoia_v2.db")
    ap.add_argument("--factors", default="", help="逗号分隔；默认全部")
    ap.add_argument("--info", action="store_true", help="只看缓存现状")
    ap.add_argument("--sig", action="store_true", help="打印结构签名")
    ap.add_argument("--migrate", action="store_true",
                    help="把旧 fingerprint 就地迁移成结构签名（不动任何 npy）")
    ap.add_argument("--build-all", action="store_true",
                    help="补齐库里全部因子的缓存（冷启动用，约 60~90 分钟，勿进定时任务）")
    ap.add_argument("--coverage", action="store_true", help="24 因子齐备性断言")
    ap.add_argument("--tail", action="store_true", help="只看尾部增量（新日期/新代码）")
    ap.add_argument("--append", default="", help="把指定日期（逗号分隔）追加进缓存")
    ap.add_argument("--no-commit", action="store_true", help="--append 时只 staging 不提交")
    ap.add_argument("--verify", action="store_true", help="抽格子回查数据库核对数值")
    ap.add_argument("--verify-mode", choices=["tail", "full"], default="tail")
    ap.add_argument("--clear", action="store_true", help="清空缓存（危险，24 因子重建 84 分钟）")
    args = ap.parse_args(argv)

    if args.clear:
        clear()
        print("已清空 ", CACHE_DIR)
        return 0
    if args.info:
        print(json.dumps(info(), ensure_ascii=False, indent=2))
        return 0

    if args.append:
        return _cli_append(args.db, args.append, args.factors, not args.no_commit)

    conn = sqlite3.connect(args.db, timeout=60)
    try:
        if args.sig:
            print(structure_sig(conn))
            return 0
        if args.migrate:
            sig = structure_sig(conn)
            vst = validate_structure(conn)
            if not vst["ok"]:
                print("库里因子集合与 registry 不一致，拒绝迁移：库有registry无 %s；"
                      "registry有库无 %s" % (vst["dbOnly"], vst["registryOnly"]))
                return 2
            meta = _load_meta()
            if meta.get("sig") == sig:
                print("结构签名已是最新，无需迁移：%s" % sig)
                return 0
            if _migrate_legacy_meta(conn, meta, sig):
                print("已把旧指纹迁移为结构签名（现有缓存全部保留）：%s" % sig)
                return 0
            print("旧指纹与当前库结构不一致，不能迁移（需重建缓存）。"
                  "当前 sig=%s；meta 里记的是 %r"
                  % (sig, meta.get("fingerprint") or meta.get("sig")))
            return 2
        if args.coverage:
            res = coverage(conn, [s for s in args.factors.split(",") if s] or None)
            print(json.dumps(res, ensure_ascii=False, indent=2))
            return 0 if res["ok"] else 2
        if args.tail:
            print(json.dumps(tail_delta(conn), ensure_ascii=False, indent=2))
            return 0
        if args.verify:
            cached = sorted((_load_meta().get("factors") or {}))
            if not cached:
                print("没有缓存可校验")
                return 1
            res = verify(conn, cached, mode=args.verify_mode)
            print(json.dumps(res, ensure_ascii=False, indent=2)[:2000])
            return 0 if res["ok"] else 1

        t0 = time.time()
        want = [s for s in args.factors.split(",") if s]
        if args.build_all and not want:
            import factor_eval as _fe    # noqa: F401  延迟导入，仅为确认环境可用
            from factors import registry as _reg
            want = _reg.all_names()
            print("补齐全部 %d 个因子缓存（每个约 3~5 分钟，预估 60~90 分钟）…"
                  % len(want), flush=True)
        res = ensure(conn, want)
        print("新建 %d 个、跳过 %d 个、总耗时 %.0fs" %
              (len(res["built"]), len(res["skipped"]), time.time() - t0))
        print(json.dumps(info(), ensure_ascii=False, indent=2))

        # 建完必须自检：抽查刚写入的因子，数值对不上立刻报错，别让错数据流到下游
        if res["built"]:
            chk = verify(conn, res["built"][:3], mode="tail")
            print("自检：抽查 %d 格，最大相对误差 %.2e，不一致 %d 个 → %s"
                  % (chk["checked"], chk["maxRelErr"], chk["mismatch"],
                     "通过" if chk["ok"] else "不通过"))
            if not chk["ok"]:
                print(json.dumps(chk["details"], ensure_ascii=False, indent=2))
                print("\n缓存与库不一致，已作废缓存，请重跑。")
                clear()
                return 2
        return 0
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
