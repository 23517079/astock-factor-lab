'use strict';
/*
 * tests/ui.test.js — 界面冒烟测试
 *
 * 用最小 DOM 桩在 Node 里把 public/analysis.js 真跑一遍，确认：
 *   1. 首屏加载不抛异常
 *   2. 结果渲染函数能吃下真实结构并产出非空 HTML
 *   3. 五个页签、标签页切换不出错
 *   4. 渲染结果里该有的段落都在
 *
 * 为什么要有这个：界面出错通常表现为白屏，用户看不到任何提示。
 * 这个测试能在不开浏览器的情况下抓到 JS 异常。
 */

var fs = require('fs');
var path = require('path');
var vm = require('vm');

var ROOT = path.join(__dirname, '..');
/* 2026-11-16：页面早已拆分 —— index.html 只剩仪表盘（5 个 id），
   真正承载这套 UI 的是 analysis.html（keyPill / pickChips / cfgModel /
   resultArea / view-* 全在里面），对应脚本是 analysis.js。
   旧桩读 index.html 却跑 app.js，四个 id 全 null，结果渲染段在
   `area.innerHTML` 处 TypeError 直接没执行 —— 测试红得很响，
   却一条产品断言都没跑。这里按真实宿主页面对齐，不造元素。 */
var APP = fs.readFileSync(path.join(ROOT, 'public', 'analysis.js'), 'utf8');
var HTML = fs.readFileSync(path.join(ROOT, 'public', 'analysis.html'), 'utf8');

var t = 0, fails = 0;
function ok(name, fn) {
  t++;
  try { fn(); console.log('  ✓ ' + name); }
  catch (e) { fails++; console.log('  ✗ ' + name + ' → ' + e.message); }
}
function section(s) { console.log('\n' + s); }

/* ---------------- 最小 DOM 桩 ---------------- */

function makeEl(id) {
  var el = {
    id: id || '',
    value: '',
    textContent: '',
    innerHTML: '',
    placeholder: '',
    className: '',
    disabled: false,
    dataset: {},
    style: {},
    children: [],
    scrollTop: 0,
    scrollHeight: 0,
    classList: {
      _s: {},
      add: function (c) { this._s[c] = 1; },
      remove: function (c) { delete this._s[c]; },
      toggle: function (c, on) { if (on === undefined) { if (this._s[c]) delete this._s[c]; else this._s[c] = 1; } else if (on) this._s[c] = 1; else delete this._s[c]; },
      contains: function (c) { return !!this._s[c]; }
    },
    appendChild: function (c) { el.children.push(c); return c; },
    addEventListener: function () { },
    removeEventListener: function () { },
    querySelectorAll: function () { return []; },
    querySelector: function () { return null; },
    getAttribute: function () { return null; },
    setAttribute: function () { },
    focus: function () { },
    click: function () { }
  };
  return el;
}

var registry = {};
var errors = [];

