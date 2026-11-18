#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_ts.py — 逐日 IC 与逐日 10 组收益的时间序列持久化（`data/factor_ts/`）

它解决什么问题
--------------
滚动验证（`rolling_eval.py`）要在每个时点 T 上「只用 `date <= T-horizon` 的
证据」重新挑一遍因子。如果每个时点都重算 24 因子 × 1420 天的横截面，
一次滚动验证就是小时级；而逐日 IC 一旦算出就**稳定**（它只是当日横截面的
相关，不随后续新增数据变化），所以可以一次算完、逐期切片。

于是本模块把「逐日 IC + 逐日 10 组平均收益」落盘，滚动验证退化成读文件切片。

为什么必须**同时**存 `ic` 与 `groups`（本方案最容易再犯的错）
------------------------------------------------------------
`export_factor_api.verdict_of()` 的判定链是：

    t_nw  ← 逐日 IC 序列可给出               ✔
    mono  ← 需要「10 组平均收益 → 组间 spearman」✘ IC 序列给不出

若只存 `ic`，`mono` 永远是 None，于是**所有统计显著的因子都落到「仅统计显著」**，
选中集合恒为空，而且全链路不报任何错 —— 页面会安静地显示「本期无因子入选」。
本模块的 `WILL_PERSIST = ("ic", "groups")` 与 `run_factor_pipeline.step_ts()`
里的同类断言就是这道防线。

为什么每因子一个独立文件、刻意不做共轴
--------------------------------------
各因子预热期不同（`turn20` 起 2021-01-29、`dist_high250` 起 2021-04-02、
`mom60` 起 2021-05-07）。共轴必然引入 NaN 或被短序列稀释。
独立轴后「最近 120 日」在**各自轴上**切片；可用天数 < 20 的期由滚动验证判
`unknown` 且不入选，与 `recent_validity` 的阈值一致。

落盘结构
--------
    data/factor_ts/<factor>.npz    dates(str U10) | ic(float32[n]) | groups(float32[n,10])
    data/factor_ts/meta.json       见 `_new_meta()`，含每因子状态与对账段

不变式（`ensure()` 逐条检查）
    * `dates` 严格递增
    * `len(ic) == len(dates)`，`groups.shape == (len(dates), nGroups)`
    * `dates[-1] <= panelMaxDate - horizon`（约束 A 的数据层探针）

失效与重建矩阵（说明书 §5.6，6 条）
    1. 口径变了（horizon/entry/minStocks/nGroups）→ 全量重建
    2. `panel_rebuild.json` 的 rebuildId 变了        → 全量重建
    3. 因子集合哈希变了                              → 全量重建
    4. 面板最早日期变了                              → 全量重建
    5. 单因子文件缺失 / 轴非递增 / 长度不符          → **只重建该因子**
    6. `ts.last > panelMaxDate - horizon`            → **保护式重算**，禁止直接截断

第 6 条为什么不直接截断
----------------------
「截断 → 重算」不可逆：判定逻辑一旦有 bug，正确数据就被自己销毁了。
所以先从 `.bak` 留退路，重算结果与 `.bak` 在重叠区间逐日比对（容差 1e-5）：
一致才 `os.replace` 替换并删 `.bak`；不一致则正式文件**保持不动**、保留 `.bak`、
把该因子标成 `state="suspect"`（滚动验证跳过它、页面标注「数据可疑，已跳过」）。
全程不丢数据、不补 0、不报错。

入口断言（共享知识第 15 条）
---------------------------
`ensure()` 进门先 `panel_cache.coverage()`，24 个因子缓存不齐直接失败
（本模块抛 `CacheIncomplete`，退出码 2）。原因不是「慢一点」而是**静默降级**：
`panel_cache.load()` 未命中返回 None 后，`factor_eval.load_panel_wide()` 会
静默回落逐因子 SQL（秒级 → 分钟级）且不报错。「只是变慢」是最难查的一类故障，
必须让它变成「直接失败」。

用法
----
    python factor_ts.py --info                 # 只看现状（不查大表）
    python factor_ts.py --build                # 初始化（约 100 秒，需 24 因子缓存齐备）
    python factor_ts.py --reconcile            # 只与 factor_eval.json 对账
    # 日常由 run_factor_pipeline.py 的 step_ts 调用 ensure() + append()
"""

from __future__ import annotations

import json
import os
import shutil
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import factor_run as FR                        # noqa: E402
import panel_cache as PC                       # noqa: E402
from factors import registry                   # noqa: E402

DB = os.path.join(HERE, "data", "sequoia_v2.db")
TS_DIR_DEFAULT = os.path.join(HERE, "data", "factor_ts")
META_NAME = "meta.json"
EVAL_PATH_DEFAULT = os.path.join(HERE, "data", "factor_eval.json")
REBUILD_INFO_DEFAULT = os.path.join(HERE, "data", "panel_rebuild.json")

# 落盘的原始量（缺一个就等于滚动验证失效，见模块 docstring）
WILL_PERSIST: Tuple[str, str] = ("ic", "groups")

# 口径常量默认值：全部可由调用方覆盖，且必须原样写进 meta（用于失效判定第 1 条）
DEFAULTS: Dict[str, Any] = {
    "horizon": 5,          # IC 的前瞻期（交易日）
    "entry": "s1_open",    # 建仓口径：T+1 开盘（与 factor_eval 一致）
    "minStocks": 100,      # 逐日横截面最小有效样本（决策 9：统一 100，不用存档的 30）
    "nGroups": 10,         # 分组数
}

# 与 factor_eval.json 对账的容差。
#
# ⚠ 这两个阈值解决的是**两个不同问题**，别混用：
#   RECONCILE_NUM_RTOL：跨口径比对的**数值**阈值（存档 min_stocks=30 vs 本模块 100）
#   PROTECT_RTOL      ：同口径比对的**数值**阈值（`.bak` 重叠区逐日自比，见第 6 条保护式重算）
#
# 命名说明：这里刻意叫 NUM（数值）而不是 CALIBER（口径）。用到它的是 `dataWarn`
# ——「同一天同一因子的数值差多大」；「口径差多少天」是另一件事，叫 `caliberWarn`、
# 用 `EXTRA_PCT_WARN`。两个名字一旦互换，读代码的人会以为 `dataWarn` 是口径告警，
# 而那恰恰是我们反复强调不许混的两类偏差。
#
# 为什么跨口径不能再用 1e-5：两种 min_stocks 下「当日有效标的集合」本来就不同，
# 逐日 IC 必然有差。用 1e-5 去卡一个刻意不同的口径，结果是 dataWarn 永远为真、
# state 永远 warn —— 一条天天亮、永远不灭的告警等于没有告警（就是本文件
# 1055 行下面自己写的「提示变墙纸」那个病，只是病灶在阈值上）。
#
# 阈值依据（2026-11-16 生产实测，24 因子 × 33187 个 (因子,日) 重叠点）：
#     P50 1.02e-07 / P90 1.15e-06 / P99 2.33e-05 / P99.9 5.92e-05 / max 2.04e-04
#     超过 1e-4 的只有 5 点（0.015%），超过 5e-4 的 0 点
# 取 1e-3：是实测最大值的 5 倍（不会因复权漂移偶发跳动而误报），
# 又比「真实数值错误」小两个量级（用例里注入 +0.05 的一天，50 倍越界，必被抓到）。
RECONCILE_NUM_RTOL = 1e-3
# 「存档有、重算无」的天数占比超过这个值 → 口径告警（决策：只告警不重建）
EXTRA_PCT_WARN = 0.05
# 保护式重算的比对容差（**同口径**：同一因子、同一 min_stocks，只是重算一遍）
PROTECT_RTOL = 1e-5
# 序列末端相对面板末端的允许滞后：`>= horizon` 都正常（min_stocks=100 会让
# 个别日子无 IC），只有 `< horizon` 才是实现错误。此处只用于断言左边界。
MIN_LAG = 1


class CacheIncomplete(RuntimeError):
    """24 因子缓存不齐（应 `exit 2`）。

    刻意用异常而不是返回 None：调用方一旦忘了检查返回值，就会静默落到
    「逐因子 SQL 回落」的分钟级路径上，而那**不会报错**。
    """

    exit_code = 2


# ────────────────────────────────────────────────────────────
# 路径与 JSON 小工具
# ────────────────────────────────────────────────────────────
def ts_dir() -> str:
    """`data/factor_ts/`（可用环境变量 `FACTOR_TS_DIR` 覆盖，测试用）。"""
    return os.environ.get("FACTOR_TS_DIR") or TS_DIR_DEFAULT


def ts_path(name: str) -> str:
    """单因子序列文件路径。"""
    return os.path.join(ts_dir(), "%s.npz" % name)


def meta_path() -> str:
    """meta.json 路径。"""
    return os.path.join(ts_dir(), META_NAME)


def rebuild_info_path() -> str:
    """`data/panel_rebuild.json`（由 `rebuild_panel.py` 在替换成功后写出）。"""
    return os.environ.get("PANEL_REBUILD_PATH") or REBUILD_INFO_DEFAULT


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    """`.tmp → os.replace`（共享知识第 7 条：任何时刻磁盘上都是完整 JSON）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load_meta() -> Dict[str, Any]:
    """读 meta.json；不存在/损坏返回空 dict（调用方按「未初始化」处理）。"""
    return _read_json(meta_path())


