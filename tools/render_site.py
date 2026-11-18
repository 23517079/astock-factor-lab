#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""把因子研究产物 JSON 渲染成一个静态研究看板（单文件 HTML）。

设计原则：
1. 只读产物，不重算任何指标 —— 页面上的数字必须能在 JSON 里原样找到。
2. 字段缺失时不猜、不补零，明确显示「未产出」。
3. 产物时点不一致时逐个标注，不合并成一句「最新」。

用法：
    python tools/render_site.py --data sequoia-service/data --out site
"""

import argparse
import html
import json
import os
import sys
from datetime import datetime

# ---------------------------------------------------------------- 读取与格式化


def load(data_dir, name):
    path = os.path.join(data_dir, name)
    if not os.path.exists(path):
        return None
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception as exc:  # noqa: BLE001
        print("[warn] 无法解析 %s: %s" % (name, exc), file=sys.stderr)
        return None


def dig(obj, *keys, default=None):
    cur = obj
    for key in keys:
        if isinstance(cur, dict) and key in cur:
            cur = cur[key]
        else:
            return default
    return cur


def esc(value):
    return html.escape("" if value is None else str(value))


def num(value, digits=2, suffix="", signed=False):
    """数字格式化。缺失时返回「—」，绝不补 0。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    sign = "+" if (signed and value > 0) else ""
    return "%s%.*f%s" % (sign, digits, value, suffix)


def pct(value, digits=2):
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return "—"
    return "%+.2f%%" % (value * 100) if digits == 2 else "%s%%" % round(value * 100, digits)


def tone(value):
    """A 股习惯：正数红、负数绿。"""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return ""
    return "up" if value > 0 else ("down" if value < 0 else "")


def badge(text, level):
    return '<span class="badge %s">%s</span>' % (level, esc(text))


# ---------------------------------------------------------------- 区块渲染


def render_gate(score):
    gate = dig(score, "evidenceGate") or {}
    passed = gate.get("passed")
    level = gate.get("level") or ("pass" if passed else "fail")
    failed = gate.get("failed") or gate.get("reasons") or []
    note = gate.get("note") or ""

    if passed is True:
        head, cls, title = "证据门槛通过", "ok", "扣费后有效性与敏感性检查均已达标"
    elif gate:
        head, cls, title = "证据门槛未通过", "bad", "名单照常产出，但不足以支持「扣费后有效」的结论"
    else:
        head, cls, title = "证据门槛未产出", "warn", "本轮产物中没有 evidenceGate 字段"
        level = "warn"

    items = "".join("<li>%s</li>" % esc(x) for x in failed) or "<li>无失败项</li>"
    return """
<section class="gate %s">
  <div class="gate-head">
    <div>
      <div class="gate-title">%s %s</div>
      <div class="gate-sub">%s</div>
    </div>
    <div class="gate-level">%s</div>
  </div>
  <ul class="gate-list">%s</ul>
  %s
</section>""" % (
        cls,
        head,
        badge("level=%s" % level, "b-" + ("ok" if level == "pass" else "bad")),
        esc(title),
        esc((gate.get("mode") or "warn") + " 模式"),
        items,
        '<div class="gate-note">%s</div>' % esc(note) if note else "",
    )