function buildSandbox(opts) {
  opts = opts || {};

  /* 从真实 HTML 里把所有 id 预建好，避免 $('xxx') 返回 null */
  var idRe = /id="([^"]+)"/g, m;
  while ((m = idRe.exec(HTML))) {
    registry[m[1]] = makeEl(m[1]);
  }

  var allTabs = [];
  var tabRe = /data-view="([a-z]+)"/g;
  while ((m = tabRe.exec(HTML))) { var te = makeEl(); te.dataset.view = m[1]; allTabs.push(te); }

  var allViews = Object.keys(registry).filter(function (k) { return k.indexOf('view-') === 0; })
    .map(function (k) { return registry[k]; });

  var document = {
    getElementById: function (id) { return registry[id] || null; },
    querySelectorAll: function (sel) {
      if (sel === '.tab') return allTabs;
      if (sel === '.view') return allViews;
      return [];
    },
    querySelector: function () { return null; },
    createElement: function () { return makeEl(); },
    createTextNode: function (txt) { return { nodeValue: txt }; },
    addEventListener: function () { }
  };

  var fetchCalls = [];
  function fakeFetch(url, o) {
    fetchCalls.push({ url: url, opts: o });
    var body;
    if (url.indexOf('/api/config') === 0) {
      body = opts && opts.__configResponse ? opts.__configResponse : {
        llm: { model: 'test-model', baseUrl: 'https://x/v1', baseUrlRaw: '', inferredBaseUrl: true,
          timeoutMs: 90000, maxRetries: 2, temperature: 0.2, maxTokens: 4000, jsonMode: true, apiKeyHeader: 'Authorization' },
        key: { ready: true, source: '环境变量 TEST_KEY', masked: 'sk-****abcd', inFile: '', savedLocally: false, hint: '', envAliases: ['A', 'B'] },
        analysis: { codes: ['688981', '159206'], klineLimit: 120, concurrency: 2, extraContext: '' },
        news: { keywords: {} },
        paths: { config: 'C:/x/config.json', dataDir: 'C:/x', historyDir: 'C:/x/h' },
        defaultModelHints: []
      };
    } else if (url.indexOf('/api/latest') === 0) {
      body = { ok: true, payload: opts && opts.__payload !== undefined ? opts.__payload : samplePayload() };
    } else if (url.indexOf('/api/history') === 0) {
      body = { ok: true, items: [{ file: 'run-x.json', size: 100, mtime: 1, head: { generatedAtText: '2026/11/13 10:00', model: 'm', summary: { total: 1, verdicts: { 观望: 1 } }, names: ['测试(000001)'] } }] };
    } else if (url.indexOf('/api/lookup') === 0) {
      body = { ok: true, code: '688981', name: '中芯国际', price: 110.5, changePct: -1.09, asOf: 'x', source: 's' };
    } else {
      body = { ok: true };
    }
    return Promise.resolve({
      ok: true,
      json: function () { return Promise.resolve(body); }
    });
  }

  var sandbox = {
    console: console,
    document: document,
    location: { hash: '', href: '', search: '' },
    history: { replaceState: function () { } },
    window: { location: { href: '' }, scrollTo: function () { } },
    fetch: fakeFetch,
    EventSource: function (u) { this.url = u; this.close = function () { }; },
    setInterval: function () { return 0; },
    clearInterval: function () { },
    setTimeout: function (fn) { return 0; },
    clearTimeout: function () { },
    Number: Number,
    Math: Math,
    Date: Date,
    JSON: JSON,
    Object: Object,
    Array: Array,
    String: String,
    isFinite: isFinite,
    encodeURIComponent: encodeURIComponent,
    decodeURIComponent: decodeURIComponent,
    parseInt: parseInt,
    parseFloat: parseFloat,
    URLSearchParams: URLSearchParams
  };
  sandbox.globalThis = sandbox;
  return { sandbox: sandbox, document: document, fetchCalls: fetchCalls, tabs: allTabs, views: allViews };
}

/* ---------------- 样例数据 ---------------- */

