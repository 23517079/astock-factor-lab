# -*- coding: utf-8 -*-
"""
因子合成打分与选股信号

为什么需要这一层
----------------
`factor_eval.py` 只回答"单个因子有没有效"。但用户要的是"今天该买哪几只"，
这中间缺两步：
    1. 把多个因子合成一个综合分（否则 24 个因子给你 24 个互相矛盾的答案）
    2. 用一个可回测的规则把综合分变成买/卖动作

直接等权相加是错的：24 个因子里有 12 对相关系数 >= 0.7（rev10↔ma_gap20 达 +0.93），
等权相加等于把"反转"这个信号抄了六遍，权重严重失衡。
所以必须先去冗余：把高相关因子归成一簇，每簇只留统计上最强的那个。

打分口径（T06 起：逐期 walk-forward，默认）
------------------------------------------
    1. 期网格复用 `rolling_eval.period_indices(step=20)`；每期用
       `evaluate_period(dates, t, params)` 判定 —— 它内部只读 `<= ic_cutoff(t)`，
       本模块**不另写**任何「取最近 N 天重挑因子」的窗口逻辑
    2. 入选集合 = `per["selected"]`（verdict ∈ {值得复核, 方向需翻转}）
    3. 方向 `d_eff = registry.direction × (+1 值得复核 / -1 方向需翻转)`。
       「翻转」是**结论**不是修饰词，必须落到符号上（旧实现全库只有
       `panels[n] * registry.direction`，没有按 verdict 取反的分支 ——
       于是把「必须先翻方向」的因子选进来、又按没翻的方向用）
    4. 权重**等权**。不用 `|t|` / `|IC|`：方向判断刚出过错，按强度加权
       会把错误方向的权重放大
    5. 每日横截面 rank 标准化到 [-0.5, +0.5]，再按「当日生效期」的符号与权重合成
    6. 综合分本身用同一套 IC 方法检验；**IC 不显著就不许拿去选股**

逐日归属判据（唯一一句，不得改写）
----------------------------------
    某日 `d` 采用「**最近一期 `t` 满足 `ic_cutoff(t) <= d - horizon`**」的结论；
    该 cutoff 之前没有可用期时**不向前回填**（那些天不打分，不做任何外推）。

    三种写错方式，一个都不许出现：
      * `cut < d - horizon` —— 边界那天被漏掉（差一天）
      * `cut <= d`         —— 就是约束 A 那个错误，等于用了 d-4..d-1 的收益
      * 拿 `per["asOf"]` 当比较键 —— `asOf` 比 `cutoff` 晚 horizon 天，
        比较基准与数据窗口错位，等于把「决策生效日」和「证据截止日」混成一个

    判据的等价形式：`ic_cutoff(t) <= d - horizon` ⟺ `t <= idx(d)`
    （期网格与日期轴同一条、升序），所以第 t 期结论从 `asOf(t)` 当天开始生效。
    实现里仍然逐字用**日期**比较（`sign_steps`），不用下标 —— 下标等价性
    依赖「两条轴完全一致」这个隐含前提，日期比较没有这个依赖。

旧口径（`--legacy` / `mode="legacy"`，保留用于对照与回滚）
--------------------------------------------------------
    1. 候选因子按**全样本** t 值挑（`load_candidates` 读 `factor_eval.json`）
    2. 方向一律用先验 `registry.direction`
    3. 簇内去冗余 → 等权平均
    这两处都是前视（判定用的是全样本、回测用的也是全样本），只作对照。

为什么用 rank 而不是 z-score
--------------------------
    z-score 会被极端值带跑：一只票涨停导致某因子值远超均值，标准化后它一个人占掉
    大半个截面方差。rank 只关心排序，这也是 IC 本来就在用的度量方式，口径统一。

输出
----
    data/factor_score.json   —— 页面用（含验证结论、今日推荐、因子构成）

    `validation`           = **walk-forward 序列**的 IC / t_NW / 十分组（主指标）
    `validationFullSample` = 全样本口径的同一组指标（对照，含轻度前视，页面不当结论）
    `directionBasis`       = 当前生效期的方向依据 `{asOf, icCutoff, flipped, signed}`
    `signAsOf`             = 逐日生效期（阶梯函数），回答「这天为什么这么算」
    `picks`                = **只有一份**名单（walk-forward 版）
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd

import factor_eval as fe
import trade_cost as TC

# 让 `run_factor_pipeline.step_score` 能显式捕获成本配置错误并映射到 exit 2，
# 不必再 import trade_cost；同时保持 ValueError 的兼容性。
CostProfileError = TC.CostProfileError

DB = "data/sequoia_v2.db"
EVAL = "data/factor_eval.json"
OUT = "data/factor_score.json"

# 进入合成的因子档位：只有"统计显著 + 十组单调"的才配进来
CANDIDATE_VERDICTS = ("值得复核", "方向需翻转")

# 去冗余阈值：|rho| 超过这个值视为同一簇
CLUSTER_RHO = 0.70

# 逐日归属判据的原文（写进 JSON，页面/报告直接显示，避免各处措辞不一）
WF_CRITERION = "icCutoff(t) <= d - horizon"

# verdict → 有效方向乘子（**全库唯一一份**）。
#
# 硬规则（说明书 §5.9）：凡是新增一个档位进 `rolling_eval.SELECTED_VERDICTS`，
# 必须在同一处同时声明消费端如何处置它 —— 用什么符号、什么权重、缺失时怎么办。
# 所以 `_ensure_verdict_signs()` 会**在运行期**核对两个集合是否一一对应：
# 少声明一个就抛 SystemExit，绝不静默按 +1 用（静默按先验方向用，正是这一轮的病灶）。
VERDICT_SIGN: Dict[str, int] = {"值得复核": 1, "方向需翻转": -1}

# 打分模式：walkforward（默认）/ legacy（旧的全样本口径，对照与回滚用）
MODE_WF = "walkforward"
MODE_LEGACY = "legacy"


class EvidenceGateError(RuntimeError):
    """证据门槛未过且 `enforce_gate=True` —— 必须在写盘前拦住。

    专门定义成独立类型，是为了让 `run_factor_pipeline.step_score` 能精确映射到
    `EXIT_INVALID=2`，而不是落进通用异常变成「打分失败 = 4」。
    `4` 只留给「连名单都做不出来」；这里是名单做得出来但**证据不够**，
    两者在运维上完全是两件事。

    Raises:
        由 `generate()` 在 `_write_json_atomic()` **之前**抛出；页面保留旧名单。
    """


# ---------------------------------------------------------------- 合成分构建

def load_candidates(evald: Dict[str, Any]) -> List[Dict[str, Any]]:
    """从评估结果里挑出可进入合成的因子，并带上 t 值用于簇内择优。"""
    out = []
    for name, f in (evald.get("factors") or {}).items():
        tag, _ = _verdict(f)
        if tag not in CANDIDATE_VERDICTS:
            continue
        rank_ic = f.get("rank_ic") or {}
        out.append({
            "name": name,
            "verdict": tag,
            "t_nw": fe_num(rank_ic.get("t_nw")),
            "ic": fe_num(rank_ic.get("mean")),
        })
    return out


def fe_num(v) -> Optional[float]:
    """把 NaN/inf/None 统一成 None，避免 JSON 里出现 NaN（不是合法 JSON）。"""
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if np.isnan(f) or np.isinf(f):
        return None
    return f


def _verdict(f: Dict[str, Any]) -> Tuple[str, str]:
    """复用 export_factor_api 里唯一那份判定规则。

    刻意不在这里重写一遍：分档规则若存在两处，改阈值时必然漏改一边，
    页面和推荐名单就会给出互相矛盾的结论。
    """
    from export_factor_api import verdict_of
    return verdict_of(f)


def cluster_by_corr(names: List[str], corr: Dict[str, Dict[str, float]],
                    rho: float = CLUSTER_RHO) -> List[List[str]]:
    """
    按相关系数把因子并成簇（并查集）。

    为什么不用层次聚类：因子只有 20 来个，并查集足够，而且结果可解释——
    "这几个因子互相高度相关，所以算一类"，比树状图更好向用户解释。
    """
    parent = {n: n for n in names}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    for i, a in enumerate(names):
        for b in names[i + 1:]:
            v = (corr.get(a) or {}).get(b)
            if v is None:
                v = (corr.get(b) or {}).get(a)
            if v is not None and abs(v) >= rho:
                union(a, b)

    groups: Dict[str, List[str]] = {}
    for n in names:
        groups.setdefault(find(n), []).append(n)
    return [sorted(v) for v in groups.values()]


def pick_representatives(names: List[str], corr: Dict[str, Dict[str, float]],
                         tmap: Dict[str, float]) -> Tuple[List[str], Dict[str, List[str]]]:
    """
    每个相关簇只留 |t| 最大的一个因子作为代表。

    返回 (代表因子列表, 簇明细)。簇明细用于页面上解释"为什么这两个因子只算一个"。
    """
    clusters = cluster_by_corr(names, corr)
    reps: List[str] = []
    detail: Dict[str, List[str]] = {}
    for cl in clusters:
        if len(cl) == 1:
            reps.append(cl[0])
            detail[cl[0]] = cl
            continue
        best = max(cl, key=lambda n: abs(tmap.get(n) or 0))
        reps.append(best)
        detail[best] = cl
    return sorted(reps), detail


def _weighted_avg(stack: np.ndarray, w: np.ndarray) -> Tuple[np.ndarray, np.ndarray]:
    """`(k, ...)` 因子堆叠 → 加权平均 + 每格实际参与因子个数。

    **加权语义只在这里写一遍**：`combine_parts`（旧路径/全样本对照）与
    `build_walk_forward`（逐期路径）都调它。两处各自写一遍是这一轮最该避免的错——
    方向、权重、缺失值处理任何一处漂移，两份结果就不再可比，而它们长得完全一样。

    缺失值不能当 0（会把这只票的分数系统性拉向中间），而是用它「有值的那些因子」
    重新归一化权重；一个因子都没有的格子给 NaN（不是 0）。

    Args:
        stack: 形如 `(因子数, 日期数, 股票数)` 的数组，缺失为 NaN。
        w: 长度 = 因子数的权重（内部再归一化，传绝对值即可）。

    Returns:
        `(combined, counts)`：与 `stack.shape[1:]` 同形的两个数组。
    """
    if stack.ndim < 2:
        raise ValueError("_weighted_avg：stack 至少要有 2 维，收到 %r" % (stack.shape,))
    n = int(stack.shape[0])
    if len(w) != n:
        raise ValueError("_weighted_avg：权重长度 %d ≠ 因子数 %d" % (len(w), n))
    abs_w = np.abs(np.asarray(w, dtype="float64"))
    total = float(abs_w.sum()) or 1.0
    w = abs_w / total

    w_shaped = w.reshape((-1,) + (1,) * (stack.ndim - 1))
    with np.errstate(invalid="ignore"):
        filled = np.where(np.isnan(stack), 0.0, stack)
        wmask = np.where(np.isnan(stack), 0.0, w_shaped)
        wsum_arr = wmask.sum(axis=0)
        combined = np.divide((filled * wmask).sum(axis=0), wsum_arr,
                             out=np.full(stack.shape[1:], np.nan),
                             where=wsum_arr > 0)
    counts = np.sum(~np.isnan(stack), axis=0)
    return combined, counts


def combine_parts(parts: Dict[str, pd.DataFrame],
                  weights: Dict[str, float]) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """把若干「已归正方向 + 已横截面标准化」的因子面板加权合成一个分数。

    单独抽出来，是为了能在不改动原始面板的前提下换一套权重重算
    （比如剔除「最近已失效」的因子后再合成一次，看名单会不会变）。

    Args:
        parts: factor -> 已归正方向并 rank 到 [-0.5, 0.5] 的宽表。
        weights: factor -> 权重（内部会再归一化，传绝对值即可）。

    Returns:
        (scores, valid)：综合分宽表；每只票实际参与合成的因子个数。
    """
    names = [n for n in parts if n in weights]
    if not names:
        raise SystemExit("combine_parts：没有可用的因子")
    frames = [parts[n] for n in names]

    # 不同因子可用股票集合不一样：预热期长的因子在某些票上没有值，
    # 停牌多的票也可能整列缺失。直接 np.stack 会 shape 不一致，
    # 所以先对齐到「日期并集 × 股票并集」，缺失处留 NaN 交给加权和的分母处理。
    idx = sorted(set().union(*[set(p.index) for p in frames]))
    cols = sorted(set().union(*[set(p.columns) for p in frames]))
    frames = [p.reindex(index=idx, columns=cols) for p in frames]

    stack = np.stack([f.values for f in frames], axis=0)
    combined, counts = _weighted_avg(
        stack, np.array([abs(weights[n]) for n in names], dtype="float64"))
    scores = pd.DataFrame(combined, index=frames[0].index, columns=frames[0].columns)
    valid = pd.DataFrame(counts, index=scores.index, columns=scores.columns)
    return scores, valid


def candidates_manual(evald: Dict[str, Any], names: List[str]) -> List[Dict[str, Any]]:
    """按显式指定的因子名构造候选（不要求它们通过「值得复核」这一档）。

    用途：合成「最近仍然有效的因子」这种对照组合时，因子是按近期状态挑的，
    不是按全期分档挑的，所以不能走 load_candidates 的过滤。
    """
    out: List[Dict[str, Any]] = []
    for n in names:
        f = (evald.get("factors") or {}).get(n)
        if not f:
            raise SystemExit("评估结果里没有因子 %s，先确认拼写" % n)
        ri = f.get("rank_ic") or {}
        out.append({"name": n, "verdict": "manual",
                    "t_nw": fe_num(ri.get("t_nw")), "ic": fe_num(ri.get("mean"))})
    return out


def _build_scores_legacy(conn: sqlite3.Connection, evald: Dict[str, Any],
                         start: Optional[str] = None, end: Optional[str] = None,
                         weight: str = "equal", verbose: bool = True,
                         only: Optional[List[str]] = None) -> Dict[str, Any]:
    """
    **旧口径**：全样本挑因子 + 先验方向 + 簇内去冗余。含两处前视，仅作对照。

    weight:
        equal —— 每个代表因子等权（默认，最不容易过拟合）
        ic    —— 按 |IC| 加权（让预测力强的因子说话更响）
        t     —— 按 |t| 加权（同时考虑预测力和稳定性）

    加权方式本身也是一种选择。用哪种应该在**切分检验之后**再定，
    而不是挑一个在训练段最好看的——那正是过拟合的起点。
    """
    from factors.registry import get as get_factor

    cands = candidates_manual(evald, only) if only else load_candidates(evald)
    if not cands:
        raise SystemExit("没有可用于合成的因子（没有「值得复核」或「方向需翻转」）")

    corr = evald.get("corr_matrix") or {}
    tmap = {c["name"]: (c["t_nw"] or 0) for c in cands}
    names = [c["name"] for c in cands]

    reps, clusters = pick_representatives(names, corr, tmap)
    if verbose:
        print("候选因子 %d 个，去冗余后保留 %d 个：" % (len(names), len(reps)))
        for r in reps:
            members = clusters.get(r, [r])
            extra = ("  （合并了 %s）" % ", ".join(m for m in members if m != r)) if len(members) > 1 else ""
            print("    %-14s |t|=%.2f%s" % (r, abs(tmap.get(r) or 0), extra))

    t0 = time.time()
    panels: Dict[str, pd.DataFrame] = {}
    for n in reps:
        panels[n] = fe.load_panel_wide(conn, n, start=start, end=end)
    if verbose:
        print("面板载入完成（%.0fs），截面 %d 天 × %d 只" %
              (time.time() - t0, len(panels[reps[0]].index), panels[reps[0]].shape[1]))

    # 方向修正 + rank 标准化
    parts: List[pd.DataFrame] = []
    used: List[str] = []
    weights: List[float] = []
    for n in reps:
        d = int(get_factor(n)["direction"])
        df = panels[n] * d          # direction=-1 取负 → 统一成"越大越好"
        # 每日横截面百分位排名，映射到 [-0.5, 0.5]
        r = df.rank(axis=1, pct=True) - 0.5
        parts.append(r)
        used.append(n)
        if weight == "ic":
            weights.append(abs(next(c["ic"] or 0 for c in cands if c["name"] == n)))
        elif weight == "t":
            weights.append(abs(tmap.get(n) or 0))
        else:
            weights.append(1.0)
    wsum = sum(weights) or 1.0
    weights = [w / wsum for w in weights]

    scores, valid_df = combine_parts(dict(zip(used, parts)), dict(zip(used, weights)))

    return {
        "mode": MODE_LEGACY,
        "scores": scores,
        "valid": valid_df,
        "factors": used,
        "weights": dict(zip(used, weights)),
        "parts": dict(zip(used, parts)),
        "clusters": clusters,
        "tmap": tmap,
        "panels": panels,
        "directionBasis": None,
        "signAsOf": None,
        "periods": [],
        "reps": reps,
        "cands": cands,
    }


# ---------------------------------------------------------------- 逐期 walk-forward

def _ensure_verdict_signs() -> None:
    """核对 `VERDICT_SIGN` 与 `rolling_eval.SELECTED_VERDICTS` 一一对应。

    少声明一个档位的符号就**直接抛**，不静默按 +1 用 —— 静默按先验方向用，
    正是这一轮那个「选进来、又按没翻的方向用」的病灶。一旦有人往
    `SELECTED_VERDICTS` 里加档位而忘了加符号，这里会在第一场运行时炸掉。

    Raises:
        SystemExit: 两个集合不一致（多声明/少声明都算）。
    """
    import rolling_eval as RE

    sel = set(RE.SELECTED_VERDICTS)
    declared = set(VERDICT_SIGN)
    if sel == declared:
        return
    missing = sorted(sel - declared)
    extra = sorted(declared - sel)
    raise SystemExit(
        "VERDICT_SIGN 与 rolling_eval.SELECTED_VERDICTS 不一致："
        "未声明符号的档位=%s，声明了但不该入选的档位=%s。"
        "新增档位时必须同时声明「消费端怎么处置它」（说明书 §5.9 硬规则）；"
        "绝不静默按 +1 处理。" % (missing, extra))


def wf_params(horizon: int = 5, roll_step: int = 20,
              roll_window: int = 120) -> Dict[str, Any]:
    """逐期打分的口径：与 `rolling_eval` **同一套常量**，不另立一份。"""
    import rolling_eval as RE

    prm = dict(RE.DEFAULTS)
    prm["horizon"] = int(horizon)
    prm["nwLag"] = max(0, int(horizon) - 1)      # 与 factor_eval 的口径一致
    prm["rollStep"] = int(roll_step)
    prm["rollSelectWindow"] = int(roll_window)
    return prm


def period_signals(dates: Sequence[str], params: Dict[str, Any],
                   verbose: bool = False) -> List[Dict[str, Any]]:
    """逐期判定：直接复用 `rolling_eval.evaluate_period`，不另写选因子逻辑。

    为什么必须复用：`evaluate_period` 内部只读 `<= ic_cutoff(t)` 的序列
    （`slice_until` 里还有一道 `raise AssertionError`），这套无前视保证是
    全库唯一的。这里再写一遍「取最近 N 天 IC 判定」就等于开了第二个入口。

    Args:
        dates: 面板日期轴（升序）。
        params: 口径（见 `wf_params`）。
        verbose: 打印每期摘要。

    Returns:
        `[{"tIndex","asOf","icCutoff","selected","signs","verdicts",
           "nSelected","nEvaluable"}]`，按 `asOf` 升序。
        `signs` 是 verdict → 符号（+1 值得复核 / -1 方向需翻转），只含入选因子。
    """
    import rolling_eval as RE

    _ensure_verdict_signs()
    idx_list = RE.period_indices(len(dates), int(params["horizon"]),
                                 int(params["rollStep"]))
    out: List[Dict[str, Any]] = []
    for t_idx in idx_list:
        per = RE.evaluate_period(dates, int(t_idx), params)
        signs: Dict[str, int] = {}
        verdicts: Dict[str, str] = {}
        unknown: List[Tuple[str, str]] = []
        for row in (per.get("factors") or []):
            if not row.get("selected"):
                continue
            tag = str(row.get("verdict") or "")
            sign = VERDICT_SIGN.get(tag)
            if sign is None:
                unknown.append((str(row.get("factor")), tag))
                continue
            signs[str(row["factor"])] = int(sign)
            verdicts[str(row["factor"])] = tag
        if unknown:
            raise SystemExit(
                "逐期判定遇到未声明符号的档位 %s（因子 %s）；"
                "请先在 VERDICT_SIGN 里声明它的有效方向"
                % (sorted({t for _n, t in unknown}),
                   sorted({n for n, _t in unknown}))[:3])
        out.append({
            "tIndex": int(per["tIndex"]),
            "asOf": str(per["asOf"]),
            "icCutoff": str(per["icCutoff"]),
            "selected": [n for n in (per.get("selected") or []) if n in signs],
            "signs": signs,
            "verdicts": verdicts,
            "nSelected": len(signs),
            "nEvaluable": int(per.get("n_evaluable") or 0),
        })
        if verbose:
            print("    期 %s（cut %s）入选 %d 个%s"
                  % (per["asOf"], per["icCutoff"], len(signs),
                     "，需翻转 %d 个" % sum(1 for s in signs.values() if s < 0)
                     if any(s < 0 for s in signs.values()) else ""))
    return out


def sign_steps(dates: Sequence[str], periods: Sequence[Dict[str, Any]],
               horizon: int) -> Tuple[List[int], List[Dict[str, Any]]]:
    """逐日生效期（阶梯函数）。**判据在这里逐字实现，只此一处。**

    判据：某日 `d` 采用「最近一期 `t` 满足 `ic_cutoff(t) <= d - horizon`」的结论；
    该 cutoff 之前没有可用期时返回 -1（**不回填**，那些天不打分）。

    为什么用日期比较而不是下标比较：`cut <= dates[i-h]` 与 `t <= i` 在「期网格
    与日期轴完全同一条」时等价，但那是隐含前提。日期比较没有这个依赖，也能让
    「改用 asOf 当键」这类错误立刻显形（asOf 比 cut <= 晚 horizon 天）。

    Args:
        dates: 面板日期轴（升序）。
        periods: `period_signals()` 的结果（按 asOf 升序，icCutoff 单调不减）。
        horizon: IC 的前瞻期。

    Returns:
        `(step_of, segments)`：
          * `step_of[i]` = 第 i 天生效的期下标；-1 表示尚无结论（不打分）
          * `segments[j]` = `{..., "rows": [日期下标...], "from", "to"}`
    """
    n_days = len(dates)
    n_per = len(periods)
    h = int(horizon)
    step_of: List[int] = [-1] * n_days
    if not n_per or n_days <= h:
        return step_of, []

    j = 0
    for i in range(n_days):
        if i < h:
            continue                                   # 连 d - horizon 都不存在
        d_minus_h = str(dates[i - h])
        # `icCutoff` 随期单调不减（期网格升序），所以指针只前进 → O(天数)
        while j + 1 < n_per and str(periods[j + 1]["icCutoff"]) <= d_minus_h:
            j += 1
        if str(periods[j]["icCutoff"]) <= d_minus_h:
            step_of[i] = j

    segments: List[Dict[str, Any]] = []
    for j, per in enumerate(periods):
        rows = [i for i, s in enumerate(step_of) if s == j]
        flips = sorted(n for n, s in (per.get("signs") or {}).items() if s < 0)
        segments.append({
            "tIndex": int(per["tIndex"]),
            "asOf": str(per["asOf"]),
            "icCutoff": str(per["icCutoff"]),
            "nSelected": int(per.get("nSelected") or 0),
            "selected": list(per.get("selected") or []),
            "signs": dict(per.get("signs") or {}),
            "flipped": flips,
            "rows": rows,
            "from": str(dates[rows[0]]) if rows else None,
            "to": str(dates[rows[-1]]) if rows else None,
        })
    return step_of, segments


def _centered_rank(wide: pd.DataFrame, index: pd.Index,
                   columns: Sequence[str]) -> pd.DataFrame:
    """每日横截面百分位排名，映射到 [-0.5, +0.5]。

    用 rank 而不是 z-score：z-score 会被极端值带跑（一只票涨停就能占掉大半个
    截面方差），而 rank 只关心排序 —— 这也正是 IC 本来就在用的度量，口径统一。
    """
    return wide.reindex(index=index, columns=list(columns)).rank(axis=1, pct=True) - 0.5


def build_walk_forward(conn: sqlite3.Connection, evald: Dict[str, Any],
                       axis: Sequence[str], params: Dict[str, Any],
                       start: Optional[str] = None, end: Optional[str] = None,
                       weight: str = "equal",
                       verbose: bool = True) -> Dict[str, Any]:
    """逐期 walk-forward 打分：每天只用「当时已有结论」的那套因子与方向。

    流程（四步，全部复用已有实现）：
        1. `RE.period_indices` 定期网格 → `RE.evaluate_period` 逐期判定
        2. `sign_steps` 把逐期结论摊成逐日阶梯（判据只在那里写一次）
        3. 每个分段内，按该期入选集合与符号做**等权**合成
        4. 同时用同一批 rank 矩阵算一份「全样本口径」对照（含轻度前视）

    为什么不用 `pick_representatives`（簇内去冗余）
    ----------------------------------------------
    去冗余要 `|t|` 挑代表、要 `corr_matrix`，两者都是**全样本**量 ——
    那正是要消掉的第一处前视。逐期做相关聚类需要每期重算只用 `<= cut` 的
    相关系数，成本与收益另算。所以逐期版**不做去冗余**，等权平均全部入选因子；
    代价是高相关的一簇会重复计权，这条写进 `note` 里，不藏着。

    Args:
        axis: 打分日期轴（必须与 `params` 的期网格同一条，否则 signAsOf 对不上）。
        params: 见 `wf_params()`。
        weight: 仅为兼容旧签名保留 —— **逐期模式强制等权**（见下）。

    Returns:
        `{"scores", "valid", "factors", "weights", "parts", "clusters", "tmap",
          "directionBasis", "signAsOf", "periods", "segments", "scoresFullSample",
          "factorsFullSample", "weightsFullSample", "clustersFullSample", "note"}`
    """
    from factors.registry import get as get_factor

    dates = [str(d) for d in axis]
    if not dates:
        raise SystemExit("逐期 walk-forward：日期轴为空，无法打分")

    periods = period_signals(dates, params, verbose=verbose)
    if not periods:
        raise SystemExit(
            "逐期 walk-forward：面板只有 %d 天，不足 horizon+20，没有可用期"
            % len(dates))
    step_of, segments = sign_steps(dates, periods, int(params["horizon"]))
    if all(s < 0 for s in step_of):
        raise SystemExit(
            "逐期 walk-forward：没有任何一天能配上「最近一期结论」"
            "（首个 cutoff 是 %s，晚于全部打分日），无法打分"
            % periods[0].get("icCutoff"))

    if weight != "equal" and verbose:
        print("    逐期模式强制等权（方向判断刚出过错，按 |t|/|IC| 加权会放大错误方向的权重）")

    # ---- 全样本对照的因子集合（reps）也要在 union 里，省一次面板往返 ----
    cands: List[Dict[str, Any]] = []
    reps: List[str] = []
    clusters: Dict[str, List[str]] = {}
    tmap: Dict[str, float] = {}
    try:
        cands = load_candidates(evald)
        if cands:
            tmap = {c["name"]: (c["t_nw"] or 0) for c in cands}
            reps, clusters = pick_representatives([c["name"] for c in cands],
                                                 evald.get("corr_matrix") or {}, tmap)
    except SystemExit:
        reps, clusters, tmap = [], {}, {}

    union = sorted({n for p in periods for n in p["selected"]} | set(reps))
    if not union:
        raise SystemExit("逐期 walk-forward：所有期都没有因子入选，无法打分")

    t0 = time.time()
    raw: Dict[str, pd.DataFrame] = {}
    for n in union:
        raw[n] = fe.load_panel_wide(conn, n, start=start, end=end)
    cols = sorted(set().union(*[set(p.columns) for p in raw.values()]))
    index = pd.Index(dates)
    if verbose:
        print("    面板载入 %d 个因子（%.0fs），截面 %d 天 × %d 只"
              % (len(raw), time.time() - t0, len(index), len(cols)))

    # 只留 rank（原始面板不再需要：下游只用 rank 与合成分，留着白占 30MB×N）
    ranks: Dict[str, pd.DataFrame] = {}
    for n, wide in raw.items():
        ranks[n] = _centered_rank(wide, index, cols)
    del raw

    scores = pd.DataFrame(np.nan, index=index, columns=cols)
    valid = pd.DataFrame(np.zeros((len(index), len(cols))), index=index, columns=cols)
    # 有效方向 = 先验方向 × verdict 符号。**两个因子都要乘**：
    # 只乘 verdict 符号会漏掉「direction=-1 的因子要取负」，那一类因子会整片反向
    # （实测：ret1/atr_pct14 等 10 个 direction=-1 的因子，part 的 IC 全变成 -0.03）。
    prior_dir = {n: int(get_factor(n)["direction"]) for n in union}
    for seg in segments:
        rows = seg["rows"]
        names = [n for n in seg["selected"] if n in ranks]
        if not rows or not names:
            continue
        w = np.full(len(names), 1.0 / len(names), dtype="float64")   # 等权
        stack = np.stack([ranks[n].values[rows]
                          * float(prior_dir[n] * int(seg["signs"][n]))
                          for n in names], axis=0)
        seg_scores, seg_counts = _weighted_avg(stack, w)
        scores.iloc[rows] = seg_scores
        valid.iloc[rows] = seg_counts

    # ---- 当前生效期（最新一天用的那套）----
    last_j = step_of[-1]
    if last_j < 0:                                   # 理论上不会：最后一期 asOf == dates[-1]
        last_j = max(j for j, s in enumerate(step_of) if s >= 0)
    last = segments[last_j]
    last_verdicts = dict(periods[last_j].get("verdicts") or {})
    signed: Dict[str, int] = {}
    prior: Dict[str, int] = {}
    for n in last["selected"]:
        d_prior = int(get_factor(n)["direction"])
        prior[n] = d_prior
        signed[n] = d_prior * int(last["signs"][n])
    directionBasis = {
        "asOf": last["asOf"],
        "icCutoff": last["icCutoff"],
        "tIndex": int(last["tIndex"]),
        "criterion": WF_CRITERION,
        "nSelected": len(last["selected"]),
        "flipped": sorted(n for n in last["flipped"]),
        "signed": signed,
        "prior": prior,
        "verdicts": {n: last_verdicts.get(n) for n in last["selected"]},
    }

    first_step = next(i for i, s in enumerate(step_of) if s >= 0)
    # 「判据上有结论」与「真的有分数」是两件事：早期期可能一个因子都选不出来
    # （预热期不够，窗口内可用天数 < 20），那些天按判据是有生效期的，但确实没有分数。
    # 两个数都报，不拿一个去顶另一个 —— 否则就是「声称一件没发生的事」。
    scored_rows = scores.notna().any(axis=1).to_numpy()
    has_score = bool(scored_rows.any())
    n_unscored = int(np.argmax(scored_rows)) if has_score else len(dates)
    no_signal = [s["asOf"] for s in segments if s["rows"] and not s["selected"]]
    signAsOf = {
        "criterion": WF_CRITERION,
        "criterionStart": dates[first_step],
        "nUnscoredDays": n_unscored,
        "unscoredBefore": (dates[n_unscored] if has_score else None),
        "noSignalSegments": no_signal,
        "dates": dates[first_step:],
        "segmentOf": [int(s) for s in step_of[first_step:]],
        "segments": [{"tIndex": s["tIndex"], "asOf": s["asOf"],
                      "icCutoff": s["icCutoff"], "from": s["from"], "to": s["to"],
                      "nSelected": s["nSelected"], "flipped": s["flipped"]}
                     for s in segments if s["rows"]],
        "note": ("逐日生效期：每天用「最近一期 icCutoff <= d - horizon」的结论；"
                 "首个 cutoff 之前的日子（前 %d 天）没有结论，不打分也不回填%s"
                 % (int(first_step),
                    "；另有 %d 期虽有生效日但一个因子都没选出来（%s），那些天同样没有分数"
                    % (len(no_signal), ", ".join(no_signal)) if no_signal else "")),
    }

    # ---- 合成权重与贡献（按当前生效期的**有效方向** d_eff）----
    used = [n for n in last["selected"] if n in ranks]
    w_eq = (1.0 / len(used)) if used else 0.0
    weights = {n: w_eq for n in used}
    # 贡献必须与综合分同口径：`d_eff`（先验 × verdict）而不是只乘 verdict 符号，
    # 否则 direction=-1 的因子在页面上的贡献是反的，用户会看到「排名靠前却是负分」
    parts = {n: (ranks[n] if signed[n] > 0 else ranks[n] * -1.0) for n in used}

    # ---- 全样本口径对照（含轻度前视，**只作指标对照，不出名单**）----
    fs_scores: Optional[pd.DataFrame] = None
    fs_weights: Dict[str, float] = {}
    if reps and all(r in ranks for r in reps):
        fs_parts: Dict[str, pd.DataFrame] = {}
        raw_w: Dict[str, float] = {}
        for n in reps:
            d_prior = int(get_factor(n)["direction"])
            # 等价变换：`rank(panel × d) - 0.5` 与 `±(rank(panel) - 0.5)` 只差一个
            # **对所有股票相同**的逐日常数（平均秩的对称性：`rank(-x) = n+1-rank(x)`）。
            # 因此两者的横截面排序相同 —— IC / 十分组 / 名单这些「只看排序」的量
            # 完全等价（浮点上极少数近乎并列的票可能抖一位，实测 |ΔIC| < 1e-3）。
            # 换来的是不必为对照版再算一遍 `rank(panel × d)`、也不必留第二份原始面板。
            fs_parts[n] = ranks[n] if d_prior > 0 else ranks[n] * -1.0
            if weight == "ic":
                raw_w[n] = abs(next((c["ic"] or 0) for c in cands if c["name"] == n))
            elif weight == "t":
                raw_w[n] = abs(tmap.get(n) or 0)
            else:
                raw_w[n] = 1.0
        tot = sum(raw_w.values()) or 1.0
        fs_weights = {n: v / tot for n, v in raw_w.items()}
        # valid 必须一并带出：`make_picks` 用每只票的「有效因子数」做门槛
        # （min_valid_factors=2）。以前这里丢弃它，调用方只能拿
        # `fs.notna()`（恒为 1）冒充，导致对照名单永远为空、却报错在别处。
        fs_scores, fs_valid = combine_parts(fs_parts, fs_weights)

    note = ("逐期 walk-forward：因子名单与方向每 %d 个交易日只用当时可得的数据"
            "（<= icCutoff）重定一次；每日按当日生效的那一套等权合成。"
            "为了让挑选本身也不含前视，逐期版**不做**相关簇去冗余"
            "（去冗余要用全样本 |t| 与全样本相关矩阵，那是另一处前视）。"
            % int(params["rollStep"]))

    return {
        "mode": MODE_WF,
        "scores": scores,
        "valid": valid,
        "factors": used,
        "weights": weights,
        "parts": parts,
        "clusters": {},
        "tmap": {},
        "panels": {},
        "directionBasis": directionBasis,
        "signAsOf": signAsOf,
        "periods": [{k: p[k] for k in ("tIndex", "asOf", "icCutoff", "selected",
                                       "nSelected", "nEvaluable")} for p in periods],
        "segments": segments,
        "params": params,
        "note": note,
        "scoresFullSample": fs_scores,
        "validFullSample": fs_valid,
        "factorsFullSample": list(reps),
        "weightsFullSample": fs_weights,
        "clustersFullSample": clusters,
        "tmapFullSample": tmap,
        "cands": cands,
    }


def build_scores(conn: sqlite3.Connection, evald: Dict[str, Any],
                 start: Optional[str] = None, end: Optional[str] = None,
                 weight: str = "equal", verbose: bool = True,
                 only: Optional[List[str]] = None, mode: str = MODE_WF,
                 horizon: int = 5, roll_step: int = 20,
                 roll_window: int = 120,
                 axis: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """构建每日每只股票的综合分（默认逐期 walk-forward）。

    Args:
        mode: `"walkforward"`（默认，无前视）/ `"legacy"`（旧全样本口径，对照回滚）。
        only: 显式指定因子名 → 强制走 legacy 路径（显式名单本身就不需要逐期重选）。
        axis: 打分日期轴；None 时用 `factor_ts` 的面板轴（生产）/ 面板自身的并集。
        roll_step / roll_window: 期网格口径，必须与 `rolling_eval` 一致。

    Returns:
        见 `build_walk_forward` / `_build_scores_legacy`。
    """
    if only:
        return _build_scores_legacy(conn, evald, start=start, end=end,
                                    weight=weight, verbose=verbose, only=only)
    if mode == MODE_LEGACY:
        return _build_scores_legacy(conn, evald, start=start, end=end,
                                    weight=weight, verbose=verbose)

    dates = [str(d) for d in axis] if axis is not None else _score_axis(conn, start, end)
    return build_walk_forward(conn, evald, dates,
                              wf_params(horizon, roll_step, roll_window),
                              start=start, end=end, weight=weight,
                              verbose=verbose)


def _score_axis(conn: sqlite3.Connection, start: Optional[str],
                end: Optional[str]) -> List[str]:
    """打分日期轴：优先 `factor_ts` 的面板轴（生产，0.03s），失败再退回库。"""
    import factor_ts as TS

    try:
        axis = [str(d) for d in TS.panel_dates_fast(conn)]
    except (sqlite3.Error, OSError, RuntimeError):
        axis = []
    if not axis:
        axis = TS.panel_dates(conn) if hasattr(TS, "panel_dates") else []
    if start:
        axis = [d for d in axis if d >= str(start)]
    if end:
        axis = [d for d in axis if d <= str(end)]
    return axis


def recent_validity(parts: Dict[str, pd.DataFrame], close_wide: pd.DataFrame,
                    horizon: int = 5, entry: str = "s1_open",
                    open_wide: Optional[pd.DataFrame] = None,
                    windows: Sequence[int] = (60, 120),
                    fwd: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """每个因子「最近还灵不灵」。

    为什么要单独看这一段
    --------------------
    全期 IC 显著只能说明"这五年里平均有效"。但因子是会失效的：
    一个因子可能三年前很猛、最近半年已经完全没用甚至反号。
    只看全期就会把这种"已经死掉的因子"继续按老权重用下去。

    判定（IC 已乘过方向，所以正常情况应当为**正**）：
        ok       最近窗口 IC > 0 且达到全期的一半以上 —— 还在工作
        weaker   同号但量级掉到全期一半以下 —— 在衰减
        reversed 反号 —— 近期已失效，继续用等于反向暴露
        unknown  样本不足

    Returns:
        {factor: {"full": {...}, "60": {...}, "120": {...}, "state": str, "note": str}}
    """
    # `generate()` 已经算过一次 fwd 并同时喂给毛/净两条链；不要在这里重读价格宽表。
    if fwd is None:
        fwd = fe.load_forward_returns(close_wide, horizon, entry=entry, open_wide=open_wide)
    lag = max(0, int(horizon) - 1)
    out: Dict[str, Any] = {}
    for name, part in parts.items():
        f = fwd.reindex(index=part.index)
        ic = fe.daily_ic(part, f, min_stocks=100).dropna()
        full_mean = float(ic.mean()) if len(ic) else None
        item: Dict[str, Any] = {
            "full": {"n_days": int(len(ic)), "ic_mean": fe_num(full_mean),
                     "t_nw": fe_num(fe.newey_west_t(ic, lag)) if len(ic) > 2 else None},
        }
        for w in windows:
            seg = ic.iloc[-int(w):]
            n = int(len(seg))
            if n < 20:
                item[str(w)] = {"n_days": n, "ic_mean": None, "t_nw": None}
                continue
            item[str(w)] = {
                "n_days": n,
                "ic_mean": fe_num(float(seg.mean())),
                "t_nw": fe_num(fe.newey_west_t(seg, lag)),
                "positive_ratio": fe_num(float((seg > 0).mean())),
            }

        # 用最短窗口判状态：它最能反映"现在还行不行"
        w0 = str(windows[0])
        r = item.get(w0, {}).get("ic_mean")
        if r is None or full_mean is None or abs(full_mean) < 1e-12:
            item["state"] = "unknown"
            item["note"] = "近期样本不足，无法判断"
        elif r <= 0:
            item["state"] = "reversed"
            item["note"] = ("最近 %s 天 IC 转负（%+.4f），与全期方向相反——近期已失效"
                            % (w0, r))
        elif abs(r) < abs(full_mean) * 0.5:
            item["state"] = "weaker"
            item["note"] = ("最近 %s 天 IC 只有全期的 %.0f%%（%+.4f vs %+.4f）——在衰减"
                            % (w0, abs(r) / abs(full_mean) * 100, r, full_mean))
        else:
            item["state"] = "ok"
            item["note"] = ("最近 %s 天 IC %+.4f，为全期的 %.0f%%——目前仍有效"
                            % (w0, r, abs(r) / abs(full_mean) * 100))
        out[name] = item
    return out


def validate(scores: pd.DataFrame, close_wide: pd.DataFrame, horizon: int = 5,
             entry: str = "s1_open", open_wide: Optional[pd.DataFrame] = None,
             fwd: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """
    验证综合分本身有没有效。

    这是整个选股链路最容易被跳过、也最不该跳过的一步：
    单因子都显著，不代表加起来还显著（可能互相抵消）。
    """
    # `generate()` 已经算过一次 fwd 并同时喂给毛/净两条链；不要在这里重读价格宽表。
    if fwd is None:
        fwd = fe.load_forward_returns(close_wide, horizon, entry=entry, open_wide=open_wide)
    ic_series = fe.daily_ic(scores, fwd, min_stocks=100)
    n = int(ic_series.notna().sum())
    if n < 10:
        return {"n_days": n, "ic_mean": None, "t_nw": None, "note": "有效天数不足"}

    ic_mean = float(ic_series.mean())
    ic_std = float(ic_series.std(ddof=1))
    lag = max(0, int(horizon) - 1)
    t_naive = ic_mean / ic_std * np.sqrt(n) if ic_std else None
    t_nw = fe.newey_west_t(ic_series.dropna(), lag) if hasattr(fe, "newey_west_t") else None

    # group_returns 返回 (DataFrame: index=date, columns=组号, Series: 第1组成分)
    # 这里要的是「各组跨日平均收益」，所以对 index 求均值，得到 Series(index=组号)
    groups_df, _ = fe.group_returns(scores, fwd, direction=1, n_groups=10, min_stocks=100)
    gmeans = groups_df.mean() if not groups_df.empty else pd.Series(dtype="float64")

    return {
        "n_days": n,
        "ic_mean": ic_mean,
        "ic_std": ic_std,
        "icir": (ic_mean / ic_std * np.sqrt(244)) if ic_std else None,
        "t_naive": float(t_naive) if t_naive is not None else None,
        "t_nw": fe_num(t_nw),
        "horizon": horizon,
        "entry": entry,
        "groups": [{"group": int(g), "mean": fe_num(m),
                    "annual": fe_num(fe._annualize(float(m), horizon))}
                   for g, m in gmeans.items()],
    }


# ---------------------------------------------------------------- 扣费验证链（T08）
#
# 为什么 IC 不能当扣费证据（硬事实，写进产物里）
# ----------------------------------------------
# `fwd_net = (1+fwd) * k[d] - 1`，k[d] 是**当天全市场同一个数**。
# 这对 Spearman IC 是单调变换：横截面排名一分不变 → IC（毛/净）逐位相等。
# 所以「扣费后 IC 还很高」什么也证明不了。扣费证据只有三种：
#   1. 纸面交易的净年化与 NW t（重叠持仓用 lag=h-1，不是 sqrt(n)）；
#   2. 全成本项翻倍的敏感性（net2<=0 直接 fail）；
#   3. 参数簇分布（不是挑最优那一格）。
COST_IC_NOTE = (
    "统一费率是横截面单调变换：(1+fwd)×k[d]−1 不改变同一天的排名，"
    "Spearman IC 天然不含成本信息 —— **不要拿 IC 证明扣费后有效**。"
    "扣费证据只看净年化、NW t 与翻倍敏感性。")


def validate_costed(scores: pd.DataFrame, valid: pd.DataFrame,
                    fwd: pd.DataFrame, close_wide: pd.DataFrame,
                    horizon: int = 5, entry: str = "s1_open",
                    top: int = 10, cost_profile: str = "a_share",
                    cost_multiplier: float = 1.0, min_trades: int = 50,
                    capital: float = 1_000_000.0,
                    n_unsettled: int = 0,
                    volume_wide: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """同一份 fwd 上算毛/净两条链；IC 只做诊断，判定只看净年化与 NW t。

    落盘字段名严格按架构契约 §7.3（`netTop10Annual` / `n_trades` /
    `gate.actual` / 小写 verdict），旧名作附加别名保留。
    """
    k = TC.adjust_factor(list(scores.index), int(horizon),
                         cost_profile, cost_multiplier)
    fwd_net = TC.net_forward(fwd.reindex(index=scores.index), k)
    gross_ic = fe.daily_ic(scores, fwd, min_stocks=100).dropna()
    net_ic = fe.daily_ic(scores, fwd_net, min_stocks=100).dropna()
    trade = TC.paper_trade(scores, valid, fwd, k, top_n=top, horizon=horizon,
                           min_valid_factors=2, min_trades=min_trades,
                           capital=capital, profile=cost_profile,
                           multiplier=cost_multiplier, volume_wide=volume_wide)

    # 十分组价差（第1组 - 第10组）按**净**收益算：这才是"扣费后还有没有分层"
    # 的直接证据；毛口径的 groups 留在顶层 validation，两处不互相覆盖。
    groups_df, _ = fe.group_returns(scores, fwd_net, direction=1,
                                    n_groups=10, min_stocks=100)
    spread_mean: Optional[float] = None
    spread_annual: Optional[float] = None
    if not groups_df.empty and 1 in groups_df.columns and 10 in groups_df.columns:
        spread = (groups_df[1] - groups_df[10]).dropna()
        if len(spread):
            spread_mean = float(spread.mean())
            spread_annual = fe._annualize(spread_mean, int(horizon))

    gate_src = trade.get("gate") or {}
    n_trades = int(trade["nTrades"])
    gate = {
        "requiredTrades": int(min_trades),
        "actual": n_trades,
        "requiredBuyDates": 10,
        "requiredDays": 50,
        "passed": bool(gate_src.get("passed")),
        "appliesTo": "full-window",
        "thresholds": gate_src.get("thresholds"),
        "reasons": gate_src.get("reasons"),
        "scope": gate_src.get("scope"),
    }
    net10 = fe_num(trade["annualNet"])
    t_nw = trade["tNw"]
    if not gate["passed"]:
        verdict = "insufficient"
    elif (float(net10) if net10 is not None else 0.0) <= 0 or (
            t_nw is not None and float(t_nw) <= 0):
        verdict = "fail"
    elif t_nw is None or float(t_nw) < 2.0:
        verdict = "weak"
    else:
        verdict = "pass"

    return {
        "basis": "costed",
        # ── 架构契约 §7.3 主字段 ──
        "n_days": int(trade["nDays"]),
        "n_trades": n_trades,
        "n_buy_dates": int(trade["nBuyDates"]),
        "n_unsettled": int(n_unsettled),
        "nUnbuyableExcluded": int(trade.get("nUnbuyableExcluded") or 0),
        "grossIcMean": fe_num(float(gross_ic.mean())) if len(gross_ic) else None,
        "netIcMean": fe_num(float(net_ic.mean())) if len(net_ic) else None,
        "icCostNote": COST_IC_NOTE,
        "netTopSpreadAnnual": fe_num(spread_annual),
        "netTop10Annual": net10,
        "tNw": t_nw,
        "gate": gate,
        "verdict": verdict,
        # ── 旧别名（附加，不参与判定）──
        "grossIc": fe_num(float(gross_ic.mean())) if len(gross_ic) else None,
        "netIc": fe_num(float(net_ic.mean())) if len(net_ic) else None,
        "nTrades": n_trades,
        "nBuyDates": int(trade["nBuyDates"]),
        "nDays": int(trade["nDays"]),
        "meanNet": fe_num(trade["meanNet"]),
        "annualNet": net10,
        "oneWayBps": fe_num(TC.one_way_bps(cost_profile, cost_multiplier)),
        "assumption": bool(TC.COST_PROFILES[cost_profile]["assumption"]),
        "source": TC.COST_PROFILES[cost_profile]["source"],
        "notCovered": list(TC.NOT_COVERED),
        "daily": trade["daily"],       # 只在内存里用，落盘前由 `_json_costed()` 剥掉
        "_trade": trade,               # 同上
    }


def _json_costed(costed: Optional[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """剥掉 DataFrame/集合等不可 JSON 化的字段，供落盘。"""
    if not costed:
        return None
    drop = {"daily", "_trade"}
    return {k: (v.to_dict() if isinstance(v, pd.Series) else v)
            for k, v in costed.items() if k not in drop}


def cost_sensitivity(scores: pd.DataFrame, valid: pd.DataFrame,
                     fwd: pd.DataFrame, close_wide: pd.DataFrame,
                     horizon: int = 5, entry: str = "s1_open",
                     top: int = 10, cost_profile: str = "a_share",
                     cost_multiplier: float = 1.0, min_trades: int = 50,
                     capital: float = 1_000_000.0,
                     volume_wide: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """全成本项翻倍：比较**净年化**，不是比较 IC（IC 对统一成本天然不变）。"""
    base = validate_costed(scores, valid, fwd, close_wide, horizon=horizon,
                           entry=entry, top=top, cost_profile=cost_profile,
                           cost_multiplier=cost_multiplier, min_trades=min_trades,
                           capital=capital, volume_wide=volume_wide)
    twice = validate_costed(scores, valid, fwd, close_wide, horizon=horizon,
                            entry=entry, top=top, cost_profile=cost_profile,
                            cost_multiplier=float(cost_multiplier) * 2.0,
                            min_trades=min_trades, capital=capital,
                            volume_wide=volume_wide)
    net1 = float(base.get("netTop10Annual") or 0.0)
    net2 = float(twice.get("netTop10Annual") or 0.0)
    t2 = twice.get("tNw")
    retained = (net2 / net1) if net1 > 0 else 0.0
    thresholds = {"minRetained": 0.5, "minT": 2.0}

    if not (base.get("gate") or {}).get("passed") or not (twice.get("gate") or {}).get("passed"):
        verdict = "insufficient"
        note = "基准或翻倍口径样本量未达门槛，成本敏感性无法判定（不等于 fail）"
    elif net2 <= 0:
        verdict = "fail"
        note = "全成本翻倍后净年化 %+.2f%% ≤ 0：当前边际完全被成本吃掉" % (net2 * 100)
    elif (t2 is not None and float(t2) >= thresholds["minT"]
          and retained >= thresholds["minRetained"]):
        verdict = "pass"
        note = ("翻倍后净年化 %+.2f%% > 0、t_NW=%.2f ≥ 2、保留 %.0f%% 边际"
                % (net2 * 100, float(t2), retained * 100))
    else:
        verdict = "weak"
        note = ("翻倍后净年化 %+.2f%% > 0，但 t_NW=%s、保留 %.0f%% —— "
                "不足以确认成本冲击下的稳健性"
                % (net2 * 100, ("%.2f" % float(t2)) if t2 is not None else "—",
                   retained * 100))
    return {
        "base": _json_costed(base),
        "doubled": _json_costed(twice),
        "net1": fe_num(net1), "net2": fe_num(net2),
        "tNw2": fe_num(t2),
        "retainedEdge": fe_num(retained),
        "thresholds": thresholds,
        "verdict": verdict,
        "note": note,
        "criterion": "样本不足→insufficient; net2<=0→fail; "
                     "net2>0 & tNw2>=2 & retainedEdge>=0.5→pass; else weak",
    }


def robustness_grid(conn: sqlite3.Connection, evald: Dict[str, Any],
                    axis: Sequence[str], scores: pd.DataFrame,
                    valid: pd.DataFrame, fwd5: pd.DataFrame,
                    close_wide: pd.DataFrame,
                    open_wide: Optional[pd.DataFrame] = None,
                    horizon: int = 5, entry: str = "s1_open",
                    top: int = 10, cost_profile: str = "a_share",
                    cost_multiplier: float = 1.0, min_trades: int = 50,
                    capital: float = 1_000_000.0,
                    n_periods_base: int = 0,
                    periods_base: Optional[Sequence[Dict[str, Any]]] = None,
                    volume_wide: Optional[pd.DataFrame] = None,
                    steps: Sequence[int] = (10, 20, 30),
                    windows: Sequence[int] = (90, 120, 150),
                    return_horizons: Sequence[int] = (4, 5, 6),
                    verbose: bool = False) -> Dict[str, Any]:
    """参数簇分布 + 收益口径敏感性。**只改网格参数，不改选股方向逻辑**。

    主网格：step × window 共 9 格，`selectionHorizon` 恒为 5 —— 因为
    `factor_ts` 的 IC/groups 是按 h=5 落盘的，换 horizon 重选需要重算整套序列，
    本轮不做，`notCovered` 里明写。`returnHorizon` 三档只换**收益标签**，
    .selectionHorizon 仍然是 5，两者不可混为一谈。

    期网格一律走 `build_scores()` → `RE.period_indices` / `RE.evaluate_period`，
    本函数**不写** `t_idx - horizon` 这样的自算下标（grep 断言会查）。
    """
    import rolling_eval as RE                     # noqa: F401 — 明确期口径来源

    base_step, base_window = int(RE.DEFAULTS["rollStep"]), int(RE.DEFAULTS["rollSelectWindow"])
    dates = list(axis)
    details: List[Dict[str, Any]] = []
    for step in steps:
        for window in windows:
            if int(step) == base_step and int(window) == base_window:
                s, vv = scores, valid
                n_periods = int(n_periods_base)
                cutoffs = [str(p.get("icCutoff")) for p in (periods_base or [])]
                t_indices = [int(p.get("tIndex")) for p in (periods_base or [])]
            else:
                try:
                    built = build_scores(conn, evald, axis=dates, verbose=False,
                                         mode=MODE_WF, horizon=int(horizon),
                                         roll_step=int(step), roll_window=int(window))
                    s, vv = built["scores"], built["valid"]
                    n_periods = len(built.get("periods") or [])
                    cutoffs = [str(p.get("icCutoff")) for p in (built.get("periods") or [])]
                    t_indices = [int(p.get("tIndex")) for p in (built.get("periods") or [])]
                except SystemExit as exc:
                    # 某一格排不出期不是「整轮失败」：按契约判该格 insufficient，
                    # 让参数簇整体降级而不是把 SystemExit 抛到 step_score 误报 exit 4。
                    details.append({"step": int(step), "window": int(window),
                                    "selectionHorizon": int(horizon),
                                    "nPeriods": 0, "cutoffs": [], "tIndices": [],
                                    "netAnnual": None,
                                    "nTrades": 0, "tNw": None,
                                    "gatePassed": False, "note": str(exc)[:120]})
                    if verbose:
                        print("    step=%d window=%d 无可用期，本格记 insufficient" %
                              (step, window))
                    continue
            c = validate_costed(s, vv, fwd5, close_wide, horizon=int(horizon),
                                entry=entry, top=top, cost_profile=cost_profile,
                                cost_multiplier=cost_multiplier,
                                min_trades=min_trades, capital=capital,
                                volume_wide=volume_wide)
            net_annual = fe_num(c.get("netTop10Annual"))
            details.append({
                "step": int(step), "window": int(window),
                "selectionHorizon": int(horizon),
                "nPeriods": n_periods,
                "cutoffs": cutoffs,
                "tIndices": t_indices,
                "netAnnual": net_annual,
                "nTrades": int(c["n_trades"]),
                "tNw": fe_num(c.get("tNw")),
                "gatePassed": bool(c["gate"]["passed"]),
            })
            if verbose:
                print("    step=%d window=%d 年化净 %s" % (step, window, net_annual))

    vals = sorted(float(g["netAnnual"]) for g in details if g.get("netAnnual") is not None)
    n = len(vals)

    def q(p: float) -> Optional[float]:
        if not n:
            return None
        i = int(p * (n - 1))
        return fe_num(vals[i])

    median = q(0.5)
    positive_ratio = fe_num(sum(1 for x in vals if x > 0) / n) if n else None
    net_annual_stats = {"median": median, "q25": q(0.25), "q75": q(0.75),
                        "min": fe_num(vals[0]) if n else None,
                        "max": fe_num(vals[-1]) if n else None}
    invalid = len(details) != len(steps) * len(windows) or any(
        int(g["nPeriods"]) < 1 or not g["gatePassed"] for g in details)
    if invalid:
        verdict = "insufficient"
    elif median is None or median <= 0:
        verdict = "fail"
    elif (positive_ratio is None or float(positive_ratio) < 7.0 / 9.0
          or net_annual_stats["min"] is None
          or float(net_annual_stats["min"]) <= -0.02):
        verdict = "weak"
    else:
        verdict = "pass"

    # 收益口径敏感性：selectionHorizon 保持 5，只换 fwd 的标签口径
    caliber: List[Dict[str, Any]] = []
    for rh in return_horizons:
        rh = int(rh)
        if rh == int(horizon):
            fwd_r = fwd5
        else:
            fwd_r = fe.load_forward_returns(close_wide, rh, entry=entry,
                                            open_wide=open_wide)
        k_r = TC.adjust_factor(dates, rh, cost_profile, cost_multiplier)
        fwd_net_r = TC.net_forward(fwd_r.reindex(index=scores.index), k_r)
        trade_r = TC.paper_trade(scores, valid, fwd_r, k_r, top_n=top, horizon=rh,
                                 min_trades=min_trades, capital=capital,
                                 profile=cost_profile, multiplier=cost_multiplier,
                                 volume_wide=volume_wide)
        ic_r = fe.daily_ic(scores, fwd_net_r, min_stocks=100).dropna()
        caliber.append({"returnHorizon": rh, "selectionHorizon": int(horizon),
                        "netAnnual": fe_num(trade_r["annualNet"]),
                        "tNw": fe_num(trade_r["tNw"]),
                        "nTrades": trade_r["nTrades"],
                        "netIc": fe_num(float(ic_r.mean())) if len(ic_r) else None,
                        "note": "只换收益标签口径；选因子与方向仍按 h=5 的结论"})

    return {
        # ── 架构契约 §7.5 主字段 ──
        "grid": {"step": list(steps), "window": list(windows),
                 "horizon": [int(horizon)]},
        "n": len(details),
        "netAnnual": net_annual_stats,
        "positiveRatio": positive_ratio,
        "details": details,
        "verdict": verdict,
        "notCovered": [
            {"param": "horizon",
             "reason": "factor_ts 按 h=5 落盘，换 horizon 重选需重算整套序列；本轮不做"},
            {"param": "selection",
             "reason": "只评网格内的这 9 格分布，不挑最优格做结论（挑最优就是过拟合）"},
            {"param": "returnHorizon",
             "reason": "4/6 只换收益标签，selectionHorizon 恒为 5，不可解读为换持有期重选"},
        ],
        "caliber": {"selectionHorizon": int(horizon),
                    "returnHorizon": list(return_horizons),
                    "returnHorizons": list(return_horizons),
                    "sensitivity": caliber},
        # ── 旧别名（附加）──
        "distribution": {"n": n, **net_annual_stats,
                         "positiveRatio": positive_ratio},
        "criterion": "任一组无有效期/门槛不过→insufficient; median<=0→fail; "
                     "positiveRatio<7/9 或 min<=-0.02→weak; 否则 pass（判分布，不判最优）",
    }


def aggregate_evidence(settlement: Dict[str, Any],
                       costed: Optional[Dict[str, Any]],
                       sensitivity: Optional[Dict[str, Any]],
                       robustness: Optional[Dict[str, Any]],
                       enforce_gate: bool = False,
                       gate_mode: str = "warn") -> Dict[str, Any]:
    """证据门槛聚合。**优先级写死，不让某一处 fail 被别处的 pass 盖掉。**

    优先级：结算失败 → fail > 样本门槛不过 → insufficient >
    costed/翻倍/参数簇任一 fail → fail > 任一 weak → weak > pass。
    `failed` 是前端 freshness 直接读的字段；`reasons` 只作旧别名。
    """
    failed: List[str] = []
    if not settlement.get("ok"):
        failed.append("结算失败：open 缺失 %d 格 / close 缺失 %d 格 / fwd 复算不符 %d 格"
                      % (int(settlement.get("missingOpen") or 0),
                         int(settlement.get("missingClose") or 0),
                         int(settlement.get("nFwdMismatch") or 0)))
        level = "fail"
    elif not (costed or {}).get("gate", {}).get("passed"):
        g = (costed or {}).get("gate") or {}
        failed.append("样本门槛不足：%s" % ("；".join(g.get("reasons") or [])
                                       or "未运行扣费验证"))
        level = "insufficient"
    else:
        verdicts: List[tuple] = []
        if costed and costed.get("verdict"):
            verdicts.append(("扣费后", str(costed.get("verdict"))))
        if sensitivity:
            verdicts.append(("费用翻倍", str(sensitivity.get("verdict"))))
        if robustness:
            verdicts.append(("参数簇", str(robustness.get("verdict"))))
        if any(v in ("fail", "insufficient") for _n, v in verdicts):
            level = "fail" if any(v == "fail" for _n, v in verdicts) else "insufficient"
        elif any(v == "weak" for _n, v in verdicts):
            level = "weak"
        else:
            level = "pass"
        failed.extend("%s：%s" % (n, v) for n, v in verdicts if v != "pass")
    note = ("；".join(failed) if failed
            else "结算通过、样本门槛达标、费用翻倍与参数簇均 pass")
    mode = "enforce" if enforce_gate else str(gate_mode or "warn")
    gate = {
        "passed": bool(level == "pass"),
        "level": level,
        "failed": failed,
        "mode": "enforce" if mode == "enforce" else "warn",
        "note": note,
        # ── 旧别名（附加）──
        "reasons": failed,
        "enforceGate": bool(enforce_gate),
        "scope": "entireValidationWindow",
        "notCovered": list(TC.NOT_COVERED),
    }
    if enforce_gate and level != "pass":
        raise EvidenceGateError("证据门槛未过（level=%s）：%s" % (level, "；".join(failed)))
    return gate


def oos_check(scores: pd.DataFrame, close_wide: pd.DataFrame, horizon: int = 5,
              entry: str = "s1_open", open_wide: Optional[pd.DataFrame] = None,
              split: float = 0.7,
              fwd: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """
    时间切分检验：前 70% 时间 vs 后 30% 时间分别算 IC。

    为什么必须有这一步
    ------------------
    候选因子是根据**全样本**的 t 值挑出来的。用同一份数据回头验证"这些因子加起来有效"，
    等于先看答案再出题，IC 天然偏乐观。真正能说明问题的是：
    把时间切成前后两段，如果后段（模型"没见过"的时间）IC 仍然同号且量级接近，
    才算这个合成方式没有过拟合到这一段行情。

    注意：这**不是**严格的 walk-forward（因子筛选仍用了全样本），
    只是成本最低、最能暴露问题的一道检查。真正的样本外要重跑因子筛选。
    """
    dates = list(scores.index)
    if len(dates) < 60:
        return {"ok": False, "note": "样本不足 60 天，无法切分"}
    cut = int(len(dates) * split)
    # `generate()` 已经算过一次 fwd 并同时喂给毛/净两条链；不要在这里重读价格宽表。
    if fwd is None:
        fwd = fe.load_forward_returns(close_wide, horizon, entry=entry, open_wide=open_wide)

    out: Dict[str, Any] = {"ok": True, "split": split,
                           "splitDate": dates[cut] if cut < len(dates) else None}
    for tag, seg in (("in", scores.iloc[:cut]), ("out", scores.iloc[cut:])):
        ic = fe.daily_ic(seg, fwd.reindex(index=seg.index),
                         min_stocks=100).dropna()
        n = len(ic)
        if n < 10:
            out[tag] = {"n_days": n, "ic_mean": None, "t_nw": None}
            continue
        t = fe.newey_west_t(ic, max(0, int(horizon) - 1))
        out[tag] = {"n_days": n, "ic_mean": float(ic.mean()),
                    "ic_std": float(ic.std(ddof=1)),
                    "t_nw": fe_num(t),
                    "ic_positive_ratio": float((ic > 0).mean())}

    a = out.get("in", {}).get("ic_mean")
    b = out.get("out", {}).get("ic_mean")
    if a is not None and b is not None:
        out["sameSign"] = (a > 0) == (b > 0)
        out["shrink"] = (abs(b) / abs(a)) if a else None
        t_out = out.get("out", {}).get("t_nw")
        # 判定要严：同号只是"没被打脸"，不代表后段证据够。
        # 必须后段自己 |t| >= 2 才能说"站得住"，否则只能说"方向一致但证据不足"。
        if not out["sameSign"]:
            out["verdict"] = "fail"
            out["comment"] = ("后段与前半段**反号**——合成分可能只在特定行情有效，"
                              "不能直接用于实盘。")
        elif t_out is not None and abs(t_out) >= 2:
            out["verdict"] = "pass"
            out["comment"] = ("后段与前半段同号，且后段自身 |t|=%.2f ≥ 2，"
                              "量级为前段的 %.0f%%——没有明显过拟合到前半段行情。"
                              % (abs(t_out), out["shrink"] * 100))
        else:
            out["verdict"] = "weak"
            out["comment"] = ("后段与前半段同号，量级为前段的 %.0f%%，但后段自身 |t|=%s < 2"
                              "（%d 天样本），只能说明「方向一致」，不足以确认有效。"
                              % (out["shrink"] * 100,
                                 ("%.2f" % abs(t_out)) if t_out is not None else "—",
                                 out.get("out", {}).get("n_days") or 0))
    return out


# ---------------------------------------------------------------- 信号生成

def factor_meta(names: List[str],
                sign_map: Optional[Dict[str, int]] = None) -> Dict[str, Dict[str, Any]]:
    """因子代号 → 中文名 + 一句话解释，供页面显示。页面上不能只给用户看 corr_pv20。

    Args:
        names: 需要展示的因子名。
        sign_map: `{因子: 有效方向}`（= 先验方向 × verdict 符号）。给了就同时输出
            `directionEff` 与 `flipped`：页面必须按 `directionEff` 说「越大越好/越小
            越好」，否则对「方向需翻转」的因子会说反 —— 它每天的实际用法是先验的反向。
            **不覆写 `direction`**：那是登记方向（先验），改了会让「为什么翻」无从解释。
    """
    import factor_plain
    from factors.registry import get as get_factor

    out: Dict[str, Dict[str, Any]] = {}
    for n in names:
        m = get_factor(n)
        p = factor_plain.plain_of(n, m.get("label", ""))
        d_prior = int(m["direction"])
        item: Dict[str, Any] = {
            "label": m.get("label", n),
            "direction": d_prior,
            "what": p.get("what", ""),
            "category": m.get("category", ""),
        }
        if sign_map is not None and n in sign_map:
            d_eff = int(sign_map[n])
            item["directionEff"] = d_eff
            # 翻不翻只看「有效方向是否与先验同号」——不猜 verdict 文字
            item["flipped"] = bool(d_eff != d_prior)
        out[n] = item
    return out


def make_picks(scores: pd.DataFrame, valid: pd.DataFrame, close_wide: pd.DataFrame,
               top_n: int = 10, min_valid_factors: int = 2,
               names: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """
    按最新交易日的综合分选出 Top N，并给出每只的买/卖信号与理由。

    信号规则（全部可回测，没有拍脑袋的"感觉"）：
        买入：综合分排名进入前 top_n，且有效因子数达标
        卖出：排名掉出 top_n*2，或综合分转负（从"好"变成"差"）
    这只是**规则**，不是**保证**。验证结论会一起输出。
    """
    names = names or {}
    dates = list(scores.index)
    if not dates:
        return {"date": None, "picks": []}
    last = dates[-1]
    row = scores.loc[last].dropna()
    vrow = valid.loc[last]
    row = row[vrow.reindex(row.index).fillna(0) >= min_valid_factors]
    if row.empty:
        return {"date": last, "picks": [], "note": "最新交易日没有可用打分"}

    ranked = row.sort_values(ascending=False)
    total = len(ranked)
    price_row = close_wide.loc[last] if last in close_wide.index else pd.Series(dtype="float64")

    picks = []
    for i, (sym, sc) in enumerate(ranked.head(top_n).items()):
        rank = i + 1
        pct = rank / total
        picks.append({
            "symbol": sym,
            "name": names.get(sym, ""),
            "close": fe_num(price_row.get(sym)),
            "score": float(sc),
            "rank": rank,
            "universe": total,
            "percentile": pct,
            "validFactors": int(vrow.get(sym, 0)),
            "signal": "买入观察",
            "reason": "综合分排名 %d / %d（前 %.1f%%）" % (rank, total, pct * 100),
            "exitRule": "排名掉出前 %d，或综合分转负" % (top_n * 2),
        })
    return {"date": last, "picks": picks}


def contributions(scores_parts: Dict[str, pd.DataFrame], sym: str,
                  date,
                  sign_map: Optional[Dict[str, int]] = None) -> List[Dict[str, Any]]:
    """某只票在某天，各因子分别贡献了多少分（用于页面解释"为什么选它"）。

    Args:
        sign_map: `{因子: 有效方向}`。给了就把 `flipped` 一起报出去 ——
            `contribution` 的符号已经按**有效**方向算好了（见 `build_walk_forward`
            的 `parts`），页面若仍按先验方向解释（"越大越好"），对翻转因子会说反。
    """
    from factors.registry import get as get_factor

    out = []
    for n, df in scores_parts.items():
        try:
            v = df.loc[date, sym]
        except KeyError:
            continue
        val = fe_num(v)
        if val is None:
            continue
        d_prior = int(get_factor(n)["direction"])
        item: Dict[str, Any] = {"factor": n, "contribution": val, "direction": d_prior}
        if sign_map is not None and n in sign_map:
            item["directionEff"] = int(sign_map[n])
            item["flipped"] = bool(int(sign_map[n]) != d_prior)
        out.append(item)
    out.sort(key=lambda x: -abs(x["contribution"]))
    return out


def inherit_recent_only(path: str) -> Optional[Dict[str, Any]]:
    """把上一版 `recentOnly` **原样继承**并打 `deprecated: true`。

    为什么不再每天重算这一版（决策 5）：它是「按最近 60 日状态挑因子」的组合，
    本质上仍是用近期表现去解释近期，样本薄且会随行情漂移。每日重算它只会
    让页面多出一份「看起来也很新」的对照，用户无法分清哪份是主结论。
    作为**历史断面冻结**是它最诚实的定位，所以这里只继承、并显式标注已停用。

    Returns:
        上一版的 `recentOnly`（含 `deprecated: true`），没有则 None。
    """
    try:
        with open(path, encoding="utf-8") as fh:
            previous = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(previous, dict):
        return None
    block = previous.get("recentOnly")
    if not isinstance(block, dict) or not block:
        return None
    out = dict(block)
    out["deprecated"] = True
    out["deprecatedNote"] = ("该版本已停用：它是按最近 60 日状态挑出的因子组合，"
                             "存在前视偏差，仅作历史断面保留")
    return out


def _apply_partial(ctx: Dict[str, Any], partial: bool,
                   warnings: Optional[list]) -> Dict[str, Any]:
    """把调用方已知的 `partial / warnings` 盖到 `freshness` 段上。

    为什么必须盖：本函数（打分/导出）跑在整轮 `finish` **之前**，
    那时 `factor_run.json` 里的 `running` 还在、`partial` 还没写。
    读状态文件只会永远读到 `partial=False`，页面就永远看不到
    「滚动这次没跑成」这句说明。状态机不知道的事，只能由编排层告诉它。
    """
    block = dict(ctx.get("freshness") or {})
    block["partial"] = bool(partial)
    block["warnings"] = [str(w) for w in (warnings or [])]
    ctx["freshness"] = block
    return ctx


def contract_fields(conn: sqlite3.Connection, eval_path: str,
                    factor_time: str = "16:10") -> Dict[str, Any]:
    """数据契约的上下文段（新鲜度 + 时间源 + 滚动验证 + 评估时点）。

    与 `export_factor_api.contract_fields()` 同源：都调 `freshness.compute()`
    与 `rolling_eval.compact()`，所以两页看到的时间与状态必然一致。
    """
    import freshness
    import rolling_eval
    from export_factor_api import eval_as_of

    out: Dict[str, Any] = {"evalAsOf": eval_as_of(eval_path),
                           "freshness": {}, "rolling": {}}
    try:
        fresh = freshness.compute(conn, factor_time=factor_time)
    except Exception:                            # noqa: BLE001 — 上下文缺失不阻断打分
        fresh = {}
    out["freshness"] = fresh
    for key in ("dataThrough", "lastCalcAt", "lastCalcOk", "lastCalcError",
                "nextRunAt", "partial", "warnings"):
        out[key] = fresh.get(key)
    try:
        out["rolling"] = rolling_eval.compact()
    except Exception:                            # noqa: BLE001
        out["rolling"] = {}
    return out


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    """`.tmp → os.replace`（共享知识第 7 条：页面读不到半份 JSON）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def generate(conn: sqlite3.Connection, eval_path: str = EVAL, out: str = OUT,
             horizon: int = 5, entry: str = "s1_open", top: int = 10,
             weight: str = "equal", recent_filter: str = "inherit",
             only: Optional[List[str]] = None,
             start: Optional[str] = None, end: Optional[str] = None,
             factor_time: str = "16:10",
             partial: bool = False,
             warnings: Optional[List[str]] = None,
             mode: str = MODE_WF,
             roll_step: int = 20, roll_window: int = 120,
             cost_profile: str = "a_share", cost_multiplier: float = 1.0,
             min_trades: int = 50, gate_mode: str = "warn",
             enforce_gate: bool = False, run_robustness: bool = True,
             verbose: bool = True) -> Dict[str, Any]:
    """打分并写 `data/factor_score.json`（供 `run_factor_pipeline.step_score` 调用）。

    Args:
        recent_filter: `"inherit"`（默认，每日 pipeline 用）= 只继承上一版
            `recentOnly` 并打 `deprecated`；`"drop"` = 现场重算一版对照
            （仅供手动 `重新计算选股.bat` 使用）。
        factor_time: 因子任务时点，用于 `nextRunAt`。
        mode: `"walkforward"`（默认，逐期、无前视）/ `"legacy"`（旧全样本口径）。
        roll_step / roll_window: 期网格口径，必须与 `rolling_eval` 一致，
            否则逐日 `signAsOf` 与页面上的「滚动验证」卡片是两套期，不可互证。
        cost_profile / cost_multiplier: 成本档位与倍数（2 = 全成本项翻倍）。
        min_trades: 纸面交易最低笔数（另需 nBuyDates>=10 且 nDays>=50）。
        gate_mode: `"warn"`（默认，写盘照常、证据不足写 warnings）；
            `"enforce"` 等价于 `enforce_gate=True`。
        enforce_gate: 只有显式为真，才在 `_write_json_atomic()` 之前拦下未过门槛的名单。

    Returns:
        `{"ok", "out", "factors", "picks", "validation", "validationFullSample",
          "directionBasis", "contract"}`
    """
    with open(eval_path, encoding="utf-8") as fh:
        evald = json.load(fh)

    # 逐期打分需要「至少一期」可用结论（面板 >= horizon + MIN_USABLE_DAYS）。
    # 小样本（如新市场刚回补、单市场只有 25 天）时一期都排不出来 —— 这不是
    # 「打分做不出来」，而是这种规模下根本无法逐期判定。此时**降级到 legacy
    # 全样本口径**（它本来就是为对照/回滚保留的），整轮仍 partial 成功、产物照写，
    # 并在 warnings 里明写降级原因。绝不静默：mode 字段会变成 legacy，
    # validation.basis 也随之变成 full-sample，页面据此显示「含轻度前视」。
    # 黑盒约束：只有「无可用期/面板天数不足」这类可识别的口径原因才降级；
    # 其他异常（如面板载入失败）照旧向上抛，不掩盖真故障。
    fallback_reason: Optional[str] = None
    try:
        built = build_scores(conn, evald, start=start, end=end, weight=weight,
                             only=only, verbose=verbose, mode=mode,
                             horizon=horizon, roll_step=roll_step,
                             roll_window=roll_window)
    except SystemExit as exc:
        msg = str(exc) or ""
        if mode == MODE_WF and ("不足 horizon" in msg or "没有可用期" in msg
                                or "没有任何一天能配上" in msg):
            fallback_reason = ("逐期打分降级为全样本口径（%s）：面板样本不足以排出"
                               "任何一期，本轮名单含轻度前视，仅供参考" % msg)
            built = build_scores(conn, evald, start=start, end=end, weight=weight,
                                 only=only, verbose=verbose, mode=MODE_LEGACY,
                                 horizon=horizon, roll_step=roll_step,
                                 roll_window=roll_window)
        else:
            raise
    scores, valid = built["scores"], built["valid"]
    sign_map: Dict[str, int] = dict((built.get("directionBasis") or {}).get("signed") or {})

    close_wide = fe.load_price_wide(conn, "close")
    open_wide = fe.load_price_wide(conn, "open")
    # 成交量宽表只在这里读一次，和 close/open/fwd 共用同一份快照。
    # 0 是有效观测（停牌/一字板）→ load_price_wide 对 volume 不做 <=0→NaN。
    # 拿不到时必须明写 notCovered，不能让「没查」显示成「查了且为 0」。
    volume_missing_note: Optional[str] = None
    try:
        volume_wide = fe.load_price_wide(conn, "volume")
        if volume_wide is None or volume_wide.empty:
            volume_wide = None
            volume_missing_note = "成交量宽表为空：T+1 volume=0 不可买检查未启用"
    except (sqlite3.Error, ValueError) as exc:
        volume_wide = None
        volume_missing_note = "成交量宽表不可用（%s）：T+1 volume=0 不可买检查未启用" % exc
    try:
        names = dict(conn.execute("SELECT symbol, name FROM symbol_meta").fetchall())
    except sqlite3.Error:
        names = {}
    # 只保留打分覆盖到的票，避免宽表把内存放大
    keep = [c for c in scores.columns if c in close_wide.columns]
    close_wide = close_wide[keep]
    open_wide = open_wide[keep]
    if volume_wide is not None:
        volume_wide = volume_wide.reindex(columns=keep)

    # 未来收益宽表**只算一次**：毛口径给 IC/分组，扣费口径在同一份 fwd 上乘 k[d]。
    # 重复 load 会让「毛/净」可能来自两次不同价格快照，那种差异最难查。
    fwd = fe.load_forward_returns(close_wide, horizon, entry=entry,
                                  open_wide=open_wide)

    if verbose:
        print("\n检查各因子「最近还灵不灵」（最近 60 / 120 交易日）…")
    recent = recent_validity(built["parts"], close_wide, horizon=horizon,
                             entry=entry, open_wide=open_wide, fwd=fwd)
    if verbose:
        for n in built["factors"]:
            r = recent.get(n, {})
            print("  %-14s %-8s %s" % (n, r.get("state", "—"), r.get("note", "")))

        print("\n验证综合分（这是关键：单因子显著不代表加起来还显著）…")
    v = validate(scores, close_wide, horizon=horizon, entry=entry,
                 open_wide=open_wide, fwd=fwd)
    # 主指标的「身世」必须写在它自己身上：页面/报告只看 validation，
    # 若不说清这一版是无前视的，读者就无法判断这个 IC 该不该信。
    v["basis"] = "walk-forward" if built.get("mode") == MODE_WF else "full-sample"
    v["criterion"] = WF_CRITERION if built.get("mode") == MODE_WF else None
    v["nUnscoredDays"] = int(((built.get("signAsOf") or {}).get("nUnscoredDays")) or 0)
    v["note"] = ("逐期 walk-forward：每日只用当时已有结论（<= icCutoff）的因子与方向，"
                 "回测区间与判定数据不重叠；前 %d 天没有分数"
                 "（首个 cutoff 之前不打分，早期期还可能一个因子都选不出来）"
                 % v["nUnscoredDays"]) if built.get("mode") == MODE_WF else (
        "全样本口径：候选因子按全样本 t 值挑、方向用先验 —— **含轻度前视**，只作对照")

    # 对照：全样本口径（含轻度前视）。**不出名单**，只给指标。
    v_full: Optional[Dict[str, Any]] = None
    fs_scores = built.get("scoresFullSample")
    if fs_scores is not None and not fs_scores.empty:
        v_full = validate(fs_scores, close_wide, horizon=horizon, entry=entry,
                          open_wide=open_wide, fwd=fwd)
        v_full["basis"] = "full-sample"
        v_full["lookahead"] = True
        v_full["note"] = ("含轻度前视：候选因子按全样本 t 值挑、方向用先验方向。"
                          "只作上限参考，**不是**这份名单的验证结论")
    if verbose:
        print("  有效天数 %s | IC %.4f | t_NW %s | 十分组第1组 %.2f%% / 第10组 %.2f%%"
              % (v.get("n_days"), v.get("ic_mean") or 0, v.get("t_nw"),
                 (v["groups"][0]["annual"] or 0) * 100 if v.get("groups") else 0,
                 (v["groups"][-1]["annual"] or 0) * 100 if v.get("groups") else 0))
        if v_full is not None:
            print("  对照（全样本口径，含轻度前视）：IC %s | t_NW %s"
                  % (("%.4f" % v_full["ic_mean"]) if v_full.get("ic_mean") is not None else "-",
                     v_full.get("t_nw")))

    picks = make_picks(scores, valid, close_wide, top_n=top, names=names)
    if verbose:
        print("\n最新交易日 %s 的推荐名单（前 %d）：" % (picks.get("date"), top))
    for p in picks.get("picks", []):
        # 每只票补上「哪几个因子给的分数」，否则用户只能看到一个毫无信息量的总分
        p["contributions"] = contributions(built["parts"], p["symbol"],
                                           picks.get("date"), sign_map=sign_map)
        if verbose:
            print("  %s %-6s 分数 %+.4f  排名 %d/%d" %
                  (p["symbol"], p.get("name", ""), p["score"], p["rank"], p["universe"]))

    oos = oos_check(scores, close_wide, horizon=horizon, entry=entry,
                    open_wide=open_wide, fwd=fwd)
    if verbose:
        if oos.get("ok"):
            i, o = oos.get("in", {}), oos.get("out", {})
            print("\n时间切分检验（前 %.0f%% / 后 %.0f%%）："
                  % (oos["split"] * 100, 100 - oos["split"] * 100))
            print("  前半段 IC %s (t=%s, %s 天) | 后半段 IC %s (t=%s, %s 天)" %
                  (("%.4f" % i["ic_mean"]) if i.get("ic_mean") is not None else "-",
                   i.get("t_nw"), i.get("n_days"),
                   ("%.4f" % o["ic_mean"]) if o.get("ic_mean") is not None else "-",
                   o.get("t_nw"), o.get("n_days")))
            print("  结论：%s" % oos.get("comment"))
            print("  判定：%s" % {"pass": "通过（后段独立显著）",
                               "weak": "偏弱（方向一致但后段不显著）",
                               "fail": "不通过（前后段反号）"}.get(oos.get("verdict"), "—"))
        else:
            print("\n时间切分检验跳过：%s" % oos.get("note"))

    # ── T08：结算 → 扣费 → 翻倍 → 参数簇 → 证据门槛 ──────────────────
    # 全部复用上面那一份 `fwd`，不重读价格宽表；期网格一律走 build_scores 里的
    # RE.period_indices / RE.evaluate_period，本文件不自算 t_idx-horizon。
    settlement = TC.settle_check(close_wide, open_wide, fwd, horizon, entry=entry,
                                 volume_wide=volume_wide)
    if volume_missing_note:
        settlement["notCovered"] = list(settlement.get("notCovered") or []) + [
            volume_missing_note]
    costed = validate_costed(scores, valid, fwd, close_wide, horizon=horizon,
                             entry=entry, top=top, cost_profile=cost_profile,
                             cost_multiplier=cost_multiplier,
                             min_trades=min_trades,
                             n_unsettled=int(settlement.get("nUnsettled") or 0),
                             volume_wide=volume_wide)
    v["costed"] = _json_costed(costed)
    v["icCostNote"] = COST_IC_NOTE
    sensitivity = cost_sensitivity(scores, valid, fwd, close_wide, horizon=horizon,
                                   entry=entry, top=top, cost_profile=cost_profile,
                                   cost_multiplier=cost_multiplier,
                                   min_trades=min_trades,
                                   volume_wide=volume_wide)
    robustness: Optional[Dict[str, Any]] = None
    if run_robustness:
        robustness = robustness_grid(
            conn, evald, list(scores.index), scores, valid, fwd, close_wide,
            open_wide=open_wide, horizon=horizon, entry=entry, top=top,
            cost_profile=cost_profile, cost_multiplier=cost_multiplier,
            min_trades=min_trades,
            n_periods_base=len(built.get("periods") or []),
            periods_base=built.get("periods"), volume_wide=volume_wide,
            verbose=verbose)

    # OOS 后段单独判一次：不过就把 oos.verdict 降为 weak（不升、不改 fail）
    if oos.get("ok") and len(scores.index) >= 20:
        cut = int(len(scores.index) * float(oos.get("split") or 0.7))
        oos_s, oos_v = scores.iloc[cut:], valid.iloc[cut:]
        if not oos_s.empty:
            oos_costed = validate_costed(
                oos_s, oos_v, fwd.reindex(index=oos_s.index), close_wide,
                horizon=horizon, entry=entry, top=top, cost_profile=cost_profile,
                cost_multiplier=cost_multiplier, min_trades=min_trades,
                volume_wide=volume_wide)
            oos["costed"] = _json_costed(oos_costed)
            t_oos = oos_costed.get("tNw")
            ann_oos = float(oos_costed.get("annualNet") or 0.0)
            oos_ok = bool(oos_costed["gate"]["passed"] and ann_oos > 0
                          and t_oos is not None and float(t_oos) >= 2.0)
            if not oos_ok and oos.get("verdict") == "pass":
                oos["verdict"] = "weak"
                oos["downgradedByCost"] = True
                oos["comment"] = str(oos.get("comment") or "") + (
                    "；扣费后段单独检验未过（净年化 %+.2f%% / t=%s），判定降为 weak"
                    % (ann_oos * 100, ("%.2f" % float(t_oos)) if t_oos is not None else "—"))

    # cost 段的字段结构来自架构契约 §7.1：rates 九键 + roundTripBps +
    # assumedPositionNotional + minCommissionBinding + priceAdjust + sources。
    # 由 trade_cost.profile_payload() 一处生成，避免打分端与成本端费率表分叉。
    cost_payload: Dict[str, Any] = TC.profile_payload(cost_profile, cost_multiplier)

    gate = aggregate_evidence(settlement, costed, sensitivity, robustness,
                              enforce_gate=bool(enforce_gate or gate_mode == "enforce"),
                              gate_mode=gate_mode)
    if verbose:
        print("\n扣费验证：净年化 %s / 翻倍 %s（%s）| 结算 %s | 证据门槛 %s"
              % (("%+.2f%%" % (float(costed["annualNet"]) * 100))
                 if costed.get("annualNet") is not None else "—",
                 ("%+.2f%%" % (float(sensitivity["net2"]) * 100))
                 if sensitivity.get("net2") is not None else "—",
                 sensitivity.get("verdict"),
                 "ok" if settlement["ok"] else "FAIL",
                 gate["level"]))

    # 默认只继承上一版（打 deprecated），不再每天重算「仅近期有效」这一版。
    # 现场重算仅在显式 `--recent-filter drop`（手动 bat）时发生。
    recent_only: Optional[Dict[str, Any]] = inherit_recent_only(out)
    if recent_filter == "drop":
        keep_names = [n for n in built["factors"]
                      if (recent.get(n) or {}).get("state") != "reversed"]
        dropped = [n for n in built["factors"] if n not in keep_names]
        if keep_names and dropped:
            parts_ok = {n: built["parts"][n] for n in keep_names}
            s2, v2 = combine_parts(parts_ok, {n: 1.0 for n in keep_names})
            v_ok = validate(s2, close_wide, horizon=horizon, entry=entry,
                            open_wide=open_wide)
            picks2 = make_picks(s2, v2, close_wide, top_n=top, names=names)
            for p in picks2.get("picks", []):
                p["contributions"] = contributions(parts_ok, p["symbol"],
                                                   picks2.get("date"))
            recent_only = {
                "factors": keep_names,
                "dropped": dropped,
                "weights": {n: 1.0 / len(keep_names) for n in keep_names},
                "validation": v_ok,
                # 这一版也必须过切分检验：剔掉因子后样本更薄，
                # 只报一个更漂亮的 IC 而不给前后段证据，就是在骗人。
                "oos": oos_check(s2, close_wide, horizon=horizon, entry=entry,
                                 open_wide=open_wide, fwd=fwd),
                "picks": picks2,
                "generatedAt": time.strftime("%Y-%m-%d %H:%M"),
                "deprecated": False,
            }
            if verbose:
                print("\n【对照】剔除最近已失效的 %d 个因子（%s）后，剩 %d 个："
                      % (len(dropped), ", ".join(dropped), len(keep_names)))
        elif verbose:
            print("\n【对照】没有因子被剔除（近期全部仍有效），跳过第二版")

    # 若上面发生过「无可用期 → legacy」降级，必须让整条链都看见：
    # payload.partial 置真、warnings 明写、freshness.warnings 同步。
    # 口径本身已经由 built["mode"] 自动变成 legacy、validation.basis 变成
    # full-sample（见上方 v["basis"] 赋值），这里只补 partial/warning，
    # 不重复改口径字段，避免两处口径不一致。
    if fallback_reason:
        warnings = list(warnings or []) + [fallback_reason]
        partial = True
    # warn 模式（默认）：证据不足照常写盘，但必须在 warnings 与 freshness 里可见。
    # 「静默地给出一份证据不足的名单」比直接失败更危险 —— 用户会当它过了关。
    if gate["level"] != "pass":
        warnings = list(warnings or []) + [
            "证据门槛 %s：%s（名单照常产出，仅供观察，不代表扣费后有效）"
            % (gate["level"], "；".join(gate["reasons"][:3]))]
        partial = True

    # enforce 模式由 `aggregate_evidence()` 直接抛 `EvidenceGateError`；上面这段
    # 到 `_write_json_atomic()` 之间没有任何写盘动作，所以页面必然保留上一份名单。
    # 绝不能先写新名单再报失败 —— 那会出现「新名单 + 更新失败」的自相矛盾。

    ctx = contract_fields(conn, eval_path, factor_time=factor_time)
    ctx = _apply_partial(ctx, partial, warnings)
    payload = {
        "generatedAt": time.strftime("%Y-%m-%d %H:%M"),
        # 打分口径的「身世」：读这份文件的人必须能一眼看出这两处前视已经消掉
        "mode": built.get("mode") or MODE_WF,
        "modeNote": built.get("note"),
        "weight": weight,
        "horizon": horizon,
        "entry": entry,
        "factors": built["factors"],
        # directionEff / flipped 只对当前生效期的那批因子有意义（见 directionBasis）
        "factorMeta": factor_meta(built["factors"], sign_map=sign_map),
        "weights": {k: fe_num(w) for k, w in (built.get("weights") or {}).items()},
        "clusters": built["clusters"],
        "tmap": {k: fe_num(v2) for k, v2 in (built.get("tmap") or {}).items()},
        "validation": v,
        # 对照：全样本口径（含轻度前视）。**字段名与 validation 相同**，
        # 但带 basis/lookahead/note —— 页面不许拿它当结论，也不许并列成「可切换的名单」
        "validationFullSample": v_full,
        # ── T08 扣费验证链六段 ──
        "cost": cost_payload,
        "settlement": settlement,
        "costSensitivity": sensitivity,
        "robustness": robustness,
        "evidenceGate": gate,
        "gateMode": gate_mode,
        # 当前生效期的方向依据：谁被翻了、翻成什么符号
        "directionBasis": built.get("directionBasis"),
        # 逐日生效期（阶梯函数）：回答「这天为什么这么算」
        "signAsOf": built.get("signAsOf"),
        "recent": recent,
        "oos": oos,
        "picks": picks,
        "recentOnly": recent_only,
        # ── 数据契约：时间源与新鲜度（页面不再自己算日期）──
        "evalAsOf": ctx["evalAsOf"],
        "dataThrough": ctx["dataThrough"],
        "lastCalcAt": ctx["lastCalcAt"],
        "lastCalcOk": ctx["lastCalcOk"],
        "lastCalcError": ctx["lastCalcError"],
        "nextRunAt": ctx["nextRunAt"],
        # 「主体成功、个别环节缺」（目前只有滚动无有效期）：
        # 它不进 freshness.status，页面靠它把滚动卡片写上一行灰字
        "partial": bool(partial),
        "warnings": [str(w) for w in (warnings or [])],
        "freshness": ctx["freshness"],
        "rolling": ctx["rolling"],
    }
    _write_json_atomic(out, payload)
    if verbose:
        print("\n已写出 %s" % out)
    return {"ok": True, "out": out, "factors": len(built["factors"]),
            "picks": len(picks.get("picks") or []),
            "mode": built.get("mode") or MODE_WF,
            "validation": {"n_days": v.get("n_days"), "ic_mean": v.get("ic_mean"),
                           "t_nw": v.get("t_nw")},
            "validationFullSample": None if v_full is None else
            {"n_days": v_full.get("n_days"), "ic_mean": v_full.get("ic_mean"),
             "t_nw": v_full.get("t_nw")},
            "directionBasis": built.get("directionBasis"),
            "recentOnlyDeprecated": bool((recent_only or {}).get("deprecated")),
            "evidenceGate": gate,
            "settlement": {"ok": settlement["ok"],
                           "nUnsettled": settlement["nUnsettled"],
                           "nUnbuyable": settlement["nUnbuyable"]},
            "costSensitivity": {"net1": sensitivity.get("net1"),
                                "net2": sensitivity.get("net2"),
                                "verdict": sensitivity.get("verdict")},
            "contract": {k: ctx[k] for k in ("evalAsOf", "dataThrough",
                                             "lastCalcAt", "nextRunAt")}}