def save_meta(meta: Dict[str, Any]) -> None:
    """原子写 meta.json。"""
    _write_json_atomic(meta_path(), meta)


# ────────────────────────────────────────────────────────────
# 单因子序列的读写
# ────────────────────────────────────────────────────────────
def _load_series(name: str) -> Optional[Dict[str, Any]]:
    """读单因子序列并做**结构校验**；缺失/损坏/长度不符一律返回 None。

    Returns:
        {"dates": np.ndarray[U10], "ic": np.ndarray[float32],
         "groups": np.ndarray[float32(n,g)]} 或 None
    """
    path = ts_path(name)
    if not os.path.exists(path):
        return None
    try:
        with np.load(path, allow_pickle=False) as z:
            dates = np.asarray(z["dates"]).astype("U10")
            ic = np.asarray(z["ic"], dtype="float32")
            groups = np.asarray(z["groups"], dtype="float32")
    except (OSError, ValueError, KeyError):
        return None
    if dates.ndim != 1 or ic.ndim != 1 or groups.ndim != 2:
        return None
    n = int(dates.size)
    if n == 0 or int(ic.size) != n or int(groups.shape[0]) != n:
        return None
    if n > 1 and not bool(np.all(dates[1:] > dates[:-1])):
        return None                       # 轴必须严格递增（失效矩阵第 5 条）
    return {"dates": dates, "ic": ic, "groups": groups}


def _save_series(name: str, dates: Sequence[str], ic: Sequence[float],
                 groups: Any) -> None:
    """原子写单因子序列（float32，见模块 docstring 的体积估算）。

    `.tmp → os.replace`：写到一半崩掉时，正式文件仍然完整可读。
    """
    directory = ts_dir()
    os.makedirs(directory, exist_ok=True)
    path = ts_path(name)
    tmp = path + ".tmp.npz"
    np.savez(tmp,
             dates=np.asarray([str(d)[:10] for d in dates], dtype="U10"),
             ic=np.asarray(ic, dtype="float32"),
             groups=np.asarray(groups, dtype="float32"))
    os.replace(tmp, path)


def last_date(name: str) -> Optional[str]:
    """单因子序列的最后一天；无序列返回 None。"""
    ser = _load_series(name)
    return None if ser is None else str(ser["dates"][-1])


def groups_mean(groups_2d: Any) -> np.ndarray:
    """逐组跨日平均收益（跳过 NaN 的列）。滚动验证算窗口单调性时用它。

    用 pandas 而不是 `np.nanmean`：全 NaN 的列会让 numpy 发 RuntimeWarning
    并返回 NaN，而「某一组整段没有成员」其实是正常情况（小样本），不该报警。
    """
    arr = np.asarray(groups_2d, dtype="float64")
    if arr.ndim != 2 or arr.shape[0] == 0:
        return np.zeros(arr.shape[1] if arr.ndim == 2 else 0, dtype="float64")
    return pd.DataFrame(arr).mean(axis=0).to_numpy(dtype="float64")


# 向后兼容的私有别名（本模块内部与早期草稿都用下划线版）
_groups_mean = groups_mean


# ────────────────────────────────────────────────────────────
# 面板侧：读宽表 / 读轴（DB 是权威，缓存是加速）
# ────────────────────────────────────────────────────────────
def _load_panel_wide(conn: sqlite3.Connection, name: str) -> Optional[pd.DataFrame]:
    """读单因子宽表。**全模块只在这里读面板**（测试可替换本函数）。

    刻意直接走 `panel_cache.load()`，并在 `ensure()` 入口用 `coverage()`
    断言缓存齐备 —— 不允许静默落到逐因子 SQL（共享知识第 15 条）。
    """
    return PC.load(conn, name)


def panel_max_date(conn: sqlite3.Connection) -> Optional[str]:
    """`SELECT MAX(date) FROM factor_panel`（走索引，实测 0.0s）。

    注意：**不是** `panel_incr.data_through()`（那个是「逐因子 MAX 的最小值」，
    用来定缺口；这里是「面板最晚一天」，用来定 IC 的上界）。两名字义不同，
    不要互相 import。
    """
    row = conn.execute("SELECT MAX(date) FROM factor_panel").fetchone()
    return None if not row or row[0] is None else str(row[0])


