# -*- coding: utf-8 -*-
"""因子体检报告生成器（面向非专业读者）

输入
----
- data/factor_eval.json   全因子评估结果（factor_eval.py 产出）
- data/segment_ic.json    分板块一致性（segment_ic.py 产出，可选）
- factor_panel / stock_daily 表（用于数据体检页）

输出
----
reports/factor-report-YYYYMMDD-HHMM.html —— 单文件、无外部依赖、双击即开。

设计原则
--------
1. **先讲能回答什么、不能回答什么**，把"样本不足"放在最前面而不是藏在附录。
2. 每个数字都标口径（entry / horizon / 样本池），不让读者以为看到的是"全市场真相"。
3. 不写"因子有效/无效"这种一刀切结论，只写证据强度和需要什么才能升级结论。
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import time
from typing import Any, Dict, List

from factors.registry import FACTOR_DEFS, CATEGORY_LABELS, get as get_factor

DB = "data/sequoia_v2.db"
EVAL = "data/factor_eval.json"
SEG = "data/segment_ic.json"

SEGMENTS = {
    "深主板": ["000", "001"],
    "深中小": ["002", "003"],
    "创业板": ["300", "301"],
    "沪主板": ["600", "601", "603", "605"],
    "科创板": ["688"],
}


# ────────────────────────────────────────────────────────────
# 数据装载
# ────────────────────────────────────────────────────────────
def load_json(path: str) -> Dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def db_stats(db: str) -> Dict[str, Any]:
    conn = sqlite3.connect(db)
    out: Dict[str, Any] = {}
    out["daily_rows"] = conn.execute("SELECT COUNT(*) FROM stock_daily").fetchone()[0]
    out["daily_symbols"] = conn.execute("SELECT COUNT(DISTINCT symbol) FROM stock_daily").fetchone()[0]
    lo, hi = conn.execute("SELECT MIN(date), MAX(date) FROM stock_daily").fetchone()
    out["daily_start"], out["daily_end"] = lo, hi
    out["daily_days"] = conn.execute("SELECT COUNT(DISTINCT date) FROM stock_daily").fetchone()[0]
    try:
        out["panel_rows"] = conn.execute("SELECT COUNT(*) FROM factor_panel").fetchone()[0]
        out["panel_symbols"] = conn.execute("SELECT COUNT(DISTINCT symbol) FROM factor_panel").fetchone()[0]
        out["panel_factors"] = conn.execute("SELECT COUNT(DISTINCT factor) FROM factor_panel").fetchone()[0]
        out["dup_rows"] = conn.execute(
            "SELECT COUNT(*) FROM (SELECT date,symbol,factor FROM factor_panel "
            "GROUP BY 1,2,3 HAVING COUNT(*)>1)"
        ).fetchone()[0]
        # 样本交易日按面板自身范围算。行情库里有几只票回溯到 2020 年，
        # 若用 stock_daily 的 DISTINCT date 会得出 1566 天（6.4 年），
        # 而面板实际只有 353 天，会严重高估样本质量。
        out["trade_days"] = conn.execute("SELECT COUNT(DISTINCT date) FROM factor_panel").fetchone()[0]
        out["panel_start"], out["panel_end"] = conn.execute(
            "SELECT MIN(date), MAX(date) FROM factor_panel").fetchone()
    except sqlite3.Error:
        out["panel_rows"] = 0
        out["panel_symbols"] = 0
        out["panel_factors"] = 0
        out["dup_rows"] = 0
        out["trade_days"] = out["daily_days"]
        out["panel_start"], out["panel_end"] = lo, hi
    allx = [r[0] for r in conn.execute("SELECT DISTINCT symbol FROM stock_daily")]
    out["segments"] = {
        name: sum(1 for s in allx if any(s.startswith(p) for p in prefixes))
        for name, prefixes in SEGMENTS.items()
    }
    out["other"] = len(allx) - sum(out["segments"].values())
    out["db_mb"] = round(os.path.getsize(db) / 1024 / 1024, 0) if os.path.exists(db) else 0
    conn.close()
    return out


# ────────────────────────────────────────────────────────────
# 格式化小工具
# ────────────────────────────────────────────────────────────
def fmt(v, digits: int = 4, sign: bool = False, pct: bool = False) -> str:
    if v is None or isinstance(v, bool):
        return "-"
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "-"
    if x != x or x in (float("inf"), float("-inf")):
        return "-"
    if pct:
        return f"{x * 100:+.2f}%" if sign else f"{x * 100:.2f}%"
    return f"{x:+.{digits}f}" if sign else f"{x:.{digits}f}"


def num(v) -> float | None:
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
    except (TypeError, ValueError):
        return None
    return x if x == x else None


def cls_pct(v) -> str:
    """收益类着色：正红负绿（A 股习惯）。"""
    x = num(v)
    if x is None or x == 0:
        return "flat"
    return "up" if x > 0 else "down"


def cls_t(v) -> str:
    """|t|>=2 视为统计上值得注意，与方向无关。"""
    x = num(v)
    if x is None:
        return "flat"
    return "strong" if abs(x) >= 2 else "weak"


def verdict_of(f: Dict[str, Any]) -> tuple[str, str]:
    """给每个因子一个粗分档，附一句人话解释。

    单调性符号的含义（与 group_returns 一致）：
      第 1 组按 registry.direction 定义为「预期最好」的一组。
      monotonicity > 0 表示收益随组序递减，即「与先验方向一致」；
      monotonicity < 0 表示收益随组序递增，即「实际方向与先验相反」，
      此时这个因子仍然有信息，只是要把方向反过来用（或修正 registry）。
    """
    rank_ic = f.get("rank_ic") or {}
    t_nw = num(rank_ic.get("t_nw"))
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


# ────────────────────────────────────────────────────────────
# HTML 片段
# ────────────────────────────────────────────────────────────
CSS = """
*{box-sizing:border-box;margin:0;padding:0}
body{background:#0d0f14;color:#e8eaf0;font:14px/1.7 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif;
     padding:32px 20px 80px;max-width:1180px;margin:0 auto}
