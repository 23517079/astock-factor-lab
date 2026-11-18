'use strict';
/*
 * runner.js — 分析流程编排（命令行与界面共用）
 *
 * 把「取数 → 算因子 → 调模型 → 解析 → 拼产物」这条链路收在一个地方，
 * cli.js 与 server.js 都调它，避免两套逻辑走偏。
 *
 * runBatch 支持 onEvent 回调，界面用它做实时进度推送。
 */

var S = require('./sources.js');
var E = require('./engine.js');
var LLM = require('./llm.js');
var P = require('./parse.js');
var PR = require('./prompt.js');

/* ---------------- 资讯检索 ---------------- */

async function searchNewsByKeyword(keyword) {
  var param = {
    uid: '', keyword: keyword, type: ['cmsArticleWebOld'], client: 'web', clientType: 'web',
    clientVersion: 'curr',
    param: { cmsArticleWebOld: { searchScope: 'default', sort: 'time', pageIndex: 1, pageSize: 8, preTag: '', postTag: '' } }
  };
  var url = 'https://search-api-web.eastmoney.com/search/jsonp?cb=cb&param=' + encodeURIComponent(JSON.stringify(param));
  var r = await S.raw(url, { referer: 'https://so.eastmoney.com/', timeoutMs: 12000 });
  if (!r.ok) return { ok: false, items: [], error: '资讯 HTTP ' + r.status, keyword: keyword };
  try {
    var j = S.jsonp(r.text);
    if (!j || !j.ok) return { ok: false, items: [], error: '资讯解析失败', keyword: keyword };
    var arr = (j.value && j.value.result && j.value.result.cmsArticleWebOld) || [];
    return {
      ok: true, keyword: keyword,
      items: arr.map(function (x) {
        return { title: x.title, media: x.mediaName, date: x.date, url: x.url };
      })
    };
  } catch (e) {
    return { ok: false, items: [], error: '资讯异常：' + e.message, keyword: keyword };
  }
}

/*
 * 多关键词回退：代码专属词 → 名称专属词 → 通用词 → 标的简称。
 * 第一个有结果的即采用；全部失败才返回空，并说明试过哪些词。
 */
async function fetchNews(code, name, cfg) {
  var kwCfg = (cfg.news && cfg.news.keywords) || {};
  var lists = [];
  function push(v) { if (v) lists.push(v); }
  push(kwCfg[code]);
  push(kwCfg[name]);
  push(kwCfg.default);
  push(name || code);

  var tried = [];
  var lastError = '';
  for (var i = 0; i < lists.length; i++) {
    var kws = Array.isArray(lists[i]) ? lists[i] : [lists[i]];
    for (var j = 0; j < kws.length; j++) {
      var kw = String(kws[j] || '').trim();
      if (!kw || tried.indexOf(kw) >= 0) continue;
      tried.push(kw);
      var res = await searchNewsByKeyword(kw);
      if (res.ok && res.items.length) {
        return {
          items: res.items,
          source: '东方财富资讯搜索（关键词：' + kw + '）',
          keyword: kw, triedKeywords: tried
        };
      }
      if (!res.ok) lastError = res.error;
    }
  }
  return {
    items: [], error: lastError || ('全部关键词无结果：' + tried.join('、')),
    keyword: tried[0] || '', triedKeywords: tried
  };
}

/* ---------------- 规则降级 ---------------- */

/*
 * 不依赖模型的保底结论：直接用客观因子拼一份固定字段的分析。
 * 立场保守 —— 技术面、资金面、基本面三者同向才给加仓/减仓，否则观望。
 */