function samplePayload() {
  return {
    generatedAt: '2026-11-13T09:00:00.000Z',
    generatedAtText: '2026/11/13 17:00:00',
    tool: 'astock-analyst',
    model: { name: 'test-model', baseUrl: 'https://x/v1', keySource: '环境变量', mode: 'llm' },
    marketContext: { usIndices: [], usAdr: [], usLeaders: [], macro: {} },
    summary: { total: 2, succeeded: 2, degraded: 0, verdicts: { 加仓: 1, 观望: 1 } },
    results: [
      {
        symbol: '688981', name: '中芯国际', ok: true, mode: 'llm', elapsedMs: 2100,
        llm: { model: 'test-model', attempts: 1, parseLevel: 'L1-direct' },
        analysis: {
          symbol: '688981', name: '中芯国际',
          dataAsOf: '2026-11-13 15:34:59', dataCoverage: '5/5 个维度可用',
          verdict: '加仓', conviction: 62,
          positionAdvice: '小步加仓，单次不超过计划仓位的 1/3',
          keyLevels: { stopLoss: '94.8', takeProfit: '116.28', support: '94.8', resistance: '116.28' },
          triggers: [{ condition: '放量站上 20 日压力位', value: '116.28 元' }],
          invalidations: [{ condition: '日收盘跌破 20 日支撑位', value: '94.8 元' }],
          dimensionScores: { fundamental: 30, capital: 15, technical: 40, sentiment: -5, macro: 0 },
          evidenceChain: [
            { dimension: '技术面', factor: '趋势判定', observation: '2026-11-13 收盘 110.5 元，R²=0.604', implication: '趋势为「上行」' },
            { dimension: '中美联动', factor: '背离', observation: '美股科技 +0.87% 而该股 -1.09%', implication: '不宜按美股外推' }
          ],
          usChinaRelation: '[背离] 判定为背离：美股科技当日 0.87% 而该股 -1.09%',
          risks: ['融资余额 5 日上升 4.16%'],
          scenarios: [{ scenario: '突破延续', condition: '放量站上 116.28', impact: '打开空间', action: '不追高' }],
          dataGaps: [],
          disclaimer: '仅供个人投资纪律参考，不构成投资建议。'
        },
        factors: {
          coverage: { covered: 5, total: 5, missing: [], notApplicable: [], notApplicableDims: [] },
          quote: { price: 110.5, changePct: -1.09 },
          dims: [
            { key: 'fundamental', label: '基本面', available: true, missing: [], items: [
              { factor: '最新报告期业绩', value: '2026-09-30', detail: '营收 606.18 亿元', asOf: '2026-09-30', source: '东方财富' }
            ] },
            { key: 'technical', label: '技术面', available: true, missing: [], items: [], technical: {
              trend: '上行', trendBasis: ['近 20 日斜率 0.6478'], pattern: '量价平稳', patternNote: '量能 1.01 倍',
              ma5: 109.66, ma20: 104.599, support20: 94.8, resistance20: 116.28,
              denseZone: { lower: 107.4, upper: 113.844, sharePct: 45.3 },
              ret5: -0.36, ret20: 7.64, ret60: -14.63, volatility: 45.48,
              bars: 120, source: '新浪日K', asOf: '2026-11-13',
              indicatorTrend: '指标偏多 4/5',
              indicators: { ok: true, signals: [
                { factor: 'MACD', value: '金叉（偏多）', detail: 'DIF 1.2 / DEA 1.0 / 柱 0.4（最近交叉：金叉）' },
                { factor: 'KDJ', value: '超买（K≥80）', detail: 'K 82.1 / D 75.3 / J 96.6' },
                { factor: 'RSI', value: '中性', detail: 'RSI6 58 / RSI12 55 / RSI24 52' }
              ] }
            } },
            { key: 'sentiment', label: '情绪面', available: true, missing: [], items: [
              { factor: '个股新闻情绪', value: '偏负面（-1）', detail: '负面词命中 1 类', asOf: '2026-11-13', source: '东方财富资讯' }
            ], newsKeyword: '中芯国际' }
          ],
          usChinaLink: {
            relations: [{ type: '背离', confidence: '中', text: '判定为背离：美股科技当日 0.87% 而该股 -1.09%，方向相反。' }],
            channels: [{ name: '情绪传导', text: '隔夜纳指方向最易在次日开盘反映。' }],
            items: []
          }
        }
      },
      {
        /* 降级样本：故意不给 analysis 的若干字段，考验容错 */
        symbol: '159206', name: '卫星ETF', ok: false, mode: 'degraded', elapsedMs: 900,
        llm: { model: 'test-model', attempts: 2, error: '模型调用失败：网络超时' },
        analysis: {
          symbol: '159206', name: '卫星ETF',
          dataAsOf: '2026-11-13 15:34:59', dataCoverage: '4/5 个维度可用（其中 基本面 维度不适用于该标的）',
          verdict: '观望', conviction: 30,
          positionAdvice: '维持当前仓位',
          keyLevels: { stopLoss: '', takeProfit: '', support: '1.098', resistance: '1.248' },
          triggers: [], invalidations: [], dimensionScores: {},
          evidenceChain: [], usChinaRelation: '', risks: [], scenarios: [], dataGaps: [],
          disclaimer: '仅供个人纪律参考。',
          _degraded: true, _degradedReason: '模型调用失败：网络超时'
        },
        factors: {
          coverage: {
            covered: 4, total: 5, missing: [],
            notApplicable: ['业绩报表（ETF 无财务报表）', '估值 PE/PB（ETF 不适用）'],
            notApplicableDims: ['基本面']
          },
          quote: null,
          dims: [], usChinaLink: null
        }
      }
    ]
  };
}

/* ---------------- 测试 ---------------- */

