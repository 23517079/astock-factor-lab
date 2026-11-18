#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""rolling_eval.py — 滚动验证：每个时点只用「当时可得」的证据重新挑因子

要解决的缺陷
------------
`data/factor_score.json` 里的「仅近期有效 7 因子」是按**最近 60 日**表现挑的
（这一挑法本身没问题），但回测却跑了 2021 年起的全历史，于是得出 IC 0.0783 /
t=11.29 —— 那是用「今天知道谁最近有效」去解释五年前的数据，属于前视偏差。

滚动验证的规则：把时间切成若干期（每 `rollStep=20` 个交易日一期），
在第 k 期只允许看 `date <= T_k - horizon` 的 IC 序列来选因子，
再用被选中的因子在这一期之后的表现记账。这样得到的平均 IC 才是**真实可达**水平。

约束 A：前视泄漏的唯一入口
--------------------------
`fwd[d]` 用到 `open[d+1]` 与 `close[d+h]`，所以在时点 T 选因子时，
可用于评判的 IC 序列最后一行最多到 `dates[idx(T) - horizon]`。

    ic_cutoff(dates, t_idx, horizon) -> dates[t_idx - horizon]   # **唯一入口**

三道防线，一个都不能删：
  1. 切片层：`factor_ts.slice_until()` 内部断言 `max(dates) <= cut`（本模块传 cut）；
  2. 本模块：`assert period.icCutoff == dates[idx(T)-h]`（`ic_cutoff` 是唯一生成点）；
  3. 数据层：`ts.last <= panelMaxDate - horizon`（`verify_lag()`；
     滞后 `>= horizon` 都正常，`min_stocks=100` 会让个别日子无 IC，
     **只有 `< horizon` 才是实现错误**，所以禁止写 `== horizon` 的断言）。

为什么这里**不自算 IC 序列**
----------------------------
逐日 IC 一旦算出就稳定（当日横截面的度量，不随后续数据变化），
`factor_ts` 已经把它和逐日 10 组收益一起落盘。本模块只做「切片 + 判定」，
所以整轮滚动验证是秒级而不是小时级（实测预期 ~70s 主要在首次读盘）。

判定必须复用，不得另写
----------------------
    `export_factor_api.verdict_of`  —— 档位唯一来源（选中集合 = {值得复核, 方向需翻转}）
    `factor_eval.monotonicity_of`   —— 窗口单调性唯一来源

如果 `mono` 拿不到，**所有统计显著的因子都会落到「仅统计显著」→ 选中集合恒为空
且全链路不报错**。这就是 `factor_ts` 必须同时持久化 `groups` 的原因。

用法
----
    python rolling_eval.py                 # 写 data/factor_rolling.json
    python rolling_eval.py --step 20 --window 120 --horizon 5
    # 日常由 run_factor_pipeline.py 的 step_rolling 调用 run()
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import factor_run as FR                        # noqa: E402
import factor_ts as TS                         # noqa: E402
from factors import registry                   # noqa: E402

DB = os.path.join(HERE, "data", "sequoia_v2.db")
OUT_DEFAULT = os.path.join(HERE, "data", "factor_rolling.json")

# 选中集合 = 这两档（共享知识第 4 条，判定档位唯一来源 verdict_of）
SELECTED_VERDICTS: Tuple[str, str] = ("值得复核", "方向需翻转")
# 窗口内可用天数下限：少于这个数，该因子本期判 unknown、不入选
# （与 factor_score.recent_validity 的 20 天阈值保持一致）
MIN_USABLE_DAYS = 20

DEFAULTS: Dict[str, Any] = {
    "horizon": 5,               # IC 的前瞻期（交易日）
    "entry": "s1_open",         # 建仓口径（只用于展示与口径核对，本模块不自算标签）
    "minStocks": 100,           # 逐日横截面最小有效样本
    "nwLag": 4,                 # Newey-West 滞后阶数（= horizon - 1）
    "nGroups": 10,              # 分组数
    "rollStep": 20,             # 每期前进多少个交易日
    "rollSelectWindow": 120,    # 每期回看多少日 IC 来选因子
}


