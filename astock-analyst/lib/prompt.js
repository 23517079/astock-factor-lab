'use strict';
/*
 * prompt.js — 提示词与输出模板
 *
 * 两条设计原则：
 *  1. 系统提示词把「固定字段 JSON + 每条结论必须挂数值与时间 + 禁止套话」写成硬约束。
 *  2. 用户消息里塞的是已经算好的客观因子，模型只做「因子 → 结论 → 建议」的推理，
 *     不负责取数，这样即使模型输出不合规，下游拿到的因子数据依然是可靠的。
 */

var SCHEMA = {
  /* 输出 JSON 的字段定义，同时用于 parse.js 的校验与降级 */
  analysis: {
    symbol: { type: 'string', required: true, default: '' },
    name: { type: 'string', default: '' },
    dataAsOf: { type: 'string', default: '' },
    dataCoverage: { type: 'string', default: '' },
    verdict: { type: 'string', required: true, default: '观望',
      label: '方向', desc: '买入 / 加仓 / 持有 / 减仓 / 观望 五选一' },
    conviction: { type: 'number', default: 0, label: '确信度', desc: '0-100' },
    positionAdvice: { type: 'string', default: '', label: '建议仓位区间',
      desc: '例如：维持当前仓位不加 / 减至 3 成 / 提升至 5 成上限' },
    keyLevels: {
      type: 'object', default: {}, label: '关键价位',
      desc: '含 stopLoss（失效/止损价）、takeProfit（止盈/目标价）、support（支撑）、resistance（压力）'
    },
    triggers: { type: 'array', default: [], label: '触发条件',
      desc: '满足哪些条件才执行该建议，每条含 condition 与 value' },
    invalidations: { type: 'array', default: [], label: '失效条件',
      desc: '出现哪些情况说明判断错了、必须撤销，每条含 condition 与 value' },
    evidenceChain: { type: 'array', default: [], label: '依据链条',
      desc: '每项含 dimension（维度）、factor（因子）、observation（观测值含数值与时间）、implication（该因子指向的结论）' },
    dimensionScores: { type: 'object', default: {}, label: '五维打分',
      desc: 'fundamental / capital / technical / sentiment / macro 各 -100 到 100' },
    usChinaRelation: { type: 'string', default: '', label: '中美联动结论',
      desc: '当前是联动还是背离，说明传导路径与对明日的影响' },
    risks: { type: 'array', default: [], label: '风险提示' },
    scenarios: { type: 'array', default: [], label: '情景推演',
      desc: '每项含 scenario（情景）、condition（触发条件）、impact（影响）、action（应对）' },
    dataGaps: { type: 'array', default: [], label: '数据缺口说明' },
    disclaimer: { type: 'string', default: '' }
  }
};

var SYSTEM_PROMPT = [
  '你是一名 A 股研究助理，服务于一位个人投资者。你的任务是基于「已经采集好的客观因子数据」，输出一份结构化的分析结论。',
  '',
  '# 硬性输出要求（违反任意一条都算输出无效）',
  '1. 只输出一个 JSON 对象，不要输出任何解释文字、不要用 Markdown 代码块包裹。',
  '2. JSON 必须严格符合下面的字段结构，字段名不得增删改：',
  JSON.stringify(SCHEMA.analysis, null, 0),
  '3. 每一条 evidenceChain 的 observation 必须同时包含「具体数值」和「时间」，例如「主力资金 11 月 12 日净流出 1.2 亿元」「PE(TTM) 132.7 倍（11-12 收盘价 111.72 元）」。',
  '4. 禁止出现以下无信息量的表述：「比买入时少了很多」「大幅波动」「走势不明朗」「市场情绪较低」「估值偏高/偏低」而无具体数字支撑。凡是你写的每一句判断，都必须能追溯到输入数据里的某个具体数值与时间。',
  '5. 如果某个维度数据缺失，就在 dataGaps 里写明缺什么，并让该维度的 dimensionScores 取 0（中性），不要用猜测填补，也不要用「基本面向好」这类空话。',
  '6. verdict 只能取以下五者之一：买入 / 加仓 / 持有 / 减仓 / 观望。',
  '7. keyLevels 里的 stopLoss / takeProfit / support / resistance 必须是具体价格数字（字符串或数字均可），不能写「视情况而定」。若数据不足以给出有意义的价位，就把该字段设为空字符串并在 dataGaps 说明。',
  '8. triggers 与 invalidations 各自至少给出一条；每条写成 { condition: "中文条件描述", value: "具体阈值或数值" }。',
  '9. 结论要能改变决策：如果所有数据都不足以支持动作，就选「观望」并说明需要等待什么信号，不要为了给建议而给建议。',
  '10. 风险提示里不要写「市场有风险」这类免责套话，写具体的风险来源（例如：两融余额 5 日上升 1.14% 但股价 5 日跌 1.79%，存在杠杆资金被动减仓压力）。',
  '',
  '# 免责',
  '本分析仅为基于公开数据的辅助判断，不构成投资建议，不承诺任何收益。',
  'disclaimer 字段请填：「本分析基于公开数据与量化因子，仅供个人投资纪律参考，不构成投资建议，不承诺收益。市场有风险，决策与后果由投资者自行承担。」'
].join('\n');