def main() -> int:
    ap = argparse.ArgumentParser(description="因子合成打分与选股信号")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--eval", default=EVAL, dest="eval_path")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--horizon", type=int, default=5)
    ap.add_argument("--entry", default="s1_open")
    ap.add_argument("--top", type=int, default=10)
    ap.add_argument("--weight", choices=["equal", "ic", "t"], default="equal",
                    help="合成分的加权方式；默认等权，最不容易过拟合")
    ap.add_argument("--recent-filter", choices=["inherit", "drop"], default="inherit",
                    help="inherit（默认）= 只继承上一版 recentOnly 并打 deprecated；"
                         "drop = 现场重算一版对照（手动 bat 用）")
    ap.add_argument("--factors", default="",
                    help="逗号分隔，显式指定参与合成的因子（跳过按评估分档自动挑选）")
    ap.add_argument("--start", default=None)
    ap.add_argument("--end", default=None)
    ap.add_argument("--factor-time", default="16:10",
                    help="因子任务时点，用于计算 nextRunAt")
    ap.add_argument("--legacy", action="store_true",
                    help="用旧的全样本口径打分（含轻度前视），仅供对照与回滚")
    ap.add_argument("--roll-step", type=int, default=20,
                    help="逐期重选的期步长（交易日），必须与 rolling_eval 一致")
    ap.add_argument("--roll-window", type=int, default=120,
                    help="每期回看多少日 IC 来判定，必须与 rolling_eval 一致")
    ap.add_argument("--cost-profile", default="a_share", choices=sorted(TC.COST_PROFILES),
                    help="成本档位；默认 a_share（含 10bp/边滑点假设）")
    ap.add_argument("--cost-multiplier", type=float, default=1.0,
                    help="全成本项倍数；2 = 翻倍敏感性口径")
    ap.add_argument("--min-trades", type=int, default=50,
                    help="纸面交易最低笔数（另需 >=10 买入日且 >=50 有效日）")
    ap.add_argument("--gate-mode", choices=["warn", "enforce"], default="warn",
                    help="warn（默认）= 证据不足照常写盘 + warnings；enforce = 写盘前拦")
    ap.add_argument("--enforce-gate", action="store_true",
                    help="等价于 --gate-mode=enforce（显式开关）")
    ap.add_argument("--no-robustness", action="store_true",
                    help="跳过 9 格参数簇与收益口径敏感性（调试用；默认跑）")
    args = ap.parse_args()
    if args.horizon < 1:
        raise SystemExit("--horizon 必须 >= 1")

    conn = sqlite3.connect(args.db)
    try:
        res = generate(conn, eval_path=args.eval_path, out=args.out,
                       horizon=args.horizon, entry=args.entry, top=args.top,
                       weight=args.weight, recent_filter=args.recent_filter,
                       only=[s.strip() for s in args.factors.split(",") if s.strip()] or None,
                       start=args.start, end=args.end,
                       factor_time=args.factor_time,
                       mode=MODE_LEGACY if args.legacy else MODE_WF,
                       roll_step=args.roll_step, roll_window=args.roll_window,
                       cost_profile=args.cost_profile,
                       cost_multiplier=args.cost_multiplier,
                       min_trades=args.min_trades,
                       gate_mode=args.gate_mode,
                       enforce_gate=args.enforce_gate,
                       run_robustness=not args.no_robustness,
                       verbose=True)
    finally:
        conn.close()
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