# ────────────────────────────────────────────────────────────
# 约束 A 的唯一入口
# ────────────────────────────────────────────────────────────
def ic_cutoff(dates: Sequence[str], t_idx: int, horizon: int) -> str:
    """时点 `dates[t_idx]` 可用于评判 IC 的**最晚**日期 = `dates[t_idx - horizon]`。

    这是「无前视铁律」在全库的唯一入口（共享知识第 9 条）：别处一律不许写
    `ic.loc[:T]`，必须调本函数拿 cut 再切片。

    Args:
        dates: 交易日序列（升序，来自 `factor_ts.panel_dates_fast()`）。
        t_idx: 当前时点在 dates 里的下标。
        horizon: 前瞻期（交易日）。

    Returns:
        `dates[t_idx - horizon]`。

    Raises:
        ValueError: `t_idx - horizon < 0`（没有足够历史，无法做无前视评估）。
    """
    j = int(t_idx) - int(horizon)
    if j < 0:
        raise ValueError(
            "ic_cutoff：t_idx=%d 减 horizon=%d 为负，没有足够历史可做无前视评估"
            % (t_idx, horizon))
    if j >= len(dates):
        raise ValueError("ic_cutoff：t_idx=%d 超出 dates 长度 %d" % (t_idx, len(dates)))
    return str(dates[j])


def verify_lag(dates: Sequence[str], horizon: int, strict: bool = True,
               exclude: Optional[Iterable[str]] = None) -> Dict[str, Any]:
    """数据层探针：`ts.last <= panelMaxDate - horizon`（共享知识第 13 条）。

    滞后 `>= horizon` 都正常（`min_stocks=100` 会让个别日子无 IC），
    只有 `< horizon` 才是错误实现 —— 抓的是「用 NaN / 前向填充凑出 T 日 IC」
    这一类错误，成本几乎为零。

    Args:
        exclude: 已经判定为 `suspect` 的因子。它们本来就会被滚动验证跳过
            （§5.7），若还让它们的越界末端把整轮验证判失败，就变成
            「一个因子的数据问题 → 今天完全不出滚动验证」，与
            「跳过该因子、页面标注可疑」的既定处置相悖。
            这些因子会单独记在返回值的 `suspects` 里，**不是**被忽略。
    """
    cutoff = TS.cutoff_date(dates, horizon)
    skipped = set(exclude or ())
    out: Dict[str, Any] = {"cutoff": cutoff, "ok": True, "violations": [],
                           "lasts": {}, "suspects": []}
    if cutoff is None:
        out.update(ok=False, violations=[{"factor": "*", "reason": "面板天数不足"}])
        if strict:
            raise RuntimeError("滚动验证无法进行：面板天数不足 horizon+1")
        return out
    for name in registry.all_names():
        last = TS.last_date(name)
        if last is None:
            out["violations"].append({"factor": name, "reason": "序列缺失",
                                      "last": None, "cutoff": cutoff})
            continue
        out["lasts"][name] = last
        if str(last) <= str(cutoff):
            continue
        item = {"factor": name, "reason": "末端越过 panelMax-horizon",
                "last": str(last), "cutoff": str(cutoff)}
        if name in skipped:
            out["suspects"].append(item)      # 已知可疑 → 记录但不判失败
        else:
            out["violations"].append(item)
    out["ok"] = not out["violations"]
    if not out["ok"] and strict:
        raise RuntimeError(
            "滚动验证数据层探针未通过：%d 个因子的末端越过 %s（%s）；"
            "ts.last > panelMax-horizon 只可能来自实现错误"
            % (len(out["violations"]), cutoff,
               ",".join(v["factor"] for v in out["violations"][:5])))
    return out


# ────────────────────────────────────────────────────────────
# 单期评估
# ────────────────────────────────────────────────────────────
def _num(value: Any) -> Optional[float]:
    """NaN/inf/None → None（JSON 里不能出现 NaN）。"""
    if value is None:
        return None
    try:
        got = float(value)
    except (TypeError, ValueError):
        return None
    return got if np.isfinite(got) else None