def panel_dates(conn: sqlite3.Connection, since: Optional[str] = None) -> List[str]:
    """面板实际日期轴（升序）。不带 `since` 是全表扫（实测 9.6s）。

    这是**权威来源**：滚动验证的期边界、`panelMaxDate - horizon` 的截断点
    都必须用它，不能用 `plan.targetDates`（残日会被 `MIN_SYMBOLS_PER_DAY`
    剔除，两者可能不等）。
    """
    if since:
        rows = conn.execute(
            "SELECT DISTINCT date FROM factor_panel WHERE date > ? ORDER BY date ASC",
            (str(since),)).fetchall()
    else:
        rows = conn.execute(
            "SELECT DISTINCT date FROM factor_panel ORDER BY date ASC").fetchall()
    return [str(r[0]) for r in rows]


def panel_dates_fast(conn: sqlite3.Connection) -> List[str]:
    """面板日期轴的**快速**读取：优先用缓存 meta 的轴（0.03s），并用
    `MAX(date)`（0.0s）交叉确认；不一致则回落全表 `DISTINCT date`（9.6s）。

    为什么可以信任缓存轴：`meta.dates` 本身就是从 `factor_panel` 建出来、
    之后只按 `SELECT DISTINCT date ... WHERE date > 轴末日` 追加的。交叉确认
    只是防「缓存落后于库」这一种情形（例如手工往库里补了行却没跑 step_cache）。
    """
    meta = PC._load_meta() if hasattr(PC, "_load_meta") else {}
    axis = [str(d) for d in (meta.get("dates") or [])]
    if axis:
        try:
            if panel_max_date(conn) == axis[-1]:
                return axis
        except sqlite3.Error:
            pass
    return panel_dates(conn)


def cutoff_date(dates: Sequence[str], horizon: int) -> Optional[str]:
    """面板轴末端往前 `horizon` 个交易日的日期 = 当日能算出 IC 的**最晚**一天。

    例：`dates[-1]=2026-11-13`、`h=5` → `2026-11-06`（与存档 `factor_eval.json`
    的 `ic_series` 末端实测一致，见说明书 §2.1）。
    """
    idx = len(dates) - 1 - int(horizon)
    return None if idx < 0 else str(dates[idx])


def _params_of(meta: Dict[str, Any]) -> Dict[str, Any]:
    """从 meta 取出四个口径字段（缺失回落 DEFAULTS）。"""
    out = dict(DEFAULTS)
    for key in DEFAULTS:
        if meta.get(key) is not None:
            out[key] = meta[key]
    out["horizon"] = int(out["horizon"])
    out["minStocks"] = int(out["minStocks"])
    out["nGroups"] = int(out["nGroups"])
    out["entry"] = str(out["entry"])
    return out


def current_rebuild_id() -> Optional[str]:
    """当前面板重建 ID（`data/panel_rebuild.json` 不存在时为 None）。"""
    info = _read_json(rebuild_info_path())
    rid = info.get("rebuildId")
    return None if not rid else str(rid)


def factor_sig() -> str:
    """因子集合哈希（与 `panel_cache.structure_sig()` 里 `fh` 同一算法）。"""
    return PC.factors_hash(registry.all_names())