async function run() {
  section('[1] 首屏加载');

  var env = buildSandbox();
  var ctx = vm.createContext(env.sandbox);

  var loadErr = null;
  try {
    vm.runInContext(APP, ctx, { filename: 'analysis.js' });
  } catch (e) { loadErr = e; }

  ok('analysis.js 在 DOM 桩中不抛异常', function () {
    if (loadErr) throw new Error(loadErr.message);
  });

  /* 等首屏的 Promise 链跑完 */
  await new Promise(function (r) { setTimeout(r, 60); });

  ok('配置加载后顶栏显示「模型已就绪」', function () {
    var pill = env.document.getElementById('keyPill');
    if (String(pill.innerHTML).indexOf('已就绪') < 0) throw new Error('顶栏状态未更新：' + pill.innerHTML);
  });

  ok('标的胶囊已渲染', function () {
    var chips = env.document.getElementById('pickChips');
    if (String(chips.innerHTML).indexOf('688981') < 0) throw new Error('胶囊未渲染');
  });

  ok('设置页字段已填充', function () {
    if (env.document.getElementById('cfgModel').value !== 'test-model') throw new Error('模型名未填充');
  });

  section('[2] 结果渲染');

  var area = env.document.getElementById('resultArea');
  var html = String(area.innerHTML);

  ok('结果区非空', function () {
    if (!html || html.length < 500) throw new Error('结果区内容过少：' + html.length);
  });

  ok('两只标的都出现在结果里', function () {
    if (html.indexOf('中芯国际') < 0) throw new Error('缺中芯国际');
    if (html.indexOf('卫星ETF') < 0) throw new Error('缺卫星ETF');
  });

  ok('每只标的都有方向标签', function () {
    if (html.indexOf('v-加仓') < 0) throw new Error('缺加仓标签');
    if (html.indexOf('v-观望') < 0) throw new Error('缺观望标签');
  });

  ok('关键价位区渲染出具体数字', function () {
    if (html.indexOf('94.8') < 0) throw new Error('缺止损价');
    if (html.indexOf('116.28') < 0) throw new Error('缺止盈价');
  });

  ok('依据链条渲染了观测与指向', function () {
    if (html.indexOf('趋势判定') < 0) throw new Error('缺依据条目');
    if (html.indexOf('R²=0.604') < 0) throw new Error('依据未带数值');
  });

  ok('中美联动渲染了判定词', function () {
    if (html.indexOf('背离') < 0) throw new Error('缺联动判定');
  });

  ok('技术面渲染了指标共振与指标明细表', function () {
    if (html.indexOf('指标共振') < 0) throw new Error('缺指标共振行');
    if (html.indexOf('技术指标明细') < 0) throw new Error('缺技术指标明细块');
    if (html.indexOf('MACD') < 0) throw new Error('缺 MACD 信号');
    if (html.indexOf('KDJ') < 0) throw new Error('缺 KDJ 信号');
  });

  ok('五维打分渲染了负分', function () {
    if (html.indexOf('-5') < 0) throw new Error('负分未渲染');
  });

  ok('降级标的显示了警示', function () {
    if (html.indexOf('这不是模型结论') < 0) throw new Error('降级警示缺失');
  });

  ok('不适用项被单独标出，不与缺失混淆', function () {
    if (html.indexOf('不适用项') < 0) throw new Error('不适用区块缺失');
    if (html.indexOf('不是数据缺失') < 0) throw new Error('缺少语义说明');
  });

  ok('无缺失时显示「没有真实数据缺口」', function () {
    if (html.indexOf('没有真实数据缺口') < 0) throw new Error('缺空态文案');
  });

  ok('免责声明在结果里', function () {
    if (html.indexOf('不构成投资建议') < 0) throw new Error('缺免责声明');
  });

  section('[3] 页签与交互');

  ok('页签数量与 HTML 一致（5 个）', function () {
    if (env.tabs.length !== 5) throw new Error('页签数 ' + env.tabs.length);
  });

  ok('切换页签不抛异常', function () {
    ['analyze', 'watch', 'settings', 'history', 'doc'].forEach(function (v) {
      ctx.go(v);
    });
  });

  ok('切到记录页会触发历史请求', function () {
    ctx.go('history');
    var hit = env.fetchCalls.some(function (c) { return c.url.indexOf('/api/history') === 0; });
    if (!hit) throw new Error('未请求 /api/history');
  });

  section('[4] 空数据容错');

  var env2 = buildSandbox({ __payload: null });
  /* 让 latest 返回空 payload */
  env2.sandbox.fetch = function (url, o) {
    if (url.indexOf('/api/latest') === 0) {
      return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ ok: true, payload: null }); } });
    }
    return Promise.resolve({ ok: true, json: function () { return Promise.resolve({ ok: true, payload: null }); } });
  };
  var ctx2 = vm.createContext(env2.sandbox);
  var err2 = null;
  try { vm.runInContext(APP, ctx2, { filename: 'analysis.js' }); } catch (e) { err2 = e; }
  await new Promise(function (r) { setTimeout(r, 60); });

  ok('没有历史结果时显示空态而不是报错', function () {
    if (err2) throw new Error(err2.message);
    var a = env2.document.getElementById('resultArea');
    if (String(a.innerHTML).indexOf('还没有分析结果') < 0) throw new Error('空态缺失：' + a.innerHTML.slice(0, 120));
  });

  console.log('\n结果：' + (t - fails) + '/' + t + ' 通过');
  if (fails) process.exit(1);
}

run().catch(function (e) {
  console.log('✗ 测试本身出错：' + e.message);
  console.log(e.stack);
  process.exit(1);
});