function buildRuleFallback(analysis) {
  var tech = null, cap = null, fund = null;
  (analysis.dims || []).forEach(function (d) {
    if (d.key === 'technical') tech = d.data;
    if (d.key === 'capital') cap = d.data;
    if (d.key === 'fundamental') fund = d.data;
  });

  var out = {
    symbol: analysis.code, name: analysis.name,
    dataAsOf: (analysis.quote && analysis.quote.asOf) || analysis.collectedAt,
    dataCoverage: analysis.coverage.covered + '/' + analysis.coverage.total + ' 个维度可用',
    verdict: '观望', conviction: 0,
    positionAdvice: '维持现状，不因本次数据做任何加仓动作',
    keyLevels: { stopLoss: '', takeProfit: '', support: '', resistance: '' },
    triggers: [], invalidations: [], evidenceChain: [], risks: [], scenarios: [], dataGaps: [],
    dimensionScores: { fundamental: 0, capital: 0, technical: 0, sentiment: 0, macro: 0 },
    usChinaRelation: '', disclaimer: '本分析基于公开数据与量化因子，仅供个人投资纪律参考，不构成投资建议，不承诺收益。'
  };

  if (tech && tech.available) {
    out.dimensionScores.technical = tech.trend === '上行' ? 40 : (tech.trend === '下行' ? -40 : 0);
    out.evidenceChain.push({
      dimension: '技术面', factor: '趋势判定',
      observation: tech.date + ' 收盘 ' + tech.price + ' 元，' + (tech.trendBasis || []).join('；'),
      implication: '趋势为「' + tech.trend + '」，' + (tech.trend === '上行' ? '回踩不破前低可继续持有' : '暂不构成加仓依据')
    });
    out.keyLevels.support = tech.support20 !== null && tech.support20 !== undefined ? String(tech.support20) : '';
    out.keyLevels.resistance = tech.resistance20 !== null && tech.resistance20 !== undefined ? String(tech.resistance20) : '';
    if (tech.support20) {
      out.keyLevels.stopLoss = String(tech.support20);
      out.invalidations.push({ condition: '日收盘跌破 20 日支撑位', value: tech.support20 + ' 元' });
    }
    if (tech.resistance20) {
      out.keyLevels.takeProfit = String(tech.resistance20);
      out.triggers.push({ condition: '放量站上 20 日压力位后可考虑加仓', value: tech.resistance20 + ' 元' });
    }
    if (tech.denseZone) {
      out.evidenceChain.push({
        dimension: '技术面', factor: '成交密集区',
        observation: '近 20 日成交量最集中的价格带 ' + tech.denseZone.lower + '–' + tech.denseZone.upper +
          ' 元，占该区间成交量的 ' + tech.denseZone.sharePct + '%（分档宽度 ' + tech.denseZone.bucketSize + ' 元）',
        implication: '该价格带是主要持仓成本区，价格回到此区间易获得支撑、跌破则抛压集中'
      });
    }
    out.evidenceChain.push({
      dimension: '技术面', factor: '量价形态',
      observation: tech.pattern + '（' + tech.patternNote + '）',
      implication: tech.pattern === '放量上涨' ? '资金配合向好' : (tech.pattern === '放量下跌' ? '抛压明显' : '量价信号不明确')
    });
  }

  if (cap && cap.available) {
    (cap.items || []).forEach(function (it) {
      out.evidenceChain.push({
        dimension: '资金面', factor: it.factor,
        observation: it.detail + '（时间 ' + (it.asOf || '未取得') + '，来源 ' + it.source + '）',
        implication: '资金面客观事实，需与价格趋势交叉验证'
      });
    });
    var mch = cap.marginChange5dPct;
    if (E.isNum(mch)) {
      out.dimensionScores.capital = mch > 0 ? 15 : -15;
      if (mch > 0) out.risks.push('融资余额 5 日上升 ' + mch + '%，杠杆资金增加；若股价反向下行存在被动减仓压力');
    }
    if (E.isNum(cap.northChangePct)) {
      out.dimensionScores.capital += cap.northChangePct > 0 ? 15 : -15;
    }
  }

  if (fund && fund.available) {
    (fund.items || []).forEach(function (it) {
      out.evidenceChain.push({
        dimension: '基本面', factor: it.factor,
        observation: it.detail + '（时间 ' + (it.asOf || '未取得') + '，来源 ' + it.source + '）',
        implication: '基本面客观事实'
      });
    });
    if (E.isNum(fund.netProfitYoy)) {
      out.dimensionScores.fundamental = fund.netProfitYoy > 20 ? 30 : (fund.netProfitYoy < 0 ? -30 : 0);
    }
  }

  var link = analysis.usChinaLink || {};
  (link.relations || []).forEach(function (r) {
    out.usChinaRelation = '[' + r.type + '] ' + r.text;
    out.evidenceChain.push({
      dimension: '中美联动', factor: r.type,
      observation: r.text,
      implication: '隔夜外盘方向对次日开盘有情绪影响，但需结合本土资金确认'
    });
  });

  out.dataGaps = (analysis.coverage.missing || []).slice();
  out.dataGaps.push('本次为规则降级结果（未调用大模型或模型输出不合法），结论未经模型综合推理');
  out.risks.push('数据源为公开接口，非交易所直连；盘后数据为当日收盘快照');
  out.scenarios.push({
    scenario: '数据缺口补齐后重新评估',
    condition: '当前缺失维度恢复取数后重跑分析',
    impact: '结论可能改变',
    action: '不要依据本次降级结果做仓位调整'
  });

  var t = out.dimensionScores.technical, c = out.dimensionScores.capital, f = out.dimensionScores.fundamental;
  if (t > 0 && c > 0 && f > 0) {
    out.verdict = '加仓'; out.conviction = 55; out.positionAdvice = '小步加仓，单次不超过计划仓位的 1/3';
  } else if (t < 0 && c < 0 && f < 0) {
    out.verdict = '减仓'; out.conviction = 55; out.positionAdvice = '降低至风险预算允许的水平，不清仓';
  } else {
    out.verdict = '观望'; out.conviction = 30; out.positionAdvice = '维持当前仓位，等待趋势与资金同向再动';
  }
  return out;
}