/* 把因子包渲染成模型可读的文本 */
function renderFactors(analysis, opts) {
  opts = opts || {};
  var L = [];
  var q = analysis.quote || {};
  L.push('# 标的');
  L.push('代码：' + analysis.code + '｜名称：' + analysis.name);
  L.push('现价：' + fmt(q.price) + ' 元｜当日涨跌：' + fmtPct(q.changePct) + '%｜今开 ' + fmt(q.open) +
    '｜最高 ' + fmt(q.high) + '｜最低 ' + fmt(q.low) + '｜昨收 ' + fmt(q.prevClose));
  L.push('行情时间：' + (q.asOf || '未取得') + '｜来源：' + (analysis.quoteSource || '未取得'));
  L.push('数据采集时间：' + analysis.collectedAt);
  L.push('');

  (analysis.dims || []).forEach(function (d) {
    L.push('# ' + d.label + '（维度）');
    if (!d.data || !d.data.available) {
      L.push('- 数据不可用：' + ((d.data && d.data.missing) || []).join('、'));
      L.push('');
      return;
    }
    if (d.key === 'technical') {
      var t = d.data;
      L.push('- 收盘价：' + fmt(t.price) + ' 元（' + t.date + '）');
      L.push('- 均线：MA5 ' + fmt(t.ma5) + ' / MA10 ' + fmt(t.ma10) + ' / MA20 ' + fmt(t.ma20) + ' / MA60 ' + fmt(t.ma60) +
        '；收盘价位于：' + t.aboveMaText);
      L.push('- 趋势判定：' + t.trend + '｜依据：' + (t.trendBasis || []).join('；'));
      L.push('- 量价形态：' + t.pattern + '（' + t.patternNote + '）');
      L.push('- 量能比（5日均量/20日均量）：' + fmt(t.volumeRatio5v20) + ' 倍');
      L.push('- 区间表现：5日 ' + fmtPct(t.ret5) + '%｜10日 ' + fmtPct(t.ret10) + '%｜20日 ' + fmtPct(t.ret20) +
        '%｜60日 ' + fmtPct(t.ret60) + '%');
      L.push('- 年化波动率：' + fmt(t.volatilityAnnualPct) + '%');
      L.push('- 关键位：20日支撑 ' + fmt(t.support20) + ' / 20日压力 ' + fmt(t.resistance20) +
        '｜60日支撑 ' + fmt(t.support60) + ' / 60日压力 ' + fmt(t.resistance60));
      if (t.denseZone) {
        L.push('- 成交密集区：' + t.denseZone.lower + '–' + t.denseZone.upper + ' 元（占近 20 日成交量 ' +
          t.denseZone.sharePct + '%，分档宽度 ' + t.denseZone.bucketSize + ' 元）——这是主要持仓成本区');
      }
      if (t.indicatorTrend) {
        L.push('- 指标共振判断：' + t.indicatorTrend + '（MACD/KDJ/RSI/BOLL/DMA 五指标多空统计，仅供参考）');
      }
      if (t.indicators && t.indicators.ok && t.indicators.signals && t.indicators.signals.length) {
        L.push('- 技术指标明细（口径对齐同花顺/通达信）：');
        t.indicators.signals.forEach(function (s) {
          L.push('  · ' + s.factor + '：' + s.value + '｜' + s.detail);
        });
      }
      L.push('- 日K 来源：' + t.source + '｜截止 ' + t.asOf + '｜样本 ' + t.bars + ' 根');
    } else if (d.key === 'macro') {
      (d.data.items || []).forEach(function (it) {
        L.push('- ' + it.factor + '：' + it.value + '｜' + it.detail + '（时间 ' + (it.asOf || '未取得') + '，来源 ' + it.source + '）');
      });
    } else {
      (d.data.items || []).forEach(function (it) {
        L.push('- ' + it.factor + '：' + it.value + '｜' + it.detail + '（时间 ' + (it.asOf || '未取得') + '，来源 ' + it.source + '）');
      });
    }
    L.push('');
  });

  var link = analysis.usChinaLink || {};
  L.push('# 中美联动（隔夜先行指标）');
  if (link.available) {
    (link.items || []).forEach(function (it) {
      L.push('- ' + it.factor + '：' + it.value + '｜' + it.detail + '（时间 ' + (it.asOf || '未取得') + '，来源 ' + it.source + '）');
    });
    (link.relations || []).forEach(function (r) {
      L.push('- [' + r.type + '，置信度 ' + r.confidence + '] ' + r.text);
    });
    (link.channels || []).forEach(function (c) {
      L.push('- 传导路径 · ' + c.name + '：' + c.text);
    });
  } else {
    L.push('- 数据不可用：' + ((link.missing) || []).join('、'));
  }
  L.push('');

  var cov = analysis.coverage || {};
  L.push('# 数据完整性');
  L.push('- 覆盖维度：' + cov.covered + '/' + cov.total + '｜缺失：' + ((cov.missing || []).join('；') || '无'));
  if ((cov.notApplicableDims || []).length) {
    L.push('- 以下维度对该标的不适用（非数据缺失，请勿据此扣分）：' + cov.notApplicableDims.join('、'));
  }
  if ((cov.notApplicable || []).length) {
    L.push('- 以下项目对该标的不适用（非数据缺失，请勿据此扣分）：' + cov.notApplicable.join('；'));
  }
  if (opts.extraContext) {
    L.push('');
    L.push('# 额外上下文');
    L.push(opts.extraContext);
  }
  return L.join('\n');
}