def render_metrics(score, roll):
    base = dig(score, "costSensitivity", "base") or {}
    doubled = dig(score, "costSensitivity", "doubled") or {}
    cards = [
        ("净年化 Top10", pct(base.get("netTop10Annual")), "扣真实费率后", tone(base.get("netTop10Annual"))),
        ("NW t 值", num(base.get("tNw"), 2), "净收益序列", ""),
        ("十分组净价差", pct(base.get("netTopSpreadAnnual")), "年化", tone(base.get("netTopSpreadAnnual"))),
        ("样本笔数", "{:,}".format(base.get("n_trades")) if isinstance(base.get("n_trades"), int) else "—",
         "买入日 %s" % (base.get("n_buy_dates") or "—"), ""),
        ("费用翻倍后", pct(doubled.get("netTop10Annual")), "敏感性检查", tone(doubled.get("netTop10Annual"))),
        ("滚动期数", str(roll.get("n_periods") or "—") if roll else "—",
         "step=%s window=%s" % (dig(roll, "params", "rollStep") or "—", dig(roll, "params", "rollSelectWindow") or "—"), ""),
    ]
    out = []
    for label, value, sub, cls in cards:
        out.append(
            '<div class="card"><div class="card-label">%s</div><div class="card-value %s">%s</div>'
            '<div class="card-sub">%s</div></div>' % (esc(label), cls, esc(value), esc(sub))
        )
    return '<section class="grid">%s</section>' % "".join(out)


def render_cost_table(score):
    cs = dig(score, "costSensitivity") or {}
    cost = dig(score, "cost") or {}
    rows = []
    for key, label in (("base", "计入真实费率"), ("doubled", "费率 × 2")):
        block = cs.get(key) or {}
        verdict = dig(block, "gate")
        rows.append(
            "<tr><td>%s</td><td class='%s'>%s</td><td>%s</td><td class='%s'>%s</td>"
            "<td>%s</td><td>%s</td></tr>" % (
                esc(label),
                tone(block.get("netTop10Annual")), esc(pct(block.get("netTop10Annual"))),
                esc(num(block.get("tNw"), 2)),
                tone(block.get("netTopSpreadAnnual")), esc(pct(block.get("netTopSpreadAnnual"))),
                esc("{:,}".format(block.get("n_trades")) if isinstance(block.get("n_trades"), int) else "—"),
                esc(num(block.get("n_unsettled"), 0)),
            )
        )
    verdict = cs.get("verdict") or "—"
    vcls = "b-ok" if verdict == "pass" else ("b-bad" if verdict == "fail" else "b-warn")
    return """
<section class="block">
  <h2>扣费验证 <span class="muted">roundTrip %s bp · multiplier %s</span></h2>
  <table>
    <thead><tr><th>情景</th><th>净年化 Top10</th><th>NW t</th><th>十分组净价差</th><th>样本笔数</th><th>未结算</th></tr></thead>
    <tbody>%s</tbody>
  </table>
  <div class="rowline">
    <span>结论 %s</span>
    <span class="muted">保留边际 %s · 阈值 minRetained %s / minT %s</span>
  </div>
  <div class="quote">%s</div>
  <div class="quote muted">%s</div>
</section>""" % (
        esc(num(cost.get("roundTripBps"), 1)),
        esc(num(cost.get("multiplier"), 1)),
        "".join(rows),
        badge(verdict, vcls),
        esc(num(cs.get("retainedEdge"), 2)),
        esc(dig(cs, "thresholds", "minRetained")),
        esc(dig(cs, "thresholds", "minT")),
        esc(cs.get("note") or ""),
        esc(dig(cs, "base", "icCostNote") or ""),
    )


def render_ic_vs_portfolio(score):
    val = dig(score, "validation") or {}
    base = dig(score, "costSensitivity", "base") or {}
    return """
<section class="block">
  <h2>因子层 vs 组合层 <span class="muted">为什么看 IC 会骗人</span></h2>
  <div class="split">
    <div class="half">
      <div class="half-label">因子层（横截面）</div>
      <div class="kv"><span>IC 均值</span><b>%s</b></div>
      <div class="kv"><span>ICIR</span><b>%s</b></div>
      <div class="kv"><span>NW t</span><b>%s</b></div>
      <div class="kv"><span>样本天数</span><b>%s</b></div>
    </div>
    <div class="half">
      <div class="half-label">组合层（扣费后）</div>
      <div class="kv"><span>净年化 Top10</span><b class="%s">%s</b></div>
      <div class="kv"><span>NW t</span><b>%s</b></div>
      <div class="kv"><span>十分组净价差</span><b class="%s">%s</b></div>
      <div class="kv"><span>结论</span><b>%s</b></div>
    </div>
  </div>
  <div class="quote">IC 显著不等于组合能赚钱：统一费率是横截面单调变换，不改变同日排名，Spearman IC 天然不含成本信息。</div>
</section>""" % (
        esc(num(val.get("ic_mean"), 4)),
        esc(num(val.get("icir"), 2)),
        esc(num(val.get("t_nw"), 2)),
        esc(str(val.get("n_days") or "—")),
        tone(base.get("netTop10Annual")), esc(pct(base.get("netTop10Annual"))),
        esc(num(base.get("tNw"), 2)),
        tone(base.get("netTopSpreadAnnual")), esc(pct(base.get("netTopSpreadAnnual"))),
        esc(dig(score, "costSensitivity", "verdict") or "—"),
    )