/* ---------------- 单只标的 ---------------- */

function coverageText(cov) {
  return cov.covered + '/' + cov.total + ' 个维度可用' +
    (cov.notApplicableDims && cov.notApplicableDims.length
      ? '（其中 ' + cov.notApplicableDims.join('、') + ' 维度不适用于该标的）' : '') +
    (cov.missing.length ? '；缺失：' + cov.missing.join('；') : '') +
    (cov.notApplicable && cov.notApplicable.length
      ? '；不适用（该标的不具备，非数据缺失）：' + cov.notApplicable.join('；') : '');
}

async function analyzeOne(code, ctx) {
  var cfg = ctx.cfg;
  var t0 = Date.now();

  var pack = await S.collectStock(code, { klineLimit: cfg.analysis.klineLimit });
  var news = await fetchNews(code, pack.name, cfg);
  var analysis = E.buildAnalysis(pack, ctx.marketCtx, news);

  var base = {
    symbol: code, name: pack.name,
    dataAsOf: (pack.quote && pack.quote.asOf) || analysis.collectedAt,
    dataCoverage: coverageText(analysis.coverage),
    dimensionScores: { fundamental: 0, capital: 0, technical: 0, sentiment: 0, macro: 0 },
    keyLevels: { stopLoss: '', takeProfit: '', support: '', resistance: '' },
    triggers: [], invalidations: [], evidenceChain: [], risks: [], scenarios: [], dataGaps: [],
    usChinaRelation: '',
    disclaimer: '本分析基于公开数据与量化因子，仅供个人投资纪律参考，不构成投资建议，不承诺收益。市场有风险，决策与后果由投资者自行承担。'
  };

  if (ctx.dryRun) {
    return {
      code: code, name: pack.name, ok: true, mode: 'dry-run',
      promptPreview: PR.renderFactors(analysis, { extraContext: cfg.analysis.extraContext }),
      analysis: null, factors: analysis, elapsedMs: Date.now() - t0
    };
  }

  if (ctx.noLlm) {
    var fb0 = buildRuleFallback(analysis);
    fb0._degraded = true;
    fb0._degradedReason = '未调用大模型：结论由本机规则基于客观因子生成';
    fb0.dataGaps = (fb0.dataGaps || []).concat([fb0._degradedReason]);
    return {
      code: code, name: pack.name, ok: true, mode: 'no-llm',
      analysis: fb0, factors: analysis, elapsedMs: Date.now() - t0
    };
  }

  var messages = PR.buildMessages(analysis, { extraContext: cfg.analysis.extraContext });
  var call = await LLM.chat(cfg.llm, messages, {
    onAttempt: function (a) {
      if (ctx.onEvent) {
        ctx.onEvent({ type: 'attempt', code: code, attempt: a.attempt, ok: a.ok, error: a.error, elapsedMs: a.elapsedMs });
      }
    }
  });

  var parsed = null, level = null, parseErrors = [];
  if (call.ok) {
    parsed = P.parseModelOutput(call.content, PR.SCHEMA.analysis);
    level = parsed.level;
    parseErrors = parsed.errors || [];
  }

  if (parsed && parsed.ok) {
    var v = parsed.value;
    v.symbol = code; v.name = pack.name;
    v.dataAsOf = base.dataAsOf; v.dataCoverage = base.dataCoverage;
    if (!v.disclaimer) v.disclaimer = base.disclaimer;
    if (!v.dataGaps || !v.dataGaps.length) v.dataGaps = analysis.coverage.missing.slice();
    return {
      code: code, name: pack.name, ok: true, mode: 'llm',
      analysis: v, factors: analysis,
      llm: { model: call.model, baseUrl: call.baseUrl, keySource: call.keySource,
        attempts: call.attempts, parseLevel: level, usage: call.usage },
      parseErrors: parseErrors, elapsedMs: Date.now() - t0
    };
  }

  var reason = call.ok
    ? ('模型输出不合法（解析级别：' + level + '）：' + (parseErrors[0] || '未知'))
    : ('模型调用失败：' + call.error);
  var fb = buildRuleFallback(analysis);
  fb._degraded = true;
  fb._degradedReason = reason;
  fb.dataGaps = (fb.dataGaps || []).concat([reason]);
  return {
    code: code, name: pack.name, ok: false, mode: 'degraded',
    analysis: fb, factors: analysis,
    llm: { model: call.model, baseUrl: call.baseUrl, keySource: call.keySource || '未配置',
      attempts: call.attempts, parseLevel: level, error: call.error },
    parseErrors: parseErrors, elapsedMs: Date.now() - t0
  };
}