h1{font-size:26px;font-weight:650;letter-spacing:-.3px}
h2{font-size:18px;font-weight:600;margin:44px 0 14px;padding-left:11px;border-left:3px solid #4f8cff}
h3{font-size:15px;font-weight:600;margin:24px 0 10px;color:#cfd5e4}
p{color:#a8b0c2;margin:9px 0}
.sub{color:#76809a;font-size:13px;margin-top:6px}
code{background:#1b202b;padding:1px 6px;border-radius:4px;font-size:12.5px;color:#9fc4ff}

.hero{display:flex;gap:14px;flex-wrap:wrap;margin:22px 0 8px}
.kpi{flex:1;min-width:150px;background:#151922;border:1px solid #232a38;border-radius:10px;padding:15px 17px}
.kpi .l{color:#78829c;font-size:12px;letter-spacing:.4px}
.kpi .v{font-size:23px;font-weight:640;margin-top:5px;font-variant-numeric:tabular-nums}
.kpi .n{color:#69738c;font-size:11.5px;margin-top:3px}

.alert{background:#1c1710;border:1px solid #4a3a1c;border-left:3px solid #d99b2b;
       border-radius:8px;padding:16px 18px;margin:18px 0}
.alert b{color:#f0c264}
.alert ul{margin:8px 0 0 20px;color:#b5a68c}
.alert li{margin:5px 0}
.ok{background:#101c17;border:1px solid #1e4436;border-left:3px solid #30a46c}
.ok b{color:#6fd39c}
.ok ul{color:#8fb9a5}

table{width:100%;border-collapse:collapse;margin:12px 0;font-size:13px}
th{text-align:right;padding:9px 10px;color:#7b86a0;font-weight:500;font-size:11.5px;
   letter-spacing:.5px;border-bottom:1px solid #262e3e;white-space:nowrap}
th:first-child,td:first-child{text-align:left}
th:nth-child(2),td:nth-child(2){text-align:left}
td{padding:9px 10px;text-align:right;border-bottom:1px solid #1a1f2a;font-variant-numeric:tabular-nums}
tbody tr:hover{background:#141822}
.up{color:#f2555a}.down{color:#3fbb7d}.flat{color:#6d768e}
.strong{color:#ffd479;font-weight:600}.weak{color:#727c94}

.tag{display:inline-block;padding:1.5px 8px;border-radius:20px;font-size:11.5px;white-space:nowrap}
.t-good{background:#1b2b3f;color:#7db6ff}
.t-warn{background:#2f2216;color:#e8a44c}
.t-mid{background:#2a2418;color:#d9b06a}
.t-bad{background:#22262f;color:#8b93a7}

.bar{position:relative;height:6px;background:#1c2230;border-radius:3px;min-width:70px;overflow:hidden}
.bar i{position:absolute;top:0;bottom:0;border-radius:3px;display:block}
.bar i.pos{background:#f2555a;left:50%}
.bar i.neg{background:#3fbb7d;right:50%}

.grid2{display:grid;grid-template-columns:1fr 1fr;gap:16px}
@media(max-width:820px){.grid2{grid-template-columns:1fr}}
.card{background:#151922;border:1px solid #232a38;border-radius:10px;padding:16px 18px}
.card h4{font-size:14px;font-weight:600;margin-bottom:8px;color:#dfe4f0}
.card p{font-size:13px;margin:6px 0}

.foot{margin-top:52px;padding-top:18px;border-top:1px solid #1e2532;color:#5f6880;font-size:12px}
ul.plain{margin:8px 0 8px 20px;color:#a8b0c2}
ul.plain li{margin:6px 0}
"""


def kpi(label: str, value: str, note: str = "") -> str:
    return (
        f'<div class="kpi"><div class="l">{label}</div>'
        f'<div class="v">{value}</div><div class="n">{note}</div></div>'
    )


def ic_bar(v) -> str:
    x = num(v)
    if x is None:
        return '<div class="bar"></div>'
    w = min(abs(x) / 0.15, 1.0) * 50  # 0.15 的 IC 已经算很强
    side = "pos" if x > 0 else "neg"
    return f'<div class="bar"><i class="{side}" style="width:{w:.1f}%"></i></div>'


def factor_table(factors: Dict[str, Any], fdr: Dict[str, Any]) -> str:
    rows: List[str] = []
    order = sorted(
        factors.items(),
        key=lambda kv: -abs(num(((kv[1].get("rank_ic") or {}).get("t_nw"))) or 0),
    )
    for name, f in order:
        meta = get_factor(name)
        rank_ic = f.get("rank_ic") or {}
        t_nw = rank_ic.get("t_nw")
        q = (fdr.get(name) or {}).get("q")
        ls = (f.get("long_short") or {}).get("annualized")
        tag, why = verdict_of(f)
        tag_cls = {"值得复核": "t-good", "方向需翻转": "t-warn", "仅统计显著": "t-mid"}.get(tag, "t-bad")
        rows.append(
            "<tr>"
            f'<td><b>{name}</b><div class="sub" style="margin:0">{meta["label"]}</div></td>'
            f'<td>{CATEGORY_LABELS.get(meta["category"], meta["category"])}</td>'
            f'<td class="{"up" if meta["direction"] > 0 else "down"}">'
            f'{"越大越好" if meta["direction"] > 0 else "越小越好"}</td>'
            f'<td>{fmt(rank_ic.get("mean"), 4, True)}</td>'
            f'<td>{ic_bar(rank_ic.get("mean"))}</td>'
            f'<td class="{cls_t(t_nw)}">{fmt(t_nw, 2, True)}</td>'
            f'<td>{fmt(q, 3)}</td>'
            f'<td>{fmt(f.get("monotonicity"), 2, True)}</td>'
            f'<td class="{cls_pct(ls)}">{fmt(ls, 2, True, pct=True)}</td>'
            f'<td>{fmt(f.get("turnover"), 2)}</td>'
            f'<td><span class="tag {tag_cls}">{tag}</span></td>'
            "</tr>"
        )
    return (
        "<table><thead><tr>"
        "<th>因子</th><th>类别</th><th>方向</th><th>IC 均值</th><th>强弱</th>"
        "<th>t (NW)</th><th>q 值</th><th>单调性</th><th>多空年化</th><th>换手</th><th>判断</th>"
        "</tr></thead><tbody>" + "".join(rows) + "</tbody></table>"
    )


def segment_table(seg: Dict[str, Any]) -> str:
    rows = seg.get("rows") or []
    if not rows:
        return '<p class="sub">尚未生成分板块结果（segment_ic.py 未运行或样本不足）。</p>'
    by_factor: Dict[str, Dict[str, Any]] = {}
    pools: List[str] = []
    for r in rows:
        by_factor.setdefault(r["factor"], {})[r["pool"]] = r
        if r["pool"] not in pools:
            pools.append(r["pool"])
    pools = [p for p in pools if p != "全市场"] + (["全市场"] if "全市场" in pools else [])

    head = "".join(f"<th>{p}</th>" for p in pools)
    body = []
    summary = {s["factor"]: s for s in (seg.get("summary") or [])}
    for name, m in by_factor.items():
        cells = []
        for p in pools:
            r = m.get(p)
            if not r:
                cells.append('<td class="flat">-</td>')
                continue
            ic = num(r.get("ic_mean"))
            t = num(r.get("t_nw"))
            mark = " ●" if (t is not None and abs(t) >= 2) else ""
            cells.append(f'<td class="{cls_pct(ic)}">{fmt(ic, 4, True)}{mark}</td>')
        s = summary.get(name) or {}
        flag = "稳健" if s.get("sign_consistent") and (s.get("n_significant") or 0) >= 3 else (
            "板块依赖" if s.get("sign_consistent") is False else "证据弱"
        )
        fcls = {"稳健": "t-good", "板块依赖": "t-bad"}.get(flag, "t-mid")
        body.append(
            f"<tr><td><b>{name}</b></td>" + "".join(cells)
            + f'<td><span class="tag {fcls}">{flag}</span></td></tr>'
        )
    return (
        "<table><thead><tr><th>因子</th>" + head + "<th>判定</th></tr></thead><tbody>"
        + "".join(body) + "</tbody></table>"
        '<p class="sub">表中为各板块 RankIC 均值，● 表示该板块 NW |t| ≥ 2。'
        "板块内样本更小，t 值波动更大；<b>符号不一致比不显著更危险</b>——"
        "说明因子在部分市场有效、在另一部分反向，合并成一条 IC 会互相抵消。</p>"
    )


def corr_clusters(corr: Dict[str, Any], threshold: float = 0.7) -> str:
    if not corr:
        return '<p class="sub">未生成相关矩阵。</p>'
    pairs = []
    names = list(corr.keys())
    for i, a in enumerate(names):
        for b in names[i + 1:]:
            v = num((corr.get(a) or {}).get(b))
            if v is not None and abs(v) >= threshold:
                pairs.append((abs(v), a, b, v))
    if not pairs:
        return f'<p class="sub">没有 |ρ| ≥ {threshold} 的因子对，冗余可接受。</p>'
    pairs.sort(reverse=True)
    rows = "".join(
        f"<tr><td><b>{a}</b> ↔ <b>{b}</b></td>"
        f'<td class="{cls_pct(v)}">{v:+.2f}</td>'
        f'<td class="sub" style="text-align:left">高度重叠，同时使用等于给同一信号重复加权</td></tr>'
        for _, a, b, v in pairs[:14]
    )
    return (
        f'<p>共 <b>{len(pairs)}</b> 对因子相关系数绝对值 ≥ {threshold}：</p>'
        "<table><thead><tr><th>因子对</th><th>相关系数</th><th>含义</th></tr></thead>"
        f"<tbody>{rows}</tbody></table>"
        '<p class="sub">相关系数基于日度横截面值的平均相关。'
        "做因子合成前必须先去冗余，否则权重会严重偏向被重复计算的那一类信号。</p>"
    )


def sample_state_note(trade_days: int) -> str:
    """样本长短决定「单一市场状态」这句话还成不成立。

    短样本（不到 5 年）说「没跨牛熊」是诚实；样本已经跨过 2021 结构牛、
    2022–2023 熊市和 2024 反弹之后还这么说，就是低估自己手上的数据了。
    """
    if trade_days >= 1200:
        return "跨越了 2021 结构牛、2022–2023 熊市与 2024 反弹，「只在一种行情里有效」的风险已明显下降"
    return "处在单一市场状态，没有跨牛熊"


def build_html(evald: Dict[str, Any], seg: Dict[str, Any], stats: Dict[str, Any]) -> str:
    meta = evald.get("meta") or {}
    factors = evald.get("factors") or {}
    fdr = evald.get("fdr") or {}
    corr = evald.get("corr_matrix") or {}

    horizon = meta.get("horizon")
    entry = meta.get("entry", "s1_open")
    n_factors = len(factors) or stats.get("panel_factors", 0)
    coverage = 0.0
    if factors:
        first = next(iter(factors.values()))
        coverage = num(first.get("market_coverage")) or 0.0

    strong = sum(1 for f in factors.values()
                 if (num((f.get("rank_ic") or {}).get("t_nw")) or 0) and abs(num((f.get("rank_ic") or {}).get("t_nw"))) >= 2)
    worth = sum(1 for f in factors.values() if verdict_of(f)[0] == "值得复核")
    flips = sum(1 for f in factors.values() if verdict_of(f)[0] == "方向需翻转")

    kpis = "".join([
        kpi("面板覆盖", f'{stats.get("panel_symbols", 0)} 只',
            f'占行情库 {coverage * 100:.1f}%' if coverage else "占比未计算"),
        kpi("面板行数", f'{stats.get("panel_rows", 0):,}', f'{n_factors} 个因子 × {stats.get("trade_days", 0)} 个交易日'),
        kpi("统计显著", f"{strong} / {n_factors}", "NW 修正后 |t| ≥ 2"),
        kpi("值得复核", f"{worth} 个", f"另有 {flips} 个方向与先验相反"),
    ])

    data_card = f"""
    <div class="grid2">
      <div class="card">
        <h4>行情库</h4>
        <p>标的 <b>{stats.get('daily_symbols', 0)}</b> 只 · 交易日 <b>{stats.get('daily_days', 0)}</b> 个</p>
        <p>区间 <b>{stats.get('daily_start', '-')}</b> ~ <b>{stats.get('daily_end', '-')}</b></p>
        <p>日线 <b>{stats.get('daily_rows', 0):,}</b> 行 · 库文件 <b>{stats.get('db_mb', 0):.0f} MB</b></p>
      </div>
      <div class="card">
        <h4>板块分布</h4>
        <p>{' · '.join(f'{k} <b>{v}</b>' for k, v in stats.get('segments', {}).items())}</p>
        <p class="sub">北交所标的当前不在数据源覆盖内（{stats.get('other', 0)} 只属其他号段）。</p>
      </div>
    </div>
    <div class="card" style="margin-top:16px">
      <h4>因子面板</h4>
      <p><b>{stats.get('panel_rows', 0):,}</b> 行 · <b>{stats.get('panel_symbols', 0)}</b> 只 · <b>{stats.get('panel_factors', 0)}</b> 个因子
         · 重复主键 <b class="{'up' if stats.get('dup_rows') else 'down'}">{stats.get('dup_rows', 0)}</b></p>
      <p class="sub">面板区间 <b>{stats.get('panel_start', '-')} ~ {stats.get('panel_end', '-')}</b>
      （<b>{stats.get('trade_days', 0)}</b> 个交易日）。注意行情库整体区间更长，但绝大多数标的只从面板起点开始有数据。</p>
      <p class="sub">评估口径：{horizon} 日持有期，{'T+1 开盘建仓' if entry == 's1_open' else entry}，T+{horizon} 收盘平仓。
      IC 为逐日横截面 Spearman 秩相关；t 值为 Newey-West 修正（重叠标签会让朴素 t 虚高约 √h 倍）。</p>
    </div>
    """

    return f"""<!DOCTYPE html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>因子体检报告 · {time.strftime('%Y-%m-%d')}</title><style>{CSS}</style></head><body>
<h1>因子体检报告</h1>
<p class="sub">生成于 {time.strftime('%Y-%m-%d %H:%M')} · 数据源 {stats.get('daily_start', '')} ~ {stats.get('daily_end', '')}
 · 全部数字可在 <code>data/factor_eval.json</code> 与 <code>data/segment_ic.json</code> 中复核</p>

<div class="hero">{kpis}</div>

<div class="alert">
<b>先读这一条：这份报告不能告诉你"哪个因子能赚钱"</b>
<ul>
<li>样本 <b>{stats.get('trade_days', 0)} 个交易日</b>（约 {stats.get('trade_days', 0) / 244:.1f} 年），{sample_state_note(stats.get('trade_days', 0))}</li>
<li>面板覆盖 <b>{stats.get('panel_symbols', 0)} 只</b>，占全市场 <b>{coverage * 100:.1f}%</b>{"，样本池仍有板块偏斜" if coverage and coverage < 0.9 else ""}。</li>
<li>库里只有<b>当前仍在上市</b>的标的，退市股不在，收益天然偏高（<b>幸存者偏差未消除</b>）。</li>
<li>{horizon} 日持有期下，标签在时间上高度重叠，独立样本大约只有 <b>{int(stats.get('trade_days', 0) / horizon) if horizon else '-'} 期</b>。</li>
</ul>
结论强度上限：<b>"值得进一步验证"，不构成"可以用于实盘"。</b>
</div>

<div class="alert ok">
<b>可以确认的部分</b>
<ul>
<li>因子计算链路<b>无前视偏差</b>：截断到前 200 行与用全量 300 行算，前 200 行结果逐位相同。</li>
<li>面板落库<b>幂等</b>：重复运行不产生重复行（当前重复主键 {stats.get('dup_rows', 0)} 条）。</li>
<li>显著性判定已修正重叠标签造成的 t 值虚高（Newey-West，lag = max(0, h−1)）。</li>
</ul>
</div>

<h2>一、因子总览</h2>
<p>按 Newey-West t 的绝对值排序。三档判断的含义：</p>
<ul class="plain">
<li><b>值得复核</b>——统计稳定，且分组收益随<b>先验方向</b>单调（先验方向 = 注册表里写的"越大越好/越小越好"）。</li>
<li><b>方向需翻转</b>——统计稳定，但分组收益与先验方向<b>相反</b>。这类因子仍有信息，只是按先验方向使用会反向暴露；要留下就得先把方向反过来。</li>
<li><b>仅统计显著</b>——IC 稳定但分组收益不单调，可能是非线性关系，也可能只是噪声。</li>
</ul>
<p class="sub">"多空年化"是第 1 组减第 10 组的年化收益（按先验方向）；"换手"是 Top 组相邻调仓期的成分更替率，越高越难落地。</p>
{factor_table(factors, fdr)}

<h2>二、分板块一致性</h2>
<p>同一个因子在不同板块（涨跌停幅度、市值结构、投资者构成都不同）表现是否一致。
这一步能抓出"在主板有效、在创业板反向"的因子——这类因子合并成全市场 IC 后会互相抵消，看着不显著，实则是两个方向的信号。</p>
{segment_table(seg)}

<h2>三、冗余检查</h2>
<p>因子之间高度相关时，同时使用等于给同一个信号重复加权。这是做因子合成前必须处理的问题。</p>
{corr_clusters(corr)}

<h2>四、已知缺陷（必须一起读）</h2>
<ul class="plain">
<li><b>turn20 / amihud20 / turn_cv20 的"成交额"是估算值。</b> 数据源不提供流通股本，
<code>turnover</code> 列实际恒等于 成交量 × (高+低+收)/3，因此这三个流动性因子建立在公式化伪成交额上，误差大小无法校验（库里没有真值可比）。</li>
<li><b>dist_high250 / dist_low250 有口径混用。</b> 窗口 250 日，数据不足时退化为"上市以来最高/最低价"。
由于多数标的只有 {stats.get('trade_days', 0)} 个交易日，这类因子的前半段实际是另一个因子。</li>
<li><b>mom120 可用天数明显偏少</b>，因为预热期就吃掉 120 天，在 {stats.get('trade_days', 0)} 天的库上是硬约束。</li>
<li><b>行业与市值中性化尚未做。</b> 未中性化时，测出来的"因子效果"有一部分其实是行业或市值暴露。</li>
</ul>

<h2>五、下一步（按依赖顺序）</h2>
<table><thead><tr><th>顺序</th><th style="text-align:left">要做的事</th><th style="text-align:left">为什么必须在这一步</th></tr></thead><tbody>
<tr><td>1</td><td style="text-align:left">历史扩到 5 年</td>
<td style="text-align:left">{"<b>已完成</b>：当前 " + str(stats.get('trade_days', 0)) + " 个交易日（约 " + "%.1f" % (stats.get('trade_days', 0) / 244) + " 年），已跨牛熊。" if stats.get('trade_days', 0) >= 1200 else "尚缺：当前只有 " + str(stats.get('trade_days', 0)) + " 天，任何「稳定/不稳定」都只是当期特征。代码已支持 <code>--since</code>，执行即可。"}</td></tr>
<tr><td>2</td><td style="text-align:left">按板块分层或随机抽样扩样本</td>
<td style="text-align:left">当前覆盖 {coverage * 100:.1f}%，且历史上用 <code>--limit N</code> 取的是代码升序，存在结构偏斜。</td></tr>
<tr><td>3</td><td style="text-align:left">簇内去冗余 + 因子合成</td>
<td style="text-align:left"><b>已完成</b>：<code>factor_score.py</code> 按 |rho| ≥ 0.7 归簇去冗余，再等权合成并做时间切分检验（页面「智能选股」）。</td></tr>
<tr><td>4</td><td style="text-align:left">行业 + 市值中性化</td>
<td style="text-align:left">不做的话，结论可能是"小市值因子换了件衣服"——这一条<b>尚未做</b>，是当前最大的方法论缺口。</td></tr>
<tr><td>5</td><td style="text-align:left">重写回测引擎</td>
<td style="text-align:left">现有回测只覆盖最近 45 个交易日，且每个组合都复制一遍整库，结论不可用。</td></tr>
<tr><td>6</td><td style="text-align:left">自存不复权价 + 复权因子</td>
<td style="text-align:left">行情用腾讯<b>前复权</b>，除权后历史价格会按最新价重算，导致回测不可复现（今天跑和三个月后跑，同一段历史收益不同）。要能复现，必须自己存原始价与复权因子。</td></tr>
</tbody></table>

<div class="foot">
本报告由 <code>make_factor_report.py</code> 自动生成。所有统计量均可用
<code>python factor_eval.py --factor &lt;名称&gt; --horizon {horizon}</code> 单独复现。
报告不构成投资建议。
</div>
</body></html>"""


def main() -> int:
    ap = argparse.ArgumentParser(description="生成因子体检 HTML 报告")
    ap.add_argument("--eval", default=EVAL)
    ap.add_argument("--seg", default=SEG)
    ap.add_argument("--db", default=DB)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    evald = load_json(args.eval)
    if not evald.get("factors"):
        print(f"未找到评估结果：{args.eval}（先运行 factor_eval.py）")
        return 1
    seg = load_json(args.seg)
    stats = db_stats(args.db)

    html = build_html(evald, seg, stats)
    os.makedirs("reports", exist_ok=True)
    out = args.out or os.path.join("reports", f"factor-report-{time.strftime('%Y%m%d-%H%M')}.html")
    with open(out, "w", encoding="utf-8") as fh:
        fh.write(html)

    n = len(evald["factors"])
    print(f"已生成 {out}（{n} 个因子 / 面板 {stats.get('panel_symbols', 0)} 只 / 分板块 {'有' if seg else '无'}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