def render_factors(score):
    meta = dig(score, "factorMeta") or {}
    factors = dig(score, "factors") or []
    if not factors:
        return ""
    rows = []
    for name in factors:
        info = meta.get(name) or {}
        flipped = info.get("flipped")
        rows.append(
            "<tr><td class='mono'>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>" % (
                esc(name),
                esc(info.get("label") or "—"),
                esc(info.get("direction")),
                esc(info.get("directionEff")),
                badge("已翻转", "b-warn") if flipped else '<span class="muted">—</span>',
            )
        )
    basis = dig(score, "directionBasis") or {}
    return """
<section class="block">
  <h2>当前生效因子 <span class="muted">%d 个 · %s 权重</span></h2>
  <div class="quote">方向基准：asOf %s，icCutoff %s，判据 <code>%s</code>；本期翻转 %d 个。</div>
  <table>
    <thead><tr><th>因子</th><th>含义</th><th>先验方向</th><th>生效方向</th><th>状态</th></tr></thead>
    <tbody>%s</tbody>
  </table>
</section>""" % (
        len(factors),
        esc(score.get("weight") or "equal"),
        esc(basis.get("asOf") or "—"),
        esc(basis.get("icCutoff") or "—"),
        esc(basis.get("criterion") or "—"),
        len(basis.get("flipped") or []),
        "".join(rows),
    )


def render_picks(score):
    picks = dig(score, "picks") or {}
    items = picks.get("picks") or []
    if not items:
        return ""
    rows = []
    for item in items[:10]:
        contrib = item.get("contributions") or []
        top = "、".join(
            "%s(%s)" % (c.get("factor"), "+" if (c.get("contribution") or 0) >= 0 else "")
            for c in contrib[:3]
        )
        rows.append(
            "<tr><td>%s</td><td class='mono'>%s</td><td>%s</td><td class='%s'>%s</td>"
            "<td>%s</td><td>%s</td><td class='small'>%s</td></tr>" % (
                esc(item.get("rank")),
                esc(item.get("symbol")),
                esc(item.get("name")),
                tone(item.get("score")), esc(num(item.get("score"), 4)),
                esc(item.get("validFactors")),
                esc(item.get("signal") or "—"),
                esc(top),
            )
        )
    return """
<section class="block">
  <h2>候选池 <span class="muted">%s · 共 %d 只（展示前 10）</span></h2>
  <table>
    <thead><tr><th>排名</th><th>代码</th><th>名称</th><th>综合分</th><th>有效因子</th><th>信号</th><th>主要贡献</th></tr></thead>
    <tbody>%s</tbody>
  </table>
  <div class="quote muted">名单是产物，不是建议。上方证据门槛为 %s 时，此名单仅供观察。</div>
</section>""" % (
        esc(picks.get("date") or "—"),
        len(items),
        "".join(rows),
        esc(dig(score, "evidenceGate", "level") or "未知"),
    )