/* ---------------- 并发池 ---------------- */

async function runPool(items, limit, worker) {
  var results = [];
  var idx = 0;
  async function runner() {
    while (idx < items.length) {
      var i = idx++;
      results[i] = await worker(items[i], i);
    }
  }
  var workers = [];
  for (var i = 0; i < Math.min(limit || 1, items.length); i++) workers.push(runner());
  await Promise.all(workers);
  return results;
}

/* ---------------- 全局上下文 ---------------- */

async function prepareMarketCtx(cfg, onEvent) {
  var [us, usTencent, macro] = await Promise.all([
    S.fetchUsMarket(cfg.us.symbols).catch(function () { return { indices: [], adr: [], errors: ['美股数据获取异常'] }; }),
    S.fetchUsMarketTencent(cfg.us.tencentLeaders).catch(function () { return { indices: [], leaders: [], errors: ['美股龙头获取异常'] }; }),
    S.fetchMacro().catch(function () { return { errors: ['宏观获取异常'] }; })
  ]);
  var ctx = { us: us, usTencent: usTencent, macro: macro };
  if (onEvent) {
    onEvent({
      type: 'market',
      usIndices: (us.indices || []).map(function (x) { return { name: x.name, changePct: x.changePct, price: x.price }; }),
      adr: (us.adr || []).map(function (x) { return { name: x.name, changePct: x.changePct }; }),
      macro: { usdcny: macro.usdcny || null, oil: macro.oil || null, gold: macro.gold || null }
    });
  }
  return ctx;
}

/* ---------------- 批次 ---------------- */

/*
 * runBatch — 跑一批标的，返回与落盘结构一致的 payload。
 * opts: { noLlm, dryRun, onEvent }
 */