# ────────────────────────────────────────────────────────────
# 入口断言
# ────────────────────────────────────────────────────────────
def require_coverage(conn: Optional[sqlite3.Connection] = None,
                     required: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """`panel_cache.coverage()` 断言：24 因子缓存不齐 → 抛 `CacheIncomplete`。

    这是「静默回落」的唯一闸门（共享知识第 15 条）。任何读面板的 T03 步骤
    都必须先过这里，宁可当天不更新，也不能拿着「19 个因子的滚动验证结果」
    当「24 个因子的」用。
    """
    cov = PC.coverage(conn, required)
    if cov.get("ok"):
        return cov
    reasons: List[str] = []
    if cov.get("missing"):
        reasons.append("缺 %d 个（%s）" % (len(cov["missing"]), ",".join(cov["missing"][:6])))
    if cov.get("badShape"):
        reasons.append("形状不符 %d 个（%s）"
                       % (len(cov["badShape"]), ",".join(cov["badShape"][:6])))
    if not (cov.get("axis") or {}).get("dates"):
        reasons.append("缓存轴为空，从未建过缓存")
    raise CacheIncomplete(
        "因子缓存不齐（%d/%d）：%s；请先执行 `python panel_cache.py --build-all`"
        % (len(cov.get("present") or []), len(cov.get("required") or []),
           "；".join(reasons) or "原因未知"))


# ────────────────────────────────────────────────────────────
# 全量重算单因子
# ────────────────────────────────────────────────────────────
def build_series(conn: sqlite3.Connection, name: str, fwd: pd.DataFrame,
                 params: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """按当前口径全量重算单因子的 `ic` 与 `groups`（一次性向量化，非逐日）。

    Args:
        conn: 数据库连接（只读）。
        name: 因子名（必须在 registry 里）。
        fwd: 未来收益宽表（`factor_eval.load_forward_returns` 的产物，
            口径由 `params["entry"]`/`horizon` 决定）。
        params: `{"horizon", "entry", "minStocks", "nGroups"}`。

    Returns:
        `{"dates", "ic", "groups"}`（numpy 数组）或 None（面板无数据）。
        `ic` 是**未乘 direction** 的原始 Spearman IC（共享知识第 12 条）；
        `groups` 内部已按 direction 归正（组 1 = 预期最好）。
    """
    import factor_eval as FE                     # 延迟导入：模块很大，只在需要时用

    direction = int(registry.get(name)["direction"])
    wide = _load_panel_wide(conn, name)
    if wide is None or wide.empty or fwd is None or fwd.empty:
        return None
    min_stocks = int(params["minStocks"])
    n_groups = int(params["nGroups"])

    ic = FE.daily_ic(wide, fwd, min_stocks=min_stocks)
    if ic.empty:
        return None
    groups_df, _members = FE.group_returns(wide, fwd, direction,
                                           n_groups=n_groups, min_stocks=min_stocks)
    # 轴 = 有 IC 的日期；groups 对齐到同一轴（没算出来的格子留 NaN，不补 0）
    aligned = groups_df.reindex(index=ic.index,
                                columns=list(range(1, n_groups + 1)))
    return {"dates": np.asarray([str(d) for d in ic.index], dtype="U10"),
            "ic": ic.to_numpy(dtype="float32"),
            "groups": aligned.to_numpy(dtype="float32")}


# ────────────────────────────────────────────────────────────
# meta 维护
# ────────────────────────────────────────────────────────────
def _new_meta(params: Dict[str, Any], panel_min: Optional[str],
              rebuild_id: Optional[str], source: str) -> Dict[str, Any]:
    """构造一份全新的 meta（字段形状见说明书 §5.5）。"""
    return {
        "horizon": int(params["horizon"]),
        "entry": str(params["entry"]),
        "minStocks": int(params["minStocks"]),
        "nGroups": int(params["nGroups"]),
        "factors": {},
        "sig": {"panelMinDate": panel_min,
                "panelRebuildId": rebuild_id,
                "factorSig": factor_sig()},
        "builtAt": FR.iso_now(),
        "lastAppendRunId": None,
        "lastAppendAt": None,
        "source": source,
        "willPersist": list(WILL_PERSIST),
        "reconcile": {},
    }


def _touch_factor(meta: Dict[str, Any], name: str,
                  dates: Sequence[str]) -> None:
    """按序列的实际轴刷新该因子的登记项（n / first / last）。

    只动 `n/first/last`，**不动** `state` 与 `suspectSince`：
    一个被标成 suspect 的因子，即使后续追加成功也不该自动恢复——
    恢复必须来自一次通过比对的重算（第 6 条），而不是「后来又写成功了一次」。
    """
    entry = dict((meta.get("factors") or {}).get(name) or {})
    n = int(len(dates))
    entry.update({"n": n,
                  "first": str(dates[0]) if n else None,
                  "last": str(dates[-1]) if n else None})
    entry.setdefault("state", "ok")
    entry.setdefault("suspectSince", None)
    meta.setdefault("factors", {})[name] = entry


def _mark_suspect(meta: Dict[str, Any], name: str, reason: str) -> None:
    """把因子标成 suspect（`suspectSince` 只记**首次**被标的那天）。"""
    entry = dict((meta.get("factors") or {}).get(name) or {})
    entry.setdefault("state", "ok")
    entry.setdefault("suspectSince", None)
    if entry["state"] != "suspect":
        entry["state"] = "suspect"
        entry["suspectSince"] = time.strftime("%Y-%m-%d")
    entry["suspectReason"] = str(reason)[:120]
    meta.setdefault("factors", {})[name] = entry


def suspects(meta: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """suspect 因子清单：`[{"factor", "reason", "since"}]`（滚动验证据此跳过）。

    语义（说明书 §5.7）：`since` 是**当前状态**的起点。
    `since` 之前的期该因子正常参与，`since` 及之后的期被跳过——
    这样逐期 `n_factors` 的变化永远可解释。
    """
    info = load_meta() if meta is None else meta
    out: List[Dict[str, Any]] = []
    for name, entry in sorted((info.get("factors") or {}).items()):
        if str((entry or {}).get("state") or "ok") != "suspect":
            continue
        out.append({"factor": name,
                    "reason": "suspect",
                    "since": (entry or {}).get("suspectSince")})
    return out


def states(meta: Optional[Dict[str, Any]] = None) -> Dict[str, str]:
    """因子名 → `ok|suspect`（页面/滚动验证共用）。"""
    info = load_meta() if meta is None else meta
    return {name: str((entry or {}).get("state") or "ok")
            for name, entry in (info.get("factors") or {}).items()}


# ────────────────────────────────────────────────────────────
# 失效判定（§5.6 第 1~4 条）
# ────────────────────────────────────────────────────────────
def invalidation_reasons(meta: Dict[str, Any], params: Dict[str, Any],
                         panel_min: Optional[str],
                         rebuild_id: Optional[str]) -> List[str]:
    """全局失效原因（空列表 = 结构仍然可用，只需尾部追加）。

    覆盖 §5.6 前 4 条。刻意把「哪一条命中」写成可读字符串：出问题时
    维护者需要知道是口径变了还是面板重建了，而不是只知道「要重建」。
    """
    if not meta or not meta.get("factors"):
        return ["factor_ts 尚未初始化"]
    reasons: List[str] = []
    saved = _params_of(meta)
    for key in ("horizon", "entry", "minStocks", "nGroups"):
        if saved[key] != params[key]:
            reasons.append("口径变化 %s: %r → %r" % (key, saved[key], params[key]))
    sig = meta.get("sig") or {}
    if sig.get("panelRebuildId") != rebuild_id:
        reasons.append("面板重建 ID 变化：%r → %r"
                       % (sig.get("panelRebuildId"), rebuild_id))
    if sig.get("factorSig") != factor_sig():
        reasons.append("因子集合变化：%r → %r" % (sig.get("factorSig"), factor_sig()))
    if sig.get("panelMinDate") != panel_min:
        reasons.append("面板最早日期变化：%r → %r" % (sig.get("panelMinDate"), panel_min))
    return reasons


# ────────────────────────────────────────────────────────────
# 第 6 条：保护式重算
# ────────────────────────────────────────────────────────────
def protective_recompute(conn: sqlite3.Connection, name: str, fwd: pd.DataFrame,
                         params: Dict[str, Any], cutoff: str,
                         meta: Dict[str, Any]) -> Dict[str, Any]:
    """`ts.last > panelMax - horizon` 时的处理：留 `.bak` → 重算 → 比对 → 才替换。

    **绝不直接截断**（截断不可逆，判定若有 bug 就销毁了正确数据）。

    Returns:
        {"factor", "action": "replaced"|"suspect"|"failed", "maxAbsDiff", "reason"}
    """
    path = ts_path(name)
    bak = path + ".bak"
    if not os.path.exists(path):
        return {"factor": name, "action": "failed", "maxAbsDiff": None,
                "reason": "序列文件不存在，无法保护式重算"}
    old = _load_series(name)
    try:
        shutil.copy2(path, bak)                  # 第一步永远是留退路
    except OSError as exc:
        return {"factor": name, "action": "failed", "maxAbsDiff": None,
                "reason": "无法创建 .bak：%s" % exc}

    fresh = build_series(conn, name, fwd, params)
    if fresh is None:
        _mark_suspect(meta, name, "保护式重算失败：面板无数据")
        return {"factor": name, "action": "suspect", "maxAbsDiff": None,
                "reason": "重算失败（面板无数据），保留 .bak 与正式文件"}

    # 截到「不超过 cutoff」，再与 .bak 的重叠区逐日比对
    keep = fresh["dates"] <= str(cutoff)
    new_dates = fresh["dates"][keep]
    new_ic = fresh["ic"][keep]
    new_groups = fresh["groups"][keep]

    worst = 0.0
    if old is not None and len(new_dates):
        pos = {str(d): i for i, d in enumerate(new_dates)}
        pairs = [(pos[str(d)], j) for j, d in enumerate(old["dates"]) if str(d) in pos]
        for i_new, i_old in pairs:
            a, b = float(new_ic[i_new]), float(old["ic"][i_old])
            if np.isfinite(a) and np.isfinite(b):
                worst = max(worst, abs(a - b))
            ga = np.asarray(new_groups[i_new], dtype="float64")
            gb = np.asarray(old["groups"][i_old], dtype="float64")
            both = np.isfinite(ga) & np.isfinite(gb)
            if both.any():
                worst = max(worst, float(np.max(np.abs(ga[both] - gb[both]))))
        if not pairs:
            worst = float("inf")                 # 完全没有重叠区：无法确认一致性

    if worst <= PROTECT_RTOL and len(new_dates):
        _save_series(name, new_dates, new_ic, new_groups)
        try:
            os.remove(bak)
        except OSError:
            pass
        _touch_factor(meta, name, [str(d) for d in new_dates])
        (meta["factors"][name])["state"] = "ok"
        (meta["factors"][name])["suspectSince"] = None
        return {"factor": name, "action": "replaced", "maxAbsDiff": worst,
                "reason": "与 .bak 重叠区一致（≤%.0e），已替换并删除 .bak" % PROTECT_RTOL}

    _mark_suspect(meta, name,
                  "保护式重算与旧序列不一致（最大差 %.2e），保留 .bak" % worst)
    return {"factor": name, "action": "suspect", "maxAbsDiff": worst,
            "reason": "不一致（最大差 %.2e）：正式文件与 .bak 都保留，滚动验证将跳过"
                      % worst}


# ────────────────────────────────────────────────────────────
# ensure：入口断言 + 失效矩阵 + 初始化/修复
# ────────────────────────────────────────────────────────────
def ensure(conn: sqlite3.Connection, fwd: pd.DataFrame,
           params: Optional[Dict[str, Any]] = None,
           panel_dates_axis: Optional[Sequence[str]] = None,
           strict: bool = True, verbose: bool = True) -> Dict[str, Any]:
    """确保 `data/factor_ts/` 与当前库、当前口径一致（必要时重建/修复/保护式重算）。

    Args:
        conn: 数据库连接（只读）。
        fwd: 未来收益宽表（由 pipeline 的 `PriceBundle` 注入，**不要自己加载**）。
        params: 口径；None 用 DEFAULTS。
        panel_dates_axis: 面板日期轴；None 时自动取（优先缓存轴，见 `panel_dates_fast`）。
        strict: 是否强制 `coverage()` 断言（默认 True）。**只有临时库的测试**可以关掉，
            生产路径关掉它等于允许「静默回落到分钟级」。
        verbose: 打印进度。

    Returns:
        {"ok", "action": "init|rebuild|repair|noop", "reasons", "rebuilt",
         "repaired", "protective", "suspects", "params", "elapsed", "axis"}
    """
    t0 = time.time()
    prm = dict(DEFAULTS)
    prm.update({k: v for k, v in (params or {}).items() if k in DEFAULTS})
    prm["horizon"] = int(prm["horizon"])
    prm["minStocks"] = int(prm["minStocks"])
    prm["nGroups"] = int(prm["nGroups"])
    prm["entry"] = str(prm["entry"])

    if strict:
        require_coverage(conn)                   # 不齐 → CacheIncomplete(exit 2)
    if fwd is None or fwd.empty:
        raise ValueError("ensure() 需要 fwd（未来收益宽表），收到空表")

    axis = [str(d) for d in (panel_dates_axis or panel_dates_fast(conn))]
    if not axis:
        raise ValueError("factor_panel 里没有日期，无法初始化 factor_ts")
    panel_min = axis[0]
    cutoff = cutoff_date(axis, prm["horizon"])
    rebuild_id = current_rebuild_id()

    meta = load_meta()
    reasons = invalidation_reasons(meta, prm, panel_min, rebuild_id)
    names = registry.all_names()
    rebuilt: List[str] = []
    repaired: List[str] = []
    protective: List[Dict[str, Any]] = []
    failed: List[Dict[str, str]] = []

    if reasons:
        action = "init" if not meta else "rebuild"
        if verbose:
            print("  factor_ts %s：%s" % (action, "；".join(reasons)), flush=True)
        meta = _new_meta(prm, panel_min, rebuild_id, source=action)
        for i, name in enumerate(names, start=1):
            got = build_series(conn, name, fwd, prm)
            if got is None:
                failed.append({"factor": name, "reason": "面板无数据"})
                _mark_suspect(meta, name, "全量重建时面板无数据")
                if verbose:
                    print("      [%2d/%d] %-14s 面板无数据" % (i, len(names), name),
                          flush=True)
                continue
            _save_series(name, got["dates"], got["ic"], got["groups"])
            _touch_factor(meta, name, [str(d) for d in got["dates"]])
            rebuilt.append(name)
            if verbose:
                print("      [%2d/%d] %-14s %4d 天  %s ~ %s"
                      % (i, len(names), name, len(got["dates"]),
                         got["dates"][0] if len(got["dates"]) else "-",
                         got["dates"][-1] if len(got["dates"]) else "-"), flush=True)
        # 一个都没建成 → **不要**落 meta。落下去就等于告诉全世界「24 个因子已初始化」，
        # 实际状态是「24 个可疑、0 条数据」：页面会显示一排可疑因子，
        # rolling 会把它们全跳过，`compact()` 还会显示 `initialized=true`。
        # 诚实的表达是「未初始化」，让下一次运行走 init 分支重来。
        if not rebuilt:
            raise CacheIncomplete(
                "factor_ts 初始化失败：24 个因子序列一个都建不出来（0/%d）；"
                "常见原因是当日横截面标的数 < minStocks=%d，或面板起点晚于最长预热窗口"
                % (len(names), prm["minStocks"]))
        meta["lastAppendRunId"] = None
        save_meta(meta)
    else:
        # 结构可用：只做第 5 条（单因子损坏）与第 6 条（越界保护式重算）
        action = "noop"
        for name in names:
            ser = _load_series(name)
            expected_cols = prm["nGroups"]
            broken = (ser is None
                      or int(ser["groups"].shape[1]) != expected_cols
                      or len(ser["ic"]) != len(ser["dates"]))
            if broken:
                got = build_series(conn, name, fwd, prm)
                if got is None:
                    failed.append({"factor": name, "reason": "面板无数据"})
                    _mark_suspect(meta, name, "单因子修复失败：面板无数据")
                    continue
                _save_series(name, got["dates"], got["ic"], got["groups"])
                _touch_factor(meta, name, [str(d) for d in got["dates"]])
                repaired.append(name)
                action = "repair"
                if verbose:
                    print("  factor_ts 修复 %s（文件缺失或长度不符）" % name, flush=True)

        # 第 6 条：末端越过「能算出 IC 的最晚一天」→ 保护式重算
        if cutoff:
            for name in names:
                last = last_date(name)
                if last is None or last <= cutoff:
                    continue
                res = protective_recompute(conn, name, fwd, prm, cutoff, meta)
                protective.append(res)
                action = "repair" if action == "noop" else action
                if verbose:
                    print("  factor_ts 第6条 %s：last=%s > %s → %s"
                          % (name, last, cutoff, res["action"]), flush=True)
        save_meta(meta)

    # 后置断言：构建「跑完」不等于「可读」。`build_series()` 返回 None（面板无数据，
    # 典型是当日横截面标的数 < minStocks，或面板起点晚于该因子的预热窗口）时，
    # 上面只记 `failed` + 标 suspect；若不在这里升级成异常，调用方会拿着 0 个可用
    # 序列继续跑 append，最后抛的是「所有因子序列都不可读」—— 那是症状，不是病因，
    # 排障方向会被带偏到「文件权限/损坏」上去。
    usable = [name for name in names if _load_series(name) is not None]
    if not usable:
        raise CacheIncomplete(
            "factor_ts 没有任何可用序列（0/%d）：%s；"
            "常见原因是当日横截面标的数 < minStocks=%d，或面板起点晚于最长预热窗口"
            % (len(names),
               "、".join("%s=%s" % (f["factor"], f["reason"]) for f in failed[:4])
               or "原因未知",
               prm["minStocks"]))

    reconcile = reconcile_with_eval(params=prm, meta=meta)
    if verbose:
        print("  对账：state=%s 重叠 %d 个(因子,日) 最大差 %.2e 口径差 %d 天(%.1f%%)"
              % (reconcile["state"], reconcile["commonFactorDays"],
                 reconcile["maxAbsDiff"], reconcile["extraDays"],
                 reconcile["extraPct"] * 100), flush=True)
    return {"ok": True, "action": action, "reasons": reasons,
            "rebuilt": rebuilt, "repaired": repaired, "protective": protective,
            "failed": failed, "suspects": suspects(meta), "params": prm,
            "reconcile": reconcile,
            "axis": {"dates": len(axis), "first": axis[0], "last": axis[-1],
                     "cutoff": cutoff},
            "elapsed": round(time.time() - t0, 2)}


# ────────────────────────────────────────────────────────────
# append：每日追加
# ────────────────────────────────────────────────────────────
def _actual_dates(conn: sqlite3.Connection, after: str,
                  until: str) -> List[str]:
    """库里**真实存在**的日期并集 `after < date <= until`（升序）。

    这是「`ts` 每日追加的日期集必须来自 `factor_panel` 实际落库日期」的落地点
    （不能取 `plan.targetDates`：残日会被 `MIN_SYMBOLS_PER_DAY` 剔除，
    按 plan 追加会让被剔除那天拿到全 NaN 横截面 → IC 算不出或更糟：
    算出个空集却不报错）。范围扫描实测 2.2s，一天一次。
    """
    rows = conn.execute(
        "SELECT DISTINCT date FROM factor_panel WHERE date > ? AND date <= ? "
        "ORDER BY date ASC", (str(after), str(until))).fetchall()
    return [str(r[0]) for r in rows]


def append(conn: sqlite3.Connection, fwd: pd.DataFrame,
           panel_dates_axis: Optional[Sequence[str]] = None,
           run_id: Optional[str] = None, verbose: bool = True) -> Dict[str, Any]:
    """把 `data/factor_ts/` 追到「面板最新 − horizon」为止（逐因子独立轴）。

    追加区间：`last < d <= panelMaxDate - horizon`。最后 `horizon` 天算不出 IC
    是**必然**（缺未来收益）：不追加行、不补 0、不报错，序列自然终止。

    Args:
        conn: 数据库连接。
        fwd: 未来收益宽表（pipeline 注入，勿自行加载）。
        panel_dates_axis: 面板日期轴；None 时自动取。
        run_id: 本次运行标识（写进 meta.lastAppendRunId）。
        verbose: 打印进度。

    Returns:
        {"ok", "appended", "factors", "skipped", "failed", "perFactor",
         "unexpectedDates", "elapsed"}
        `skipped` 只表示「因 `> panelMax - horizon` 而算不出 IC 的日期」
        （suspect 因子的跳过记录在 `rolling.skippedFactors`，两者含义不同）。
    """
    import factor_eval as FE

    t0 = time.time()
    meta = load_meta()
    if not meta or not meta.get("factors"):
        return {"ok": False, "reason": "factor_ts 尚未初始化，先调用 ensure()",
                "appended": 0, "factors": 0, "skipped": [], "failed": []}
    prm = _params_of(meta)
    axis = [str(d) for d in (panel_dates_axis or panel_dates_fast(conn))]
    cutoff = cutoff_date(axis, prm["horizon"])
    if cutoff is None:
        return {"ok": True, "appended": 0, "factors": len(meta["factors"]),
                "skipped": list(axis), "failed": [], "perFactor": {},
                "unexpectedDates": [], "elapsed": round(time.time() - t0, 2),
                "reason": "面板天数不足 horizon+1，暂无可用期"}

    skipped = [d for d in axis if d > cutoff]
    names = [n for n in registry.all_names() if n in (meta.get("factors") or {})]
    per_factor: Dict[str, int] = {}
    appended_total = 0
    failed: List[Dict[str, str]] = []
    unexpected: List[str] = []

    # 全部因子里「最落后」的末端 → 一次范围查询拿到候选日期
    lasts = [(n, last_date(n)) for n in names]
    valid_lasts = [d for _, d in lasts if d]
    if not valid_lasts:
        return {"ok": False, "reason": "所有因子序列都不可读，先跑 ensure()",
                "appended": 0, "factors": len(names), "skipped": skipped,
                "failed": [{"factor": n, "reason": "序列不可读"} for n in names]}
    tail_after = min(valid_lasts)
    candidates = _actual_dates(conn, tail_after, cutoff) if tail_after < cutoff else []
    if not candidates:
        if verbose:
            print("  factor_ts 已是最新（最落后因子止于 %s，可用上界 %s）"
                  % (tail_after, cutoff), flush=True)
        _write_append_meta(meta, run_id)
        return {"ok": True, "appended": 0, "factors": len(names),
                "skipped": skipped, "failed": [], "perFactor": per_factor,
                "unexpectedDates": unexpected,
                "elapsed": round(time.time() - t0, 2)}

    known = set(axis)
    unexpected = [d for d in candidates if d not in known]

    for name in names:
        last = last_date(name)
        ser = _load_series(name)
        if last is None or ser is None:
            failed.append({"factor": name, "reason": "序列不可读"})
            continue
        targets = [d for d in candidates if d > last]
        if not targets:
            continue
        wide = _load_panel_wide(conn, name)
        if wide is None or wide.empty:
            failed.append({"factor": name, "reason": "面板缓存缺失"})
            continue
        sub = wide.reindex(index=pd.Index(targets, name="date"))
        ic = FE.daily_ic(sub, fwd, min_stocks=prm["minStocks"])
        direction = int(registry.get(name)["direction"])
        groups_df, _m = FE.group_returns(sub, fwd, direction,
                                         n_groups=prm["nGroups"],
                                         min_stocks=prm["minStocks"])
        if ic.empty:
            failed.append({"factor": name,
                           "reason": "%d 天均无有效 IC（样本 < %d）"
                                     % (len(targets), prm["minStocks"])})
            continue

        new_dates = [str(d) for d in ic.index]
        new_ic = ic.to_numpy(dtype="float32")
        aligned = groups_df.reindex(index=ic.index,
                                   columns=list(range(1, prm["nGroups"] + 1)))
        new_groups = aligned.to_numpy(dtype="float32")
        all_dates = np.concatenate([ser["dates"], np.asarray(new_dates, dtype="U10")])
        all_ic = np.concatenate([ser["ic"], new_ic])
        all_groups = np.concatenate([ser["groups"], new_groups], axis=0)
        _save_series(name, all_dates, all_ic, all_groups)
        _touch_factor(meta, name, [str(d) for d in all_dates])
        per_factor[name] = len(new_dates)
        appended_total += len(new_dates)

    _write_append_meta(meta, run_id)
    reconcile_with_eval(params=prm, meta=meta)
    if verbose:
        print("  factor_ts 追加 %d 个日期点（%d 个因子有增长；最后可用日 %s）"
              % (appended_total, len(per_factor), cutoff), flush=True)
    return {"ok": True, "appended": appended_total, "factors": len(names),
            "skipped": skipped, "failed": failed, "perFactor": per_factor,
            "unexpectedDates": unexpected,
            "elapsed": round(time.time() - t0, 2)}


def _write_append_meta(meta: Dict[str, Any], run_id: Optional[str]) -> None:
    """记录本次追加的运行标识与时刻（`lastAppendRunId` 供排障对照）。"""
    meta["lastAppendRunId"] = run_id
    meta["lastAppendAt"] = FR.iso_now()
    save_meta(meta)


# ────────────────────────────────────────────────────────────
# 读：切片（滚动验证唯一入口）
# ────────────────────────────────────────────────────────────
def slice_until(name: str, cut: str, window: Optional[int] = None) -> Optional[Dict[str, Any]]:
    """取 `date <= cut` 的序列（可选只取最后 `window` 行）。**预留未来，绝不含 T 之后**。

    这是滚动验证读数据的唯一入口。函数内部再断言一次 `max(dates) <= cut`
    （约束 A 的第一道：切片层），因为「调用方传错了 cut」是这里唯一挡不住的错。

    Args:
        name: 因子名。
        cut: 截止日期（含），通常来自 `rolling_eval.ic_cutoff()`。
        window: 只保留最后 window 行；None 表示全给。

    Returns:
        {"name", "dates", "ic", "groups", "n", "last"} 或 None（无序列/无可用日期）。
    """
    ser = _load_series(name)
    if ser is None:
        return None
    cut_s = str(cut)
    mask = ser["dates"] <= cut_s
    if not mask.any():
        return {"name": name, "dates": np.asarray([], dtype="U10"),
                "ic": np.asarray([], dtype="float32"),
                "groups": np.zeros((0, int(ser["groups"].shape[1])), dtype="float32"),
                "n": 0, "last": None}
    idx = np.flatnonzero(mask)
    if window:
        idx = idx[-int(window):]
    dates = ser["dates"][idx]
    ic = ser["ic"][idx]
    groups = ser["groups"][idx]
    if len(dates) and str(dates[-1]) > cut_s:        # 切片层断言（约束 A 第一道）
        raise AssertionError(
            "factor_ts.slice_until 切出了未来数据：%s > cut=%s（%s）"
            % (dates[-1], cut_s, name))
    return {"name": name, "dates": dates, "ic": ic, "groups": groups,
            "n": int(len(idx)), "last": str(dates[-1]) if len(dates) else None}


def axis_info(meta: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """各因子轴的汇总（元信息，不含数组），用于日志与 verify 探针。"""
    info = load_meta() if meta is None else meta
    per: Dict[str, Any] = {}
    for name in (info.get("factors") or {}):
        ser = _load_series(name)
        if ser is None:
            per[name] = {"n": 0, "first": None, "last": None, "state": "missing"}
            continue
        entry = (info.get("factors") or {}).get(name) or {}
        per[name] = {"n": int(len(ser["dates"])),
                     "first": str(ser["dates"][0]),
                     "last": str(ser["dates"][-1]),
                     "state": str(entry.get("state") or "ok")}
    return {"dir": ts_dir(), "factors": per,
            "states": states(info), "suspects": suspects(info)}


def summary() -> Dict[str, Any]:
    """写进 `factor_run.json` / verify 段的紧凑摘要（不含数组）。"""
    meta = load_meta()
    info = axis_info(meta)
    per = info["factors"]
    lasts = [v["last"] for v in per.values() if v["last"]]
    return {"dir": ts_dir(),
            "initialized": bool(meta.get("factors")),
            "factors": len(per),
            "n": int(sum(v["n"] for v in per.values())),
            "last": max(lasts) if lasts else None,
            "first": min((v["first"] for v in per.values() if v["first"]), default=None),
            "states": info["states"],
            "suspects": info["suspects"],
            "reconcile": (meta.get("reconcile") or {}),
            "params": _params_of(meta) if meta else None,
            "builtAt": meta.get("builtAt"),
            "lastAppendAt": meta.get("lastAppendAt"),
            "lastAppendRunId": meta.get("lastAppendRunId"),
            "willPersist": list(WILL_PERSIST),
            "axis": "per-factor"}


# ────────────────────────────────────────────────────────────
# 与 factor_eval.json 对账（§5.7，warn-only）
# ────────────────────────────────────────────────────────────
def reconcile_with_eval(path: Optional[str] = None,
                        params: Optional[Dict[str, Any]] = None,
                        meta: Optional[Dict[str, Any]] = None,
                        save: bool = True) -> Dict[str, Any]:
    """把本模块的逐日 IC 与存档 `factor_eval.json` 的 `ic_series` 比一遍。

    **只作可见性，不作重建触发**：重算本身已经是全量重建，而且更可能的差异源是
    前复权漂移（腾讯按最新价重算全历史）而不是代码错误 —— 重建解决不了，
    但记录能让它可见。存档是 `min_stocks=30` 口径，本模块是 100，所以
    「存档有、重算无」的天数是**口径差异**，与数值差分开记两个布尔：

        dataWarn    = maxAbsDiff > RECONCILE_NUM_RTOL   数值问题（复权漂移/代码差异）
        caliberWarn = extraPct > 5%                          参数问题（100 口径太严）

    ⚠ `dataWarn` 用的是**跨口径**阈值（1e-3），不是 `PROTECT_RTOL`（1e-5）：
    后者是给「同口径自比」用的（第 6 条保护式重算）。拿 1e-5 卡 30 vs 100 两种
    口径，会让 `dataWarn` 永远为真、`state` 永远 `warn` —— 天天亮的告警等于没有告警。
    阈值依据见常量定义处的实测分布。

    ⚠ `commonFactorDays` 是 **(因子, 日) 的累加**，不是交易日数：
    24 个因子 × 约 1400 天 ≈ 33187。以前叫 `commonDays`，读者会以为
    「重叠了 33187 个交易日」（A 股一年才 244 天）。

    全局 `state` 由 `factors[].state` **派生**，不另写一套判定（否则两处必然分叉）。

    Returns:
        reconcile 段（同时写进 meta.reconcile）。
    """
    info = load_meta() if meta is None else meta
    prm = dict(params or _params_of(info) or DEFAULTS)
    eval_path = path or os.environ.get("FACTOR_EVAL_PATH") or EVAL_PATH_DEFAULT
    evald = _read_json(eval_path)
    evald_factors = evald.get("factors") or {}

    worst = 0.0
    common_factor_days = 0
    extra_days = 0
    eval_days = 0
    checked: List[str] = []
    for name in sorted((info.get("factors") or {})):
        series = ((evald_factors.get(name) or {}).get("ic_series") or [])
        if not series:
            continue
        ser = _load_series(name)
        if ser is None:
            continue
        ours = {str(d): float(v) for d, v in zip(ser["dates"].tolist(),
                                                ser["ic"].tolist())}
        theirs: Dict[str, float] = {}
        for item in series:
            try:
                d, v = str(item[0]), float(item[1])
            except (TypeError, ValueError, IndexError):
                continue
            if np.isfinite(v):
                theirs[d] = v
        if not theirs:
            continue
        eval_days += len(theirs)
        common = set(ours) & set(theirs)
        common_factor_days += len(common)
        extra_days += len(set(theirs) - set(ours))
        for d in common:
            worst = max(worst, abs(ours[d] - theirs[d]))
        checked.append(name)

    extra_pct = (extra_days / eval_days) if eval_days else 0.0
    data_warn = bool(worst > RECONCILE_NUM_RTOL)
    caliber_warn = bool(extra_pct > EXTRA_PCT_WARN)
    sus = suspects(info)
    if sus:
        state = "suspect"          # 优先级最高
    elif data_warn or caliber_warn:
        state = "warn"
    else:
        state = "ok"

    prev = info.get("reconcile") or {}
    first_warn = prev.get("firstWarnAt")
    if state == "ok":
        first_warn = None
    elif not first_warn:
        # 复权漂移天天存在，warn 一旦触发基本不会消失。只存最新一次 warning
        # 会让提示变成墙纸，用户无法区分「今天新出现问题」和「半年前就存在」。
        first_warn = time.strftime("%Y-%m-%d")

    out = {
        "checkedAt": FR.iso_now(),
        # (因子, 日) 累加，不是交易日数（24 × ~1400 ≈ 33187）
        "commonFactorDays": int(common_factor_days),
        "maxAbsDiff": float(worst),
        "extraDays": int(extra_days),
        "extraPct": float(round(extra_pct, 6)),
        "state": state,
        "firstWarnAt": first_warn,
        "dataWarn": data_warn,
        "caliberWarn": caliber_warn,
        "suspectFactors": [s["factor"] for s in sus],
        "factorsChecked": checked,
        "evalPath": eval_path,
        "rtol": RECONCILE_NUM_RTOL,
        "extraWarnPct": EXTRA_PCT_WARN,
        "note": "存档口径 min_stocks=30，本序列 min_stocks=%d；差异只告警，不作重建触发"
                % int(prm.get("minStocks") or DEFAULTS["minStocks"]),
    }
    info["reconcile"] = out
    if save:
        save_meta(info)
    return out


# ────────────────────────────────────────────────────────────
# 销毁性操作（一律留退路）
# ────────────────────────────────────────────────────────────
def invalidate(names: Optional[Iterable[str]] = None,
               reason: str = "manual") -> Dict[str, Any]:
    """删除指定因子的序列（先改名成 `.bak`，**不真删**）。

    全量重建走 `ensure(..., force)` 语义时用不到本函数；这里主要给排障用。
    刻意保留 `.bak`：一个「删掉重来」的直觉操作，在盘上有 1.5MB 正确数据时
    没有任何好处，但会让人失去回退的可能。
    """
    target = list(names) if names else sorted((load_meta().get("factors") or {}))
    moved: List[str] = []
    for name in target:
        path = ts_path(name)
        if not os.path.exists(path):
            continue
        try:
            os.replace(path, path + ".bak")
            moved.append(name)
        except OSError:
            continue
    return {"ok": True, "moved": moved, "reason": reason,
            "note": "已改名为 .bak（未删除）；下次 ensure() 会按失效矩阵重建"}


# ────────────────────────────────────────────────────────────
# CLI
# ────────────────────────────────────────────────────────────
def _load_bundle(conn: sqlite3.Connection, params: Dict[str, Any]) -> pd.DataFrame:
    """CLI 用：自己加载价格宽表算 fwd（pipeline 路径由 PriceBundle 注入）。"""
    import factor_eval as FE
    close = FE.load_close_wide(conn)
    open_wide = FE.load_open_wide(conn)
    return FE.load_forward_returns(close, int(params["horizon"]),
                                   entry=str(params["entry"]),
                                   open_wide=open_wide)


def main(argv: Optional[Sequence[str]] = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="因子时间序列（逐日 IC + 逐日分组收益）")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--build", action="store_true", help="ensure()：初始化/修复")
    ap.add_argument("--append", action="store_true", help="追加到 panelMax-horizon")
    ap.add_argument("--reconcile", action="store_true", help="只与 factor_eval.json 对账")
    ap.add_argument("--info", action="store_true", help="只看现状（不查大表）")
    ap.add_argument("--horizon", type=int, default=DEFAULTS["horizon"])
    ap.add_argument("--entry", default=DEFAULTS["entry"])
    ap.add_argument("--min-stocks", type=int, default=DEFAULTS["minStocks"])
    ap.add_argument("--groups", type=int, default=DEFAULTS["nGroups"])
    args = ap.parse_args(argv)

    if args.info:
        s = summary()
        print(json.dumps(s, ensure_ascii=False, indent=2))
        return 0
    if args.reconcile:
        info = reconcile_with_eval()
        print(json.dumps(info, ensure_ascii=False, indent=2))
        return 0
    if not (args.build or args.append):
        ap.print_help()
        return 0

    prm = {"horizon": args.horizon, "entry": args.entry,
           "minStocks": args.min_stocks, "nGroups": args.groups}
    conn = sqlite3.connect(args.db, timeout=120)
    try:
        fwd = _load_bundle(conn, prm)
        if args.build:
            res = ensure(conn, fwd, prm, verbose=True)
            print("ensure：%s，重建 %d / 修复 %d / 保护式 %d，耗时 %.1fs"
                  % (res["action"], len(res["rebuilt"]), len(res["repaired"]),
                     len(res["protective"]), res["elapsed"]))
        if args.append:
            res = append(conn, fwd, verbose=True)
            print("append：+%d 个日期点，跳过 %d 天（> panelMax-horizon），耗时 %.1fs"
                  % (res["appended"], len(res["skipped"]), res["elapsed"]))
    except CacheIncomplete as exc:
        print("！！ %s" % exc, file=sys.stderr)
        return exc.exit_code
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