def render_lookahead(roll):
    if not roll:
        return ""
    lag = roll.get("lag") or {}
    ok = lag.get("ok")
    violations = lag.get("violations") or []
    latest = roll.get("latest") or {}
    summary = roll.get("summary") or {}
    violet = "".join("<li>%s</li>" % esc(v) for v in violations[:5]) or "<li>无越界</li>"
    return """
<section class="block">
  <h2>前视门禁 <span class="muted">断言式拦截</span></h2>
  <div class="rowline">
    <span>lag 校验 %s</span>
    <span class="muted">cutoff %s · 越界 %d 条</span>
  </div>
  <ul class="tight">%s</ul>
  <div class="split">
    <div class="half">
      <div class="half-label">最新一期</div>
      <div class="kv"><span>asOf</span><b>%s</b></div>
      <div class="kv"><span>icCutoff</span><b>%s</b></div>
      <div class="kv"><span>入选因子</span><b>%s / %s</b></div>
      <div class="kv"><span>方向调整后 IC</span><b>%s</b></div>
    </div>
    <div class="half">
      <div class="half-label">滚动摘要（%s 期）</div>
      <div class="kv"><span>原始 avgIC</span><b>%s</b></div>
      <div class="kv"><span>方向调整 avgIC</span><b>%s</b></div>
      <div class="kv"><span>近 5 期调整 avgIC</span><b>%s</b></div>
      <div class="kv"><span>入选实例数</span><b>%s</b></div>
    </div>
  </div>
  <div class="quote">原始 avgIC 为负、方向调整后转正，说明多数因子与先验方向相反——这正是逐期重定方向的必要性。</div>
</section>""" % (
        badge("通过" if ok else "未通过", "b-ok" if ok else "b-bad"),
        esc(lag.get("cutoff") or "—"),
        len(violations),
        violet,
        esc(latest.get("asOf") or "—"),
        esc(latest.get("icCutoff") or "—"),
        esc(latest.get("n_selected")),
        esc(latest.get("n_evaluable")),
        esc(num(latest.get("avgIcSelectedAdj"), 4)),
        esc(summary.get("nUsablePeriods")),
        esc(num(summary.get("avgSelectedIc"), 4)),
        esc(num(summary.get("avgSelectedIcAdj"), 4)),
        esc(num(summary.get("avgSelectedIcAdjRecent5"), 4)),
        esc(summary.get("instanceCount")),
    )


def render_panel(panel, run):
    if not panel:
        return ""
    st = panel.get("structure") or {}
    cov = panel.get("coverage") or {}
    ver = panel.get("verify") or {}
    problems = panel.get("problems") or []
    verify = dig(run, "verify") or {}
    prob = "".join("<li>%s</li>" % esc(p) for p in problems) or "<li>无异常</li>"
    return """
<section class="block">
  <h2>数据体检 <span class="muted">面板完整性 + 抽样复算</span></h2>
  <div class="split">
    <div class="half">
      <div class="half-label">面板结构</div>
      <div class="kv"><span>行数</span><b>%s</b></div>
      <div class="kv"><span>股票数</span><b>%s</b></div>
      <div class="kv"><span>因子数</span><b>%s</b></div>
      <div class="kv"><span>日期区间</span><b>%s → %s</b></div>
      <div class="kv"><span>主键重复</span><b>%s</b></div>
    </div>
    <div class="half">
      <div class="half-label">抽样复算</div>
      <div class="kv"><span>抽样单元</span><b>%s</b></div>
      <div class="kv"><span>比对单元格</span><b>%s</b></div>
      <div class="kv"><span>不一致</span><b class="%s">%s</b></div>
      <div class="kv"><span>流水线最大相对误差</span><b>%s</b></div>
      <div class="kv"><span>覆盖股票</span><b>%s / %s</b></div>
    </div>
  </div>
  <ul class="tight">%s</ul>
</section>""" % (
        esc("{:,}".format(st.get("panel_rows")) if isinstance(st.get("panel_rows"), int) else "—"),
        esc("{:,}".format(st.get("panel_symbols")) if isinstance(st.get("panel_symbols"), int) else "—"),
        esc(st.get("panel_factors")),
        esc(st.get("date_min")), esc(st.get("date_max")),
        esc(st.get("dup_pk")),
        esc(ver.get("sampled")),
        esc(ver.get("cells_compared")),
        "down" if ver.get("mismatch_cells") == 0 else "up", esc(ver.get("mismatch_cells")),
        esc("%.2e" % verify.get("maxRelErr") if isinstance(verify.get("maxRelErr"), (int, float)) else "—"),
        esc(cov.get("covered")), esc(cov.get("universe")),
        prob,
    )