async function runBatch(codes, cfg, opts) {
  opts = opts || {};
  var onEvent = opts.onEvent || function () {};

  onEvent({ type: 'start', codes: codes });
  var marketCtx = await prepareMarketCtx(cfg, onEvent);

  var ctx = { cfg: cfg, marketCtx: marketCtx, noLlm: !!opts.noLlm, dryRun: !!opts.dryRun, onEvent: onEvent };

  var results = await runPool(codes, cfg.analysis.concurrency, async function (code, i) {
    onEvent({ type: 'item-start', code: code, index: i + 1, total: codes.length });
    var r = await analyzeOne(code, ctx);
    onEvent({
      type: 'item-done', code: code, index: i + 1, total: codes.length,
      name: r.name, ok: r.ok, mode: r.mode,
      verdict: r.analysis && r.analysis.verdict,
      conviction: r.analysis && r.analysis.conviction,
      coverage: r.factors && r.factors.coverage,
      reason: r.analysis && r.analysis._degradedReason,
      elapsedMs: r.elapsedMs
    });
    return r;
  });

  var payload = {
    generatedAt: new Date().toISOString(),
    generatedAtText: S.nowBj(),
    tool: 'astock-analyst',
    model: {
      name: LLM.resolveModel(cfg.llm),
      baseUrl: LLM.resolveBaseUrl(cfg.llm),
      keySource: LLM.resolveApiKey(cfg.llm).source,
      mode: opts.noLlm ? 'no-llm' : (opts.dryRun ? 'dry-run' : 'llm')
    },
    marketContext: {
      usIndices: marketCtx.us.indices || [],
      usAdr: marketCtx.us.adr || [],
      usLeaders: (marketCtx.usTencent.leaders || []).slice(0, 8),
      macro: marketCtx.macro
    },
    summary: {
      total: results.length,
      succeeded: results.filter(function (r) { return r.ok; }).length,
      degraded: results.filter(function (r) { return !r.ok; }).length,
      verdicts: results.reduce(function (m, r) {
        var v = r.analysis && r.analysis.verdict; if (v) m[v] = (m[v] || 0) + 1; return m;
      }, {})
    },
    results: results.map(serializeResult)
  };

  onEvent({ type: 'done', summary: payload.summary, payload: payload });
  return payload;
}

/* 单只结果 → 落盘结构（因子只保留界面/下游需要的部分，避免文件过大） */
function serializeResult(r) {
  return {
    symbol: r.code, name: r.name, ok: r.ok, mode: r.mode, elapsedMs: r.elapsedMs,
    llm: r.llm || null, parseErrors: r.parseErrors || null,
    analysis: r.analysis || null,
    promptPreview: r.promptPreview || null,
    factors: r.factors ? {
      coverage: r.factors.coverage,
      quote: r.factors.quote || null,
      dims: (r.factors.dims || []).map(function (d) {
        return {
          key: d.key, label: d.label,
          available: !!(d.data && d.data.available),
          missing: (d.data && d.data.missing) || [],
          items: (d.data && d.data.items) || [],
          newsKeyword: d.key === 'sentiment' && d.data ? (d.data.newsKeyword || null) : null,
          headlines: d.key === 'sentiment' && d.data ? (d.data.headlines || null) : null,
          technical: d.key === 'technical' && d.data ? {
            trend: d.data.trend, trendBasis: d.data.trendBasis, pattern: d.data.pattern,
            patternNote: d.data.patternNote, ma5: d.data.ma5, ma10: d.data.ma10,
            ma20: d.data.ma20, ma60: d.data.ma60,
            support20: d.data.support20, resistance20: d.data.resistance20,
            support60: d.data.support60, resistance60: d.data.resistance60,
            denseZone: d.data.denseZone, ret5: d.data.ret5, ret20: d.data.ret20,
            ret60: d.data.ret60, volatility: d.data.volatilityAnnualPct,
            bars: d.data.bars, source: d.data.source, asOf: d.data.asOf,
            date: d.data.date, price: d.data.price,
            indicators: d.data.indicators || null,
            indicatorTrend: d.data.indicatorTrend || null
          } : null
        };
      }),
      usChinaLink: r.factors.usChinaLink ? {
        relations: r.factors.usChinaLink.relations,
        channels: r.factors.usChinaLink.channels,
        items: r.factors.usChinaLink.items
      } : null
    } : null
  };
}

module.exports = {
  analyzeOne: analyzeOne,
  buildRuleFallback: buildRuleFallback,
  runPool: runPool,
  fetchNews: fetchNews,
  searchNewsByKeyword: searchNewsByKeyword,
  prepareMarketCtx: prepareMarketCtx,
  runBatch: runBatch,
  coverageText: coverageText
};