/* 构造完整消息数组 */
function buildMessages(analysis, opts) {
  return [
    { role: 'system', content: SYSTEM_PROMPT },
    { role: 'user', content: renderFactors(analysis, opts) }
  ];
}

function fmt(v) { return (v === null || v === undefined || v === '' || !isFinite(Number(v))) ? '无数据' : Number(v); }
function fmtPct(v) {
  if (v === null || v === undefined || !isFinite(Number(v))) return '无数据';
  return (Number(v) >= 0 ? '+' : '') + Number(v);
}

/* ---------- 输出模板 ---------- */

/* 固定字段的 JSON 输出（批量任务直接消费） */
function renderJson(result) {
  return JSON.stringify(result, null, 2);
}

/* 固定格式的文本输出（人读 / 日志） */
function renderText(result) {
  var L = [];
  var a = result.analysis || {};
  L.push('════════════════════════════════════════');
  L.push('标的：' + (a.name || '-') + '（' + (a.symbol || '-') + '）');
  L.push('方向：' + (a.verdict || '-') + '｜确信度：' + (a.conviction || 0) + '/100');
  L.push('建议仓位：' + (a.positionAdvice || '未给出'));
  L.push('数据时间：' + (a.dataAsOf || '-') + '｜覆盖：' + (a.dataCoverage || '-'));
  L.push('════════════════════════════════════════');
  L.push('');
  L.push('【关键价位】');
  var k = a.keyLevels || {};
  L.push('  止损/失效：' + (k.stopLoss || '未给出') + '｜止盈/目标：' + (k.takeProfit || '未给出') +
    '｜支撑：' + (k.support || '未给出') + '｜压力：' + (k.resistance || '未给出'));
  L.push('');
  L.push('【触发条件】（满足才执行）');
  (a.triggers || []).forEach(function (t, i) { L.push('  ' + (i + 1) + '. ' + (t.condition || '') + ' → ' + (t.value || '')); });
  if (!(a.triggers || []).length) L.push('  （未给出）');
  L.push('');
  L.push('【失效条件】（出现即撤销判断）');
  (a.invalidations || []).forEach(function (t, i) { L.push('  ' + (i + 1) + '. ' + (t.condition || '') + ' → ' + (t.value || '')); });
  if (!(a.invalidations || []).length) L.push('  （未给出）');
  L.push('');
  L.push('【依据链条】因子 → 结论 → 建议');
  (a.evidenceChain || []).forEach(function (e, i) {
    L.push('  ' + (i + 1) + '. [' + (e.dimension || '-') + '] ' + (e.factor || '-'));
    L.push('     观测：' + (e.observation || '-'));
    L.push('     指向：' + (e.implication || '-'));
  });
  if (!(a.evidenceChain || []).length) L.push('  （未给出）');
  L.push('');
  L.push('【五维打分】');
  var ds = a.dimensionScores || {};
  ['fundamental', 'capital', 'technical', 'sentiment', 'macro'].forEach(function (k2) {
    var label = { fundamental: '基本面', capital: '资金面', technical: '技术面', sentiment: '情绪面', macro: '宏观' }[k2];
    L.push('  ' + label + '：' + (ds[k2] !== undefined && ds[k2] !== null ? ds[k2] : 0));
  });
  L.push('');
  L.push('【中美联动】');
  L.push('  ' + (a.usChinaRelation || '未给出'));
  L.push('');
  L.push('【风险提示】');
  (a.risks || []).forEach(function (r, i) { L.push('  ' + (i + 1) + '. ' + r); });
  if (!(a.risks || []).length) L.push('  （未给出）');
  L.push('');
  L.push('【情景推演】');
  (a.scenarios || []).forEach(function (s, i) {
    L.push('  ' + (i + 1) + '. ' + (s.scenario || '-') + '｜条件：' + (s.condition || '-') +
      '｜影响：' + (s.impact || '-') + '｜应对：' + (s.action || '-'));
  });
  if (!(a.scenarios || []).length) L.push('  （未给出）');
  L.push('');
  L.push('【数据缺口】');
  (a.dataGaps || []).forEach(function (g, i) { L.push('  ' + (i + 1) + '. ' + g); });
  if (!(a.dataGaps || []).length) L.push('  （无）');
  L.push('');
  L.push('免责声明：' + (a.disclaimer || '本分析仅供参考，不构成投资建议，不承诺收益。'));
  return L.join('\n');
}

module.exports = {
  SCHEMA: SCHEMA,
  SYSTEM_PROMPT: SYSTEM_PROMPT,
  buildMessages: buildMessages,
  renderFactors: renderFactors,
  renderJson: renderJson,
  renderText: renderText
};