def render_footer(sources):
    rows = "".join(
        "<tr><td class='mono'>%s</td><td>%s</td><td>%s</td></tr>" % (esc(name), esc(ts or "—"), esc(note))
        for name, ts, note in sources
    )
    return """
<section class="block">
  <h2>产物时点</h2>
  <table>
    <thead><tr><th>文件</th><th>生成时间</th><th>说明</th></tr></thead>
    <tbody>%s</tbody>
  </table>
  <div class="quote">多个产物时点不一致时逐项标注，不合并成一句「最新」。页面只做展示，不重算任何指标。</div>
  <div class="quote muted">重计算环境在本地（25 GB 行情库 + 境内数据源）。云端只负责渲染这个页面。</div>
</section>""" % rows


CSS = """
:root{--bg:#0e1116;--panel:#151a21;--panel2:#1b222b;--line:#262e3a;--text:#e6e9ee;--muted:#8a94a6;
--up:#e5554e;--down:#3fae7a;--ok:#3fae7a;--bad:#e5554e;--warn:#d9a23b;--accent:#5b9bd5}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);
font:14px/1.65 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1080px;margin:0 auto;padding:32px 20px 64px}
header h1{font-size:22px;font-weight:600;margin:0 0 6px}
header .meta{color:var(--muted);font-size:13px}
.muted{color:var(--muted);font-weight:400}
h2{font-size:15px;font-weight:600;margin:0 0 14px}
.block,.gate{background:var(--panel);border:1px solid var(--line);border-radius:10px;
padding:18px 20px;margin:16px 0}
.gate.ok{border-color:rgba(63,174,122,.45)}
.gate.bad{border-color:rgba(229,85,78,.5)}
.gate.warn{border-color:rgba(217,162,59,.45)}
.gate-head{display:flex;justify-content:space-between;align-items:flex-start;gap:16px}
.gate-title{font-size:16px;font-weight:600;display:flex;align-items:center;gap:10px}
.gate-sub{color:var(--muted);font-size:13px;margin-top:4px}
.gate-level{color:var(--muted);font-size:12px;white-space:nowrap}
.gate-list{margin:12px 0 0;padding-left:20px;color:var(--text);font-size:13px}
.gate-note{margin-top:10px;color:var(--muted);font-size:12.5px}
.badge{display:inline-block;font-size:11px;padding:2px 8px;border-radius:999px;
border:1px solid currentColor;line-height:1.6}
.b-ok{color:var(--ok)}.b-bad{color:var(--bad)}.b-warn{color:var(--warn)}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:12px;margin:16px 0}
.card{background:var(--panel2);border:1px solid var(--line);border-radius:10px;padding:14px 16px}
.card-label{color:var(--muted);font-size:12px}
.card-value{font-size:22px;font-weight:600;margin:4px 0 2px;font-variant-numeric:tabular-nums}
.card-sub{color:var(--muted);font-size:11.5px}
.up{color:var(--up)}.down{color:var(--down)}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;
padding:8px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:8px 10px;border-bottom:1px solid rgba(38,46,58,.6);font-variant-numeric:tabular-nums}
tr:last-child td{border-bottom:none}
.mono{font-family:ui-monospace,Consolas,monospace;font-size:12.5px}
.small{font-size:12px;color:var(--muted)}
.rowline{display:flex;justify-content:space-between;gap:16px;flex-wrap:wrap;
margin-top:12px;font-size:13px}
.quote{margin-top:12px;padding:10px 12px;background:rgba(91,155,213,.08);
border-left:2px solid var(--accent);border-radius:0 6px 6px 0;font-size:12.5px;color:#c6d2e0}
.split{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin:8px 0}
.half{background:var(--panel2);border:1px solid var(--line);border-radius:8px;padding:14px}
.half-label{color:var(--muted);font-size:12px;margin-bottom:8px}
.kv{display:flex;justify-content:space-between;font-size:13px;padding:3px 0}
.kv span{color:var(--muted)}
.kv b{font-weight:500;font-variant-numeric:tabular-nums}
.tight{margin:8px 0 0;padding-left:20px;font-size:12.5px;color:var(--muted)}
code{background:rgba(255,255,255,.06);padding:1px 5px;border-radius:4px;font-size:12px}
footer{color:var(--muted);font-size:12px;margin-top:28px;text-align:center}
@media(max-width:720px){.split{grid-template-columns:1fr}.card-value{font-size:19px}}
"""