def judge_factor(name: str, cut: str, params: Dict[str, Any],
                 select_window: int) -> Dict[str, Any]:
    """在 `date <= cut` 的窗口内判定单个因子（复用 verdict_of / monotonicity_of）。

    Returns:
        {"factor", "n", "icMean", "icAdj", "direction", "tNw", "mono", "verdict",
         "note", "selected", "first", "last"}

    ⚠ `icMean` 是**原始** IC（未乘 direction，§10 第 12 条）；`icAdj = icMean × direction`
    才是「按当前执行方向算出来的有效预测力」。原始 IC 对 `direction=-1` 的因子为负，
    **正是先验成立**的表现 —— 把两者混在一个平均值里会得出「组合反向预测」的错觉。
    """
    import factor_eval as FE
    from export_factor_api import verdict_of

    # 先验方向（registry 是唯一来源，本模块不自己维护一份）
    dirn = int(registry.get(name)["direction"])
    sl = TS.slice_until(name, cut, window=select_window)
    empty = {"factor": name, "n": 0, "icMean": None, "icAdj": None, "direction": dirn,
             "tNw": None, "mono": None,
             "verdict": "数据不足", "note": "无序列或窗口内无可用日期",
             "selected": False, "first": None, "last": None}
    if sl is None or sl["n"] == 0:
        return empty

    row: Dict[str, Any] = {"factor": name, "n": int(sl["n"]), "direction": dirn,
                           "first": str(sl["dates"][0]), "last": str(sl["dates"][-1])}
    ic = np.asarray(sl["ic"], dtype="float64")
    ic = ic[np.isfinite(ic)]
    row["icMean"] = _num(ic.mean()) if ic.size else None
    row["icAdj"] = _num(ic.mean() * dirn) if ic.size else None
    if ic.size < MIN_USABLE_DAYS:
        row.update(tNw=None, mono=None, verdict="数据不足",
                   note="窗口内可用天数 %d < %d" % (ic.size, MIN_USABLE_DAYS),
                   selected=False)
        return row

    lag = int(params["nwLag"])
    t_nw = FE.newey_west_t(ic, lag) if ic.size >= 3 else float("nan")
    mono = FE.monotonicity_of(TS.groups_mean(sl["groups"]))
    stored_n_groups = int(np.asarray(sl["groups"]).shape[1])
    # 组数与口径不一致 → 单调性不可比，直接判数据不足（不静默用错口径）
    if stored_n_groups != int(params["nGroups"]):
        row.update(tNw=_num(t_nw), mono=None, verdict="数据不足",
                   note="分组数 %d ≠ 口径 %d" % (stored_n_groups, params["nGroups"]),
                   selected=False)
        return row

    verdict, note = verdict_of({"rank_ic": {"t_nw": _num(t_nw)},
                                "monotonicity": mono})
    row.update(tNw=_num(t_nw), mono=mono, verdict=verdict, note=note,
               selected=bool(verdict in SELECTED_VERDICTS))
    return row


def evaluate_period(dates: Sequence[str], t_idx: int, params: Dict[str, Any],
                    suspect_since: Optional[Dict[str, Optional[str]]] = None
                    ) -> Dict[str, Any]:
    """评估单个时点（`dates[t_idx]` 就是「今天」，能看到的只有 `<= ic_cutoff`）。

    Args:
        dates: 面板日期轴（升序）。
        t_idx: 时点下标。
        params: 口径（horizon / nwLag / nGroups / rollSelectWindow / …）。
        suspect_since: `{factor: since}`；`T >= since` 的期跳过该因子，
            `T < since` 的期照常参与（这样逐期 `n_factors` 的变化可解释）。

    Returns:
        期结果字典（含 `icCutoff`、`factors`、`selected`、`avgIcSelected`）。
    """
    horizon = int(params["horizon"])
    window = int(params["rollSelectWindow"])
    as_of = str(dates[t_idx])
    cut = ic_cutoff(dates, t_idx, horizon)
    since = suspect_since or {}

    rows: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    for name in registry.all_names():
        start = since.get(name)
        if start and str(as_of) >= str(start):
            skipped.append({"factor": name, "reason": "suspect", "since": start})
            continue
        rows.append(judge_factor(name, cut, params, window))

    selected = [r["factor"] for r in rows if r["selected"]]
    ic_values = [r["icMean"] for r in rows if r["icMean"] is not None]
    adj_values = [r["icAdj"] for r in rows if r.get("icAdj") is not None]
    sel_values = [r["icMean"] for r in rows
                  if r["selected"] and r["icMean"] is not None]
    sel_adj_values = [r["icAdj"] for r in rows
                      if r["selected"] and r.get("icAdj") is not None]
    # 「进 rows」≠「真的能评估」：预热期短的因子在早期期里 ic.size < MIN_USABLE_DAYS，
    # 会被判「数据不足」并留在 rows 里。把 len(rows) 当因子数报出去，等于把
    # 「没参与判定」的因子算成有效样本（例：T=39/Cut=2021-02-26 时 mom120 序列
    # 起点是 2021-08-02，窗口内一天数据都没有，却也被计入 24）。
    n_evaluable = sum(1 for r in rows
                      if r.get("verdict") != "数据不足" and r.get("tNw") is not None)
    return {
        "tIndex": int(t_idx),
        "asOf": as_of,
        "icCutoff": cut,
        "rollSelectWindow": window,
        # `n_factors` 保留字段名但语义收紧为「可评估」；`n_considered` 是进 rows 的
        "n_factors": int(n_evaluable),
        "n_considered": len(rows),
        "n_evaluable": int(n_evaluable),
        "n_selected": len(selected),
        "selected": selected,
        "avgIc": _num(np.mean(ic_values)) if ic_values else None,
        "avgIcAdj": _num(np.mean(adj_values)) if adj_values else None,
        "avgIcSelected": _num(np.mean(sel_values)) if sel_values else None,
        # ⚠ 页面该看的是这个（= 按方向归正后的有效 IC）。
        # `avgIcSelected` 是原始 IC 算术平均，把 dir=+1 和 dir=-1 混在一起，
        # 对反向因子为负正是先验成立 —— 单独读它会得出「组合反向预测」的错觉。
        "avgIcSelectedAdj": _num(np.mean(sel_adj_values)) if sel_adj_values else None,
        "skipped": skipped,
        "factors": rows,
    }


