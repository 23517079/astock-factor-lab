# -*- coding: utf-8 -*-
"""导出前端消费的因子数据（单一事实来源）

为什么不让 Node 端自己算
------------------------
"值得复核 / 方向需翻转"这类判定规则只应该存在一处。若 Node 再实现一遍，
改阈值时一定漏改其中一边，页面和报告就会给出不同结论。所以这里在 Python 侧
算完，导出一个扁平 JSON，Node 只负责原样返回。

输出：data/factor_api.json

用法：
    python export_factor_api.py
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from typing import Any, Dict, List, Optional

from factors.registry import FACTOR_DEFS, CATEGORY_LABELS, get as get_factor
import factor_plain
# 时点常量只从 freshness 取（那里是「新鲜度判定」的唯一实现，16:10 也在那里；
# T04 会把它放进 scheduler.DEFAULT_CFG["factor_time"]，届时这里改 import 一行）
from freshness import DEFAULT_FACTOR_TIME

DB = "data/sequoia_v2.db"
EVAL = "data/factor_eval.json"
SEG = "data/segment_ic.json"
OUT = "data/factor_api.json"


def num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def verdict_of(f: Dict[str, Any]) -> tuple[str, str]:
    """与 make_factor_report.py 保持完全一致的分档规则（改这里要同步改那边）。"""
    t_nw = num((f.get("rank_ic") or {}).get("t_nw"))
    mono = num(f.get("monotonicity"))
    if t_nw is None:
        return "数据不足", "有效交易日太少，算不出稳定统计量"
    if abs(t_nw) < 2:
        return "未达门槛", "|t| < 2，当前样本下与 0 无法区分"
    if mono is not None and mono >= 0.5:
        return "值得复核", "统计稳定，且分组收益随先验方向单调（需扩样本确认）"
    if mono is not None and mono <= -0.5:
        return "方向需翻转", "统计稳定，但分组收益与先验方向相反——按先验方向使用会反向暴露"
    return "仅统计显著", "IC 稳定但分组收益不单调，可能是非线性的，也可能只是噪声"


def panel_stats(db: Optional[str] = None,
                conn: Optional[sqlite3.Connection] = None) -> Dict[str, Any]:
    """面板/行情库的规模统计（页面「样本量」与 caveats 用）。

    `conn` 优先：pipeline 里已经有一条带 WAL 的连接，再开一条会让
    `COUNT(*)`/`COUNT(DISTINCT …)` 这些重查询多付一次冷页缓存。
    """
    own = False
    if conn is None:
        conn = sqlite3.connect(db or DB)
        own = True
    out: Dict[str, Any] = {}
    try:
        out["panelRows"] = conn.execute("SELECT COUNT(*) FROM factor_panel").fetchone()[0]
        out["panelSymbols"] = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM factor_panel").fetchone()[0]
        out["panelFactors"] = conn.execute(
            "SELECT COUNT(DISTINCT factor) FROM factor_panel").fetchone()[0]
        out["universe"] = conn.execute(
            "SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0]
        # 样本交易日必须按面板自身的日期范围算：
        # 行情库里有几只票回溯到了 2020 年，用 stock_daily 的 DISTINCT date 会得出
        # 1566 天（6.4 年），而面板实际只有 353 天，会让人严重高估样本质量。
        out["tradeDays"] = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM factor_panel").fetchone()[0]
        out["dateMin"], out["dateMax"] = conn.execute(
            "SELECT MIN(date), MAX(date) FROM factor_panel").fetchone()
        out["stockDays"] = conn.execute(
            "SELECT COUNT(DISTINCT date) FROM stock_daily").fetchone()[0]
        out["stockMin"], out["stockMax"] = conn.execute(
            "SELECT MIN(date), MAX(date) FROM stock_daily").fetchone()
        out["dupRows"] = conn.execute(
            "SELECT COUNT(*) FROM (SELECT date,symbol,factor FROM factor_panel "
            "GROUP BY 1,2,3 HAVING COUNT(*)>1)").fetchone()[0]
    finally:
        if own:
            conn.close()
    if db and os.path.exists(db):
        out["dbMB"] = round(os.path.getsize(db) / 1024 / 1024)
    else:
        out["dbMB"] = None
    return out


def load(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def eval_as_of(path: str) -> Optional[str]:
    """`factor_eval.json` 的**写出时刻**（文件 mtime），格式 `YYYY-MM-DD HH:MM`。

    为什么必须有这个字段（决策 6）：每日 pipeline **明确不重跑** `factor_eval.py`
    （小时级）。所以页面上会出现「数据截止 2026-11-16」与「IC 基于 11-13 评估」
    并存的局面 —— 不把 `evalAsOf` 显式展示出来，用户会以为 IC 也是今天的。
    取 mtime 而不是 JSON 里的 `generated_at`：文件被手工替换/回滚时，
    mtime 才是「这份数据到底是什么时候落到盘上的」这一事实。
    """
    try:
        stamp = os.path.getmtime(path)
    except OSError:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(stamp))


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


def contract_fields(db: Optional[str] = None,
                    conn: Optional[sqlite3.Connection] = None,
                    eval_path: str = EVAL,
                    rolling_path: Optional[str] = None,
                    factor_time: str = "16:10") -> Dict[str, Any]:
    """数据契约的「上下文」段：新鲜度 + 时间源 + 滚动验证。

    这些字段与因子数值无关，但页面离不开：徽标（4 态）、三段式时间文案、
    「下次更新」、滚动验证卡片。全部由 Python 侧算好，页面只做展示 ——
    前端一旦自己算日期，就会出现改造前那种「页面时间比内容还新」的自相矛盾。

    刻意**不让任何一环失败拖垮整份导出**：拿不到就落 None，页面显示「未知」，
    这比「因为 stock_daily 锁了，所以整页 500」要好。
    """
    out: Dict[str, Any] = {"evalAsOf": eval_as_of(eval_path),
                           "dataThrough": None, "lastCalcAt": None,
                           "lastCalcOk": None, "lastCalcError": None,
                           "nextRunAt": None, "partial": False, "warnings": [],
                           "freshness": {}, "rolling": {}}
    own_conn = False
    if conn is None and db:
        try:
            conn = sqlite3.connect(db, timeout=120)
            own_conn = True
        except sqlite3.Error:
            conn = None
    try:
        if conn is not None:
            import freshness                     # 延迟导入：只在导出时用一次
            fresh = freshness.compute(conn, factor_time=factor_time)
            out["freshness"] = fresh
            for key in ("dataThrough", "lastCalcAt", "lastCalcOk",
                        "lastCalcError", "nextRunAt", "partial", "warnings"):
                out[key] = fresh.get(key)
    except Exception:                            # noqa: BLE001 — 上下文缺失不阻断导出
        pass
    finally:
        if own_conn and conn is not None:
            conn.close()

    try:
        import rolling_eval                      # 延迟导入：避免 cycle
        out["rolling"] = rolling_eval.compact(rolling_path)
    except Exception:                            # noqa: BLE001
        out["rolling"] = {}
    return out


def export(db: Optional[str] = DB, eval_path: str = EVAL, seg_path: str = SEG,
           out: str = OUT, factor_time: str = DEFAULT_FACTOR_TIME,
           rolling_path: Optional[str] = None,
           conn: Optional[sqlite3.Connection] = None,
           partial: bool = False,
           warnings: Optional[list] = None,
           verbose: bool = True) -> Dict[str, Any]:
    """生成 `data/factor_api.json`（原子替换）。

    Args:
        db: 数据库路径（`conn` 为空时用它开连接并统计面板规模）。
        conn: 已有连接；pipeline 传进来以复用（省一次冷页缓存）。
        rolling_path: `factor_rolling.json` 路径；None 用默认位置。

    Returns:
        {"ok": bool, "reason": str|None, "out": path, "factors": n, "pairs": n,
         "evalAsOf", "dataThrough", "rollingPeriods"}
    """
    evald = load(eval_path)
    if not evald.get("factors"):
        return {"ok": False, "reason": "缺少评估结果，先运行 factor_eval.py",
                "out": out, "factors": 0, "pairs": 0}
    seg = load(seg_path)
    ps = panel_stats(db=db, conn=conn)
    meta = evald.get("meta") or {}
    fdr = evald.get("fdr") or {}

    factors: List[Dict[str, Any]] = []
    for name, f in (evald.get("factors") or {}).items():
        m = get_factor(name)
        tag, note = verdict_of(f)
        rank_ic = f.get("rank_ic") or {}
        groups = f.get("groups") or []
        factors.append({
            "name": name,
            "label": m["label"],
            "category": CATEGORY_LABELS.get(m["category"], m["category"]),
            "categoryKey": m["category"],
            "direction": m["direction"],
            "minBars": m["min_bars"],
            "formula": m.get("formula_desc", ""),
            "verdict": tag,
            "verdictNote": note,
            "ic": num(rank_ic.get("mean")),
            "icStd": num(rank_ic.get("std")),
            "icir": num(rank_ic.get("icir")),
            "tNaive": num(rank_ic.get("t_value")),
            "tNw": num(rank_ic.get("t_nw")),
            "pNw": num(rank_ic.get("p_value_nw")),
            "qNw": num((fdr.get(name) or {}).get("q")),
            "nDays": rank_ic.get("n_days"),
            "nObs": f.get("n_obs"),
            "positiveRatio": num(rank_ic.get("positive_ratio")),
            "mono": num(f.get("monotonicity")),
            "lsMean": num((f.get("long_short") or {}).get("mean")),
            "lsAnnual": num((f.get("long_short") or {}).get("annualized")),
            "lsWin": num((f.get("long_short") or {}).get("win_ratio")),
            "turnover": num(f.get("turnover")),
            "completeness": num(f.get("panel_completeness")),
            "coverage": num(f.get("market_coverage")),
            "groups": [{"g": g.get("group"), "mean": num(g.get("mean")),
                        "annual": num(g.get("annualized"))} for g in groups],
            "icDecay": {k: num(v) for k, v in (f.get("ic_decay") or {}).items()},
            "decayCaliber": f.get("ic_decay_caliber") or {},
            "directionNote": f.get("direction_note"),
            "directionMismatch": bool(f.get("direction_sign_mismatch")),
        })

    # 补「大白话」层：what/high/note（这是什么、数值高说明什么）
    # + plain/detail/tone（按实测结果生成的人话结论）。
    # 放在这里而不是在 factor dict 里直接写，是为了让解释逻辑只存在 factor_plain.py 一处。
    horizon = int(meta.get("horizon") or 5)
    notes = factor_plain.factor_notes({x["name"]: x for x in factors}, horizon)
    for x in factors:
        n = notes.get(x["name"]) or {}
        x["plainWhat"] = n.get("what")
        x["plainHigh"] = n.get("high")
        x["plainNote"] = n.get("note")
        x["reading"] = n.get("plain")
        x["readingDetail"] = n.get("detail")
        x["tone"] = n.get("tone")

    # 冗余：只保留 |rho| >= 0.7 的对
    corr = evald.get("corr_matrix") or {}
    pairs = []
    names = list(corr.keys())
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            v = num((corr.get(a) or {}).get(b))
            if v is not None and abs(v) >= 0.7:
                pairs.append({"a": a, "b": b, "rho": v})
    pairs.sort(key=lambda x: -abs(x["rho"]))

    ctx = contract_fields(db=db, conn=conn, eval_path=eval_path,
                          rolling_path=rolling_path, factor_time=factor_time)
    ctx = _apply_partial(ctx, partial, warnings)

    payload = {
        "ok": True,
        "generatedAt": time.strftime("%Y-%m-%d %H:%M"),
        "meta": {
            "horizon": meta.get("horizon"),
            "entry": meta.get("entry"),
            "entryNote": meta.get("entry_note"),
            "entryNoteH1": meta.get("entry_note_h1"),
            "statNote": meta.get("stat_note"),
            "minStocks": meta.get("min_stocks"),
            "panel": ps,
            # 单因子 IC 的评估时点（决策 6：每日 pipeline 不重跑 factor_eval.py）
            "evalAsOf": ctx["evalAsOf"],
        },
        "factors": factors,
        "segments": {
            "pools": [p for p in (seg.get("meta") or {}).get("segments", {})] if seg else [],
            "size": (seg.get("meta") or {}).get("segments", {}) if seg else {},
            "rows": [{"factor": r["factor"], "pool": r["pool"], "ic": num(r["ic_mean"]),
                      "tNw": num(r["t_nw"]), "nDays": r["n_days"]}
                     for r in (seg.get("rows") or [])],
            "summary": seg.get("summary") or [],
            "horizon": (seg.get("meta") or {}).get("horizon"),
        },
        "corrPairs": pairs,
        "metricHelp": factor_plain.METRIC_HELP,
        # ── 数据契约：新鲜度与滚动验证（页面只读，不再自己算日期）──
        "evalAsOf": ctx["evalAsOf"],
        "dataThrough": ctx["dataThrough"],
        "lastCalcAt": ctx["lastCalcAt"],
        "lastCalcOk": ctx["lastCalcOk"],
        "lastCalcError": ctx["lastCalcError"],
        "nextRunAt": ctx["nextRunAt"],
        # 「主体成功、个别环节缺」（目前只有「滚动本次无有效期」）。
        # 页面靠它把滚动卡片写一行灰字说明，而**不是**把整页刷成失败红字。
        "partial": bool(partial),
        "warnings": [str(w) for w in (warnings or [])],
        "freshness": ctx["freshness"],
        "rolling": ctx["rolling"],
        "caveats": [
            # 样本长短决定这句该怎么写：短样本只能自认"单一市场状态"，
            # 长样本再说这句话就是低估了自己的数据。
            ("样本 %d 个交易日（约 %.1f 年），跨越了 2021 结构牛、2022–2023 熊市与 2024 反弹，"
             "「只在一种行情里有效」的风险已明显下降"
             % (ps["tradeDays"], ps["tradeDays"] / 244))
            if ps["tradeDays"] >= 1200 else
            ("样本 %d 个交易日（约 %.1f 年），单一市场状态，未跨牛熊"
             % (ps["tradeDays"], ps["tradeDays"] / 244)),
            "面板覆盖 %d / %d 只；北交所（8/4 号段）不在数据源内" % (ps["panelSymbols"], ps["universe"]),
            "库里只有当前在册标的，退市股不在，收益天然偏高（幸存者偏差未消除）",
            "turn20 / amihud20 / turn_cv20 的成交额是 成交量×(高+低+收)/3 的估算值，非交易所真值",
            "dist_high250 / dist_low250 在数据不足 250 日时退化为「上市以来」口径，存在口径混用",
            "行业与市值中性化尚未做",
        ],
    }

    # 原子替换：页面永远读不到半份 JSON（共享知识第 7 条）
    parent = os.path.dirname(os.path.abspath(out))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = out + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, out)

    if verbose:
        print(f"已导出 {out}：{len(factors)} 个因子 / {len(pairs)} 对高相关 / "
              f"面板 {ps['panelRows']:,} 行（evalAsOf={ctx['evalAsOf']}）")
    return {"ok": True, "reason": None, "out": out,
            "factors": len(factors), "pairs": len(pairs),
            "evalAsOf": ctx["evalAsOf"],
            "dataThrough": ctx["dataThrough"],
            "rollingPeriods": len((ctx["rolling"] or {}).get("periods") or [])}


def main() -> int:
    ap = argparse.ArgumentParser(description="导出前端因子数据")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--eval", default=EVAL, dest="eval_path")
    ap.add_argument("--seg", default=SEG, dest="seg_path")
    ap.add_argument("--out", default=OUT)
    ap.add_argument("--factor-time", default=DEFAULT_FACTOR_TIME,
                    help="因子任务时点，用于计算 nextRunAt")
    ap.add_argument("--rolling", default=None, help="factor_rolling.json 路径")
    args = ap.parse_args()

    res = export(db=args.db, eval_path=args.eval_path, seg_path=args.seg_path,
                 out=args.out, factor_time=args.factor_time,
                 rolling_path=args.rolling, verbose=True)
    if not res.get("ok"):
        print(res.get("reason") or "导出失败")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