def build(data_dir):
    run = load(data_dir, "factor_run.json")
    score = load(data_dir, "factor_score.json")
    roll = load(data_dir, "factor_rolling.json")
    panel = load(data_dir, "panel_check.json")
    picks_old = load(data_dir, "picks.json")

    if not score:
        raise SystemExit("缺少 factor_score.json，无法渲染")

    sources = [
        ("factor_run.json", str(run.get("finishedAt")) if run else None, "流水线运行状态"),
        ("factor_score.json", str(score.get("generatedAt")), "打分与扣费验证（主数据源）"),
        ("factor_rolling.json", str(roll.get("generatedAt")) if roll else None, "70 期滚动评估"),
        ("panel_check.json", "体检耗时 %ss" % panel.get("elapsed_sec") if panel else None, "面板完整性与抽样复算"),
    ]
    if picks_old:
        sources.append(
            ("picks.json", str(picks_old.get("generatedAt")),
             "独立候选池产物（时点 %s，早于主数据源）" % (dig(picks_old, "picks", "date") or "—"))
        )

    cost = dig(score, "cost") or {}
    head = """
<header>
  <h1>A 股因子研究与验证看板</h1>
  <div class="meta">数据截止 %s · 打分模式 %s · 门禁 %s 模式 · 成本口径 %s（%s bp）· 生成于 %s</div>
</header>""" % (
        esc(score.get("dataThrough") or "—"),
        esc(score.get("mode") or "—"),
        esc(score.get("gateMode") or "—"),
        esc(cost.get("profile") or "—"),
        esc(num(cost.get("roundTripBps"), 1)),
        esc(datetime.now().strftime("%Y-%m-%d %H:%M")),
    )

    body = "".join([
        head,
        render_gate(score),
        render_metrics(score, roll),
        render_cost_table(score),
        render_ic_vs_portfolio(score),
        render_factors(score),
        render_picks(score),
        render_lookahead(roll),
        render_panel(panel, run),
        render_footer(sources),
        '<footer>页面由 tools/render_site.py 从产物 JSON 生成 · 不重算任何指标 · 数字可在 JSON 中原样核对</footer>',
    ])

    return (
        '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        "<title>A 股因子研究与验证看板</title><style>%s</style></head>"
        '<body><div class="wrap">%s</div></body></html>' % (CSS, body)
    )


def main():
    parser = argparse.ArgumentParser(description="渲染因子研究静态看板")
    parser.add_argument("--data", default="sequoia-service/data", help="产物目录")
    parser.add_argument("--out", default="site", help="输出目录")
    args = parser.parse_args()

    page = build(args.data)
    os.makedirs(args.out, exist_ok=True)
    target = os.path.join(args.out, "index.html")
    with open(target, "w", encoding="utf-8") as fh:
        fh.write(page)
    print("[ok] 已生成 %s（%.1f KB）" % (target, len(page.encode("utf-8")) / 1024))


if __name__ == "__main__":
    main()