def period_indices(n_dates: int, horizon: int, step: int,
                   min_usable: int = MIN_USABLE_DAYS) -> List[int]:
    """期下标列表（升序），保证**最后一期就是最新的 T**。

    从 `n_dates - 1` 往回按 `step` 退，退到「cut 之前至少还有 min_usable 天」
    为止。这样不会因为 `(n-1 - 起点) % step != 0` 而丢掉最新一期 ——
    页面标题写的是「最近 N 期」，最新一期缺失会直接误导用户。
    """
    last = int(n_dates) - 1
    first = int(horizon) + int(min_usable)
    if last < first:
        return []
    out: List[int] = []
    idx = last
    while idx >= first:
        out.append(idx)
        idx -= max(1, int(step))
    return sorted(out)


# ────────────────────────────────────────────────────────────
# 全流程
# ────────────────────────────────────────────────────────────
def run(conn: Optional[sqlite3.Connection] = None,
        params: Optional[Dict[str, Any]] = None,
        panel_dates_axis: Optional[Sequence[str]] = None,
        out: Optional[str] = OUT_DEFAULT,
        verbose: bool = True) -> Dict[str, Any]:
    """跑完整滚动验证并（默认）写 `data/factor_rolling.json`。

    Args:
        conn: 数据库连接；None 时只用传入的 `panel_dates_axis`（测试用）。
        params: 口径；None 用 DEFAULTS。
        panel_dates_axis: 面板日期轴；None 时从库/缓存轴取。
        out: 输出路径；None 表示不落盘（测试用）。

    Returns:
        {"ok", "code": 0|4, "reason", "n_periods", "periods", "latest",
         "skippedFactors", "summary", "lag", "elapsed"}
        `code == 4` 表示「滚动验证没有有效期」（共享知识第 6 条退出码 4）。
    """
    t0 = time.time()
    prm = dict(DEFAULTS)
    prm.update({k: v for k, v in (params or {}).items() if k in DEFAULTS})
    for key in ("horizon", "minStocks", "nwLag", "nGroups", "rollStep",
                "rollSelectWindow"):
        prm[key] = int(prm[key])

    dates = [str(d) for d in (panel_dates_axis
                             or (TS.panel_dates_fast(conn) if conn is not None
                                 else []))]
    if not dates:
        return {"ok": False, "code": 4, "reason": "面板没有日期轴，无法滚动验证",
                "n_periods": 0, "periods": [], "latest": None,
                "skippedFactors": [], "summary": {}, "lag": None,
                "elapsed": round(time.time() - t0, 2)}

    # 先判「有没有期」再跑探针：面板天数不足时根本没得验证，此时报
    # 「ts.last 越界」会把排障方向带偏（真正的原因是数据太短）。
    idx_list = period_indices(len(dates), prm["horizon"], prm["rollStep"])
    if not idx_list:
        return {"ok": False, "code": 4,
                "reason": "面板只有 %d 天，不足 horizon+%d，没有可用期"
                          % (len(dates), MIN_USABLE_DAYS),
                "n_periods": 0, "periods": [], "latest": None,
                "skippedFactors": TS.suspects(), "summary": {}, "lag": None,
                "elapsed": round(time.time() - t0, 2)}

    suspect_list = TS.suspects()
    suspect_since = {s["factor"]: s.get("since") for s in suspect_list}
    # suspect 因子本来就被跳过，它们的越界末端不该把整轮验证判失败（见 verify_lag）
    lag = verify_lag(dates, prm["horizon"], strict=True, exclude=suspect_since)

    periods: List[Dict[str, Any]] = []
    for t_idx in idx_list:
        periods.append(evaluate_period(dates, t_idx, prm, suspect_since))

    usable = [p for p in periods if p["n_selected"] > 0 or p["avgIc"] is not None]
    latest = periods[-1]
    selected_means = [p["avgIcSelected"] for p in periods
                      if p["avgIcSelected"] is not None]
    recent = [p["avgIcSelected"] for p in periods[-5:]
              if p["avgIcSelected"] is not None]
    # 按方向归正后的有效 IC 序列：这才是「当前这套执行方向」的预测力。
    # 页面上的平均 IC / t 值 / IC>0 占比都应该基于这一条，而不是原始 IC。
    selected_adj = [p["avgIcSelectedAdj"] for p in periods
                    if p.get("avgIcSelectedAdj") is not None]
    recent_adj = [p["avgIcSelectedAdj"] for p in periods[-5:]
                  if p.get("avgIcSelectedAdj") is not None]
    # 实例加权（每个「期 × 入选因子」算一个实例）：**只存不显示**。
    # 与期级均值回答的不是同一个问题：
    #   期级均值（avgSelectedIcAdj）= 平均每期这套组合多强；
    #   实例加权                   = 随便抽一个入选因子，它当期多强。
    # 某期入选 12 个、另一期入选 2 个时，后者会被前者的 12 个实例淹掉 ——
    # 用户问的是「这个月我的组合有多强」，所以页面用期级；实例值留着供核对。
    inst_adj = [f["icAdj"] for p in periods for f in (p.get("factors") or [])
                if f.get("selected") and f.get("icAdj") is not None]
    summary = {
        "nPeriods": len(periods),
        "nUsablePeriods": len(usable),
        # 原始 IC（未乘 direction）：只留作诊断，**不可单独解读** —— dir=-1 的因子
        # 原始 IC 为负正是先验成立。
        "avgSelectedIc": _num(np.mean(selected_means)) if selected_means else None,
        "avgSelectedIcRecent5": _num(np.mean(recent)) if recent else None,
        # 有效 IC（= icMean × direction）：页面标题用这一条
        "avgSelectedIcAdj": _num(np.mean(selected_adj)) if selected_adj else None,
        "avgSelectedIcAdjRecent5": _num(np.mean(recent_adj)) if recent_adj else None,
        # 实例加权（只存不显示，见上）
        "instanceWeightedAdjIc": _num(np.mean(inst_adj)) if inst_adj else None,
        "instanceCount": len(inst_adj),
        "avgSelectedNum": _num(np.mean([p["n_selected"] for p in periods])),
        "nSuspectFactors": len(suspect_list),
        "horizon": prm["horizon"],
        "rollStep": prm["rollStep"],
        "rollSelectWindow": prm["rollSelectWindow"],
    }
    payload = {
        "ok": True,
        "code": 0,
        "generatedAt": FR.iso_now(),
        "params": prm,
        "panelMaxDate": dates[-1],
        "panelDates": len(dates),
        "n_periods": len(periods),
        "periods": periods,
        "latest": {k: latest[k] for k in
                   ("tIndex", "asOf", "icCutoff", "n_factors", "n_considered",
                    "n_evaluable", "n_selected", "selected", "avgIc",
                    "avgIcSelected", "avgIcSelectedAdj")},
        "skippedFactors": suspect_list,
        "summary": summary,
        "lag": lag,
    }
    if not usable:
        payload.update(ok=False, code=4,
                       reason="滚动验证没有有效期：%d 期均无可用 IC（检查 factor_ts "
                              "是否已初始化、minStocks=%d 是否过严）"
                              % (len(periods), prm["minStocks"]))
    if out:
        _write_json_atomic(out, payload)
    if verbose:
        print("  滚动验证 %d 期（%.1fs）：最新期 %s / cut %s / 入选 %d 个%s"
              % (len(periods), time.time() - t0, latest["asOf"], latest["icCutoff"],
                 latest["n_selected"],
                 "；跳过 %d 个 suspect 因子" % len(suspect_list) if suspect_list else ""),
              flush=True)
    payload["elapsed"] = round(time.time() - t0, 2)
    return payload


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    """`.tmp → os.replace`（共享知识第 7 条）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def load(path: Optional[str] = None) -> Dict[str, Any]:
    """读上一次的 `factor_rolling.json`（缺失/损坏返回空 dict）。"""
    target = path or OUT_DEFAULT
    try:
        with open(target, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def compact(path: Optional[str] = None) -> Dict[str, Any]:
    """给 `factor_api.json` / `/api/picks` 用的精简副本。

    保留：`params` / `latest` / `summary` / `skippedFactors` / `generatedAt`
    与逐期表（每期每因子只留判定所需的 8 个字段）。
    刻意**不**保留每期的 `skipped` 明细（期 × 因子 会重复 24 遍，体积大且
    信息重复：全局 `skippedFactors` 已带 `since`，逐期是否跳过可由
    `asOf >= since` 推出，与说明书 §5.7 的语义一致）。

    ⚠ 逐期 IC 有两条：`avgIcSelected`（原始，未乘 direction）与
    `avgIcSelectedAdj`（有效，= icMean × direction）。**页面只能用 Adj**：
    原始 IC 把 `dir=+1` 和 `dir=-1` 的因子混在一个平均里，对反向因子为负
    恰恰说明先验成立，单独读它会得出「组合反向预测」的错觉。
    """
    data = load(path)
    if not data:
        return {}
    periods = []
    for p in (data.get("periods") or []):
        periods.append({
            "asOf": p.get("asOf"),
            "icCutoff": p.get("icCutoff"),
            # n_factors = 可评估；n_considered = 进 rows 的（含「数据不足」）
            "n_factors": p.get("n_factors"),
            "n_considered": p.get("n_considered"),
            "n_evaluable": p.get("n_evaluable"),
            "n_selected": p.get("n_selected"),
            "selected": p.get("selected") or [],
            "avgIc": p.get("avgIc"),
            "avgIcSelected": p.get("avgIcSelected"),
            "avgIcSelectedAdj": p.get("avgIcSelectedAdj"),
            "factors": [{k: (f or {}).get(k) for k in
                         ("factor", "n", "direction", "icMean", "icAdj", "tNw",
                          "mono", "verdict", "selected")}
                        for f in (p.get("factors") or [])],
        })
    return {"generatedAt": data.get("generatedAt"),
            "params": data.get("params"),
            "latest": data.get("latest"),
            "summary": data.get("summary"),
            "skippedFactors": data.get("skippedFactors") or [],
            "periods": periods}


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="滚动验证（逐期重选因子）")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default=OUT_DEFAULT)
    ap.add_argument("--step", type=int, default=DEFAULTS["rollStep"])
    ap.add_argument("--window", type=int, default=DEFAULTS["rollSelectWindow"])
    ap.add_argument("--horizon", type=int, default=DEFAULTS["horizon"])
    ap.add_argument("--min-stocks", type=int, default=DEFAULTS["minStocks"])
    ap.add_argument("--groups", type=int, default=DEFAULTS["nGroups"])
    ap.add_argument("--nw-lag", type=int, default=DEFAULTS["nwLag"])
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    prm = {"horizon": args.horizon, "minStocks": args.min_stocks,
           "nGroups": args.groups, "nwLag": args.nw_lag,
           "rollStep": args.step, "rollSelectWindow": args.window}
    conn = sqlite3.connect(args.db, timeout=120)
    try:
        res = run(conn, prm, out=args.out, verbose=not args.quiet)
    finally:
        conn.close()
    if not res.get("ok"):
        print("！！ %s" % res.get("reason"), file=sys.stderr)
    return int(res.get("code", 0))


if __name__ == "__main__":
    raise SystemExit(main())
