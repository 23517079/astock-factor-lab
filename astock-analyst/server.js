#!/usr/bin/env node
'use strict';
/*
 * server.js — 本地服务：界面 + REST API + SSE 实时进度
 *
 * 零第三方依赖，只用 Node 内置模块。
 * 启动后打开 http://127.0.0.1:8790 即可使用界面。
 *
 * 设计要点：
 *   - 配置存 data/config.json，界面里改、服务端落盘
 *   - 密钥只在服务端解析，接口只回「是否已配置 + 来源」，绝不回传密钥原文
 *   - 分析走 SSE 推送，前端能看到逐只进度，不用干等
 *   - 同一时间只允许一个分析任务，避免重复点把上游打到限流
 */

var http = require('http');
var fs = require('fs');
var path = require('path');
var url = require('url');

var R = require('./lib/runner.js');
var LLM = require('./lib/llm.js');
var CFG = require('./lib/config.js');
var PR = require('./lib/prompt.js');
var S = require('./lib/sources.js');
var PF = require('./lib/portfolio.js');
var LK = require('./lib/linkage.js');
var CT = require('./lib/contract.js');   /* L1 统一状态契约 */
var RC = require('./lib/reconcile.js');  /* L2 字段级第二源对账 */
var FA = require('./lib/factors.js');    /* 因子体检数据（Python 侧导出） */
var PK = require('./lib/picks.js');      /* 智能选股推荐（Python 侧合成） */

var ROOT = __dirname;
var PUBLIC = path.join(ROOT, 'public');
var DATA_DIR = process.env.ASTOCK_DATA_DIR || path.join(ROOT, 'data');
var CONFIG_PATH = path.join(DATA_DIR, 'config.json');
var HISTORY_DIR = path.join(DATA_DIR, 'history');
var PORT = parseInt(process.env.ASTOCK_PORT || '8790', 10);

var MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'application/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.ico': 'image/x-icon'
};

/* ---------------- 配置读写 ---------------- */

function ensureDirs() {
  [DATA_DIR, HISTORY_DIR].forEach(function (d) {
    if (!fs.existsSync(d)) fs.mkdirSync(d, { recursive: true });
  });
}

/* 首次启动写入一份默认配置（含 3 个示例标的），让界面一打开就有内容 */
function ensureConfig() {
  ensureDirs();
  if (fs.existsSync(CONFIG_PATH)) return;
  var seed = {
    llm: {
      baseUrl: '', model: 'gpt-4o-mini', apiKey: '', apiKeyHeader: 'Authorization',
      timeoutMs: 90000, maxRetries: 2, retryBaseDelayMs: 1200,
      temperature: 0.2, maxTokens: 4000, jsonMode: true
    },
    analysis: {
      codes: ['688981', '159206', '159227'],
      klineLimit: 120, concurrency: 2, extraContext: ''
    },
    news: {
      keywords: {
        '688981': ['中芯国际', '晶圆代工', '先进制程'],
        '159206': ['卫星互联网', '商业航天'],
        '159227': ['商业航天', '军工']
      }
    },
    sequoiaUrl: 'http://127.0.0.1:8791',
    output: { dir: 'out', jsonFile: 'analysis-results.json', textFile: 'analysis-report.md', saveFactorDump: true }
  };
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(seed, null, 2), 'utf8');
}

function loadCfg() {
  ensureConfig();
  return CFG.loadConfig(CONFIG_PATH);
}

function saveCfgPatch(patch) {
  ensureConfig();
  var raw = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
  CFG.deepMerge(raw, patch);
  fs.writeFileSync(CONFIG_PATH, JSON.stringify(raw, null, 2), 'utf8');
  return raw;
}

/* 密钥打码：只露首尾，避免截屏泄露 */
function maskKey(k) {
  if (!k) return '';
  var s = String(k);
  if (s.length <= 8) return '****';
  return s.slice(0, 4) + '****' + s.slice(-4);
}

/* 对外暴露的配置视图：绝不含密钥原文 */
function configView(cfg) {
  var info = LLM.resolveApiKey(cfg.llm);
  var fileKey = '';
  var fileBase = '';
  var fileModel = '';
  try {
    var raw = JSON.parse(fs.readFileSync(CONFIG_PATH, 'utf8'));
    fileKey = (raw.llm && raw.llm.apiKey) || '';
    fileBase = (raw.llm && raw.llm.baseUrl) || '';
    fileModel = (raw.llm && raw.llm.model) || '';
  } catch (e) { }

  return {
    llm: {
      model: LLM.resolveModel(cfg.llm),
      baseUrl: LLM.resolveBaseUrl(cfg.llm),
      /* baseUrlRaw 是用户在文件里真实填的值（空表示让他自动推断） */
      baseUrlRaw: fileBase,
      inferredBaseUrl: !fileBase,
      modelRaw: fileModel,
      timeoutMs: cfg.llm.timeoutMs,
      maxRetries: cfg.llm.maxRetries,
      retryBaseDelayMs: cfg.llm.retryBaseDelayMs,
      temperature: cfg.llm.temperature,
      maxTokens: cfg.llm.maxTokens,
      jsonMode: cfg.llm.jsonMode,
      apiKeyHeader: cfg.llm.apiKeyHeader || 'Authorization'
    },
    key: {
      ready: !!info.key,
      source: info.source,
      masked: maskKey(info.key),
      inFile: /^\$\{/.test(fileKey || '') ? fileKey : '',
      savedLocally: !!fileKey && !/^\$\{/.test(fileKey),
      hint: info.key ? '' : ('未找到密钥。方式一：设环境变量 ' + LLM.KEY_ENV_ALIASES.slice(0, 3).join(' / ') +
        '；方式二：在下方输入框填写并保存（存在本机 data/config.json）；方式三：填 ${ENV_NAME} 间接引用。'),
      envAliases: LLM.KEY_ENV_ALIASES.slice()
    },
    analysis: {
      codes: cfg.analysis.codes || [],
      klineLimit: cfg.analysis.klineLimit,
      concurrency: cfg.analysis.concurrency,
      extraContext: cfg.analysis.extraContext || ''
    },
    news: { keywords: cfg.news.keywords || {} },
    sequoiaUrl: cfg.sequoiaUrl || 'http://127.0.0.1:8791',
    paths: { config: CONFIG_PATH, dataDir: DATA_DIR, historyDir: HISTORY_DIR },
    defaultModelHints: [
      { name: 'gpt-4o-mini', baseUrl: 'https://api.openai.com/v1' },
      { name: 'deepseek-chat', baseUrl: 'https://api.deepseek.com/v1' },
      { name: 'moonshot-v1-8k', baseUrl: 'https://api.moonshot.cn/v1' },
      { name: 'qwen-plus', baseUrl: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
      { name: 'glm-4-flash', baseUrl: 'https://open.bigmodel.cn/api/paas/v4' }
    ]
  };
}

/* ---------------- 历史记录 ---------------- */

function listHistory() {
  ensureDirs();
  var files = fs.readdirSync(HISTORY_DIR).filter(function (f) { return /\.json$/.test(f); });
  return files.map(function (f) {
    var p = path.join(HISTORY_DIR, f);
    var st = fs.statSync(p);
    var head = {};
    try {
      var j = JSON.parse(fs.readFileSync(p, 'utf8'));
      head = {
        generatedAtText: j.generatedAtText,
        model: j.model && j.model.name,
        summary: j.summary,
        names: (j.results || []).map(function (r) { return r.name + '(' + r.symbol + ')'; })
      };
    } catch (e) { head = { error: '读取失败' }; }
    return { file: f, size: st.size, mtime: st.mtimeMs, head: head };
  }).sort(function (a, b) { return b.mtime - a.mtime; }).slice(0, 30);
}

function readHistory(file) {
  var safe = path.basename(String(file || ''));
  var p = path.join(HISTORY_DIR, safe);
  if (!fs.existsSync(p)) throw new Error('记录不存在：' + safe);
  return JSON.parse(fs.readFileSync(p, 'utf8'));
}

/* ---------------- HTTP 工具 ---------------- */

/**
 * 读取策略引擎最近一次扫描结果，构建 code -> 策略名 的映射，
 * 用于在持仓列表里标注「这只票当前被哪个策略命中」。
 * 只读缓存文件，不触发扫描；缓存缺失或过期就返回空对象。
 */
function loadSignalMap() {
  try {
    var p = path.join(ROOT, '..', 'sequoia-service', 'data', 'scan_results.json');
    if (!fs.existsSync(p)) return {};
    var raw = JSON.parse(fs.readFileSync(p, 'utf8'));
    var map = {};
    (raw.strategies || []).forEach(function (s) {
      (s.picks || []).forEach(function (pk) {
        var code = String(pk.symbol || pk.code || '');
        if (/^\d{6}$/.test(code)) {
          map[code] = map[code] ? map[code] + ' / ' + s.name : s.name;
        }
      });
    });
    return map;
  } catch (e) {
    return {};
  }
}

function sendJson(res, code, obj) {
  var body = JSON.stringify(obj);
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
    'Content-Length': Buffer.byteLength(body)
  });
  res.end(body);
}

function sendText(res, code, text, type) {
  res.writeHead(code, { 'Content-Type': type || 'text/plain; charset=utf-8', 'Cache-Control': 'no-store' });
  res.end(text);
}

function readBody(req) {
  return new Promise(function (resolve, reject) {
    var chunks = [];
    var size = 0;
    req.on('data', function (c) {
      size += c.length;
      if (size > 2 * 1024 * 1024) { reject(new Error('请求体过大')); req.destroy(); return; }
      chunks.push(c);
    });
    req.on('end', function () {
      var raw = Buffer.concat(chunks).toString('utf8');
      if (!raw) return resolve({});
      try { resolve(JSON.parse(raw)); } catch (e) { reject(new Error('请求体不是合法 JSON')); }
    });
    req.on('error', reject);
  });
}

function serveStatic(res, urlPath) {
  var rel = urlPath === '/' ? 'index.html' : urlPath.replace(/^\/+/, '');
  var p = path.join(PUBLIC, rel);
  /* 目录穿越防护 */
  if (p.indexOf(PUBLIC) !== 0) return sendText(res, 403, 'Forbidden');
  if (!fs.existsSync(p) || fs.statSync(p).isDirectory()) return sendText(res, 404, 'Not Found');
  var ext = path.extname(p).toLowerCase();
  res.writeHead(200, {
    'Content-Type': MIME[ext] || 'application/octet-stream',
    'Cache-Control': 'no-cache'
  });
  fs.createReadStream(p).pipe(res);
}

/* ---------------- Sequoia-X 伴侣服务代理 + ETF / 行业 ---------------- */

function proxyUrl(fullUrl, method, bodyObj) {
  return new Promise(function (resolve) {
    var u;
    try { u = url.parse(fullUrl); } catch (e) { return resolve({ ok: false, status: 0, error: 'bad url' }); }
    var lib = u.protocol === 'https:' ? require('https') : http;
    var data = bodyObj ? JSON.stringify(bodyObj) : null;
    var opt = {
      method: method, hostname: u.hostname,
      port: u.port || (u.protocol === 'https:' ? 443 : 80),
      path: u.path,
      headers: { 'Content-Type': 'application/json', 'Accept': 'application/json' }
    };
    if (data) opt.headers['Content-Length'] = Buffer.byteLength(data);
    var r = lib.request(opt, function (resp) {
      var chunks = [];
      resp.on('data', function (c) { chunks.push(c); });
      resp.on('end', function () {
        var text = Buffer.concat(chunks).toString('utf8');
        var json = null; try { json = JSON.parse(text); } catch (e) { }
        resolve({ ok: resp.statusCode < 400, status: resp.statusCode, json: json, text: text });
      });
    });
    r.on('error', function (e) { resolve({ ok: false, status: 0, error: e.message }); });
    if (data) r.write(data);
    r.end();
  });
}

/* 核心宽基 ETF 列表（对应模板图3） */
var ETF_LIST = [
  { code: '510050', name: '华夏上证50ETF', idx: '1' },
  { code: '510300', name: '华泰柏瑞沪深300ETF', idx: '1' },
  { code: '510330', name: '华夏沪深300ETF', idx: '1' },
  { code: '510500', name: '南方中证500ETF', idx: '1' },
  { code: '512100', name: '南方中证1000ETF', idx: '1' },
  { code: '159915', name: '易方达创业板ETF', idx: '0' },
  { code: '510310', name: '易方达沪深300ETF', idx: '1' },
  { code: '159919', name: '嘉实沪深300ETF', idx: '0' }
];

async function fetchEtf() {
  var errors = [];
  var syms = ETF_LIST.map(function (e) { return (e.idx === '1' ? 'sh' : 'sz') + e.code; }).join(',');
  var t = await S.raw('https://qt.gtimg.cn/q=' + syms, { timeoutMs: 12000 });
  var cards = [];
  if (t.ok) {
    t.text.split(/\n/).forEach(function (line) {
      var m = line.match(/v_([a-z0-9]+)="([^"]*)"/i);
      if (!m) return;
      var b = m[2].split('~');
      if (b.length < 37) return;
      var code = b[2];
      var meta = ETF_LIST.find(function (e) { return e.code === code; });
      if (!meta) return;
      var price = parseFloat(b[3]);
      var pct = parseFloat(b[32]);
      var amount = parseFloat(b[37]) * 10000;
      cards.push({
        code: code, name: meta.name,
        price: isNaN(price) ? null : S.r2(price, 3),
        changePct: isNaN(pct) ? null : S.r2(pct, 2),
        amount: isNaN(amount) ? null : amount,
        source: '腾讯行情 qt.gtimg.cn', asOf: b[30] || null,
        shares: null, netSubscribe: null
      });
    });
  } else { errors.push('腾讯ETF行情：' + (t.error || t.status)); }

  /* 尝试东财取 份额 / 净申购（沙箱可能不通，失败则置空，前端显示 —） */
  try {
    var fs2 = ETF_LIST.map(function (e) { return e.idx + '.' + e.code; }).join(',');
    var em = await S.raw('https://push2.eastmoney.com/api/qt/clist/get?pn=1&pz=20&fs=' +
      encodeURIComponent('b:MK0021,b:MK0022,b:MK0023,b:MK0024') +
      '&fields=f12,f14,f20,f184,f71', { timeoutMs: 12000 });
    if (em.ok) {
      var ej = JSON.parse(em.text);
      var arr = (ej && ej.data && ej.data.diff) || [];
      arr.forEach(function (d) {
        var c = String(d.f12 || '');
        var card = cards.find(function (x) { return x.code === c; });
        if (card) { card.shares = S.n(d.f20); card.netSubscribe = S.n(d.f71); }
      });
    }
  } catch (e) { errors.push('东财ETF份额/净申购：' + (e.message || '不可用')); }

  return { ok: cards.length > 0, cards: cards, errors: errors, asOf: S.nowBj(),
    note: '当前份额/净申购依赖东财接口，若显示 — 表示本环境未放行，可在本机正常使用' };
}

async function fetchIndustry() {
  var errors = [];
  var h = await S.rawGbk('https://vip.stock.finance.sina.com.cn/q/view/newSinaHy.php',
    { referer: 'https://finance.sina.com.cn', timeoutMs: 12000 });
  if (!h.ok) return { ok: false, boards: [], errors: ['新浪行业接口：' + (h.error || h.status)] };
  var m = h.text.match(/=\s*(\{[\s\S]*?\})\s*;?/);
  if (!m) return { ok: false, boards: [], errors: ['新浪行业数据解析失败'] };
  var map;
  try { map = JSON.parse(m[1]); } catch (e) { return { ok: false, boards: [], errors: ['新浪行业 JSON 解析失败'] }; }
  var boards = [];
  Object.keys(map).forEach(function (k) {
    var a = String(map[k]).split(',');
    if (a.length < 9) return;
    var name = a[1];
    var count = S.n(a[2]);
    var avgChange = S.n(a[4]);
    var changePct = S.n(a[5]);
    var amount = S.n(a[6]);
    var leadCode = a[8] ? String(a[8]).replace(/^(sh|sz)/, '') : null;
    var leadPrice = S.n(a[9]);
    var leadChangePct = S.n(a[10]);
    var leadName = a[12] || null;
    var strength = S.r2(Math.max(0, Math.min(100, 50 + (changePct || 0) * 6)), 0);
    boards.push({
      id: k, name: name, count: count,
      avgChange: avgChange, changePct: changePct, amount: amount,
      leadCode: leadCode, leadPrice: leadPrice, leadChangePct: leadChangePct, leadName: leadName,
      strength: strength
    });
  });
  boards.sort(function (x, y) { return y.strength - x.strength; });
  return { ok: boards.length > 0, boards: boards, errors: errors, asOf: S.nowBj() };
}

/* ---------------- 任务状态 ---------------- */

var running = null;   /* { startedAt, codes, mode } */

/* ---------------- 路由 ---------------- */

var server = http.createServer(async function (req, res) {
  var parsed = url.parse(req.url, true);
  var p = parsed.pathname;

  try {
    if (req.method === 'GET' && p === '/api/health') {
      return sendJson(res, 200, { ok: true, running: !!running, time: S.nowBj() });
    }

    /* 配置读取 */
    if (req.method === 'GET' && p === '/api/config') {
      return sendJson(res, 200, configView(loadCfg()));
    }

    /* 配置保存 */
    if (req.method === 'POST' && p === '/api/config') {
      var body = await readBody(req);
      var patch = {};

      if (body.llm) {
        patch.llm = {};
        ['model', 'baseUrl', 'timeoutMs', 'maxRetries', 'retryBaseDelayMs', 'temperature', 'maxTokens', 'jsonMode', 'apiKeyHeader']
          .forEach(function (k) { if (body.llm[k] !== undefined) patch.llm[k] = body.llm[k]; });
        /* 密钥留空 = 不修改；填了就覆盖 */
        if (typeof body.llm.apiKey === 'string' && body.llm.apiKey.trim()) {
          patch.llm.apiKey = body.llm.apiKey.trim();
        }
        if (body.llm.clearApiKey === true) patch.llm.apiKey = '';
      }
      if (body.analysis) {
        patch.analysis = {};
        if (Array.isArray(body.analysis.codes)) {
          patch.analysis.codes = body.analysis.codes.map(function (c) { return String(c).trim(); }).filter(Boolean);
        }
        ['klineLimit', 'concurrency', 'extraContext'].forEach(function (k) {
          if (body.analysis[k] !== undefined) patch.analysis[k] = body.analysis[k];
        });
      }
      if (body.news && body.news.keywords) {
        patch.news = { keywords: body.news.keywords };
      }
      if (body.sequoiaUrl !== undefined) {
        patch.sequoiaUrl = String(body.sequoiaUrl || '').trim() || 'http://127.0.0.1:8791';
      }

      saveCfgPatch(patch);
      return sendJson(res, 200, { ok: true, config: configView(loadCfg()) });
    }

    /* 测试模型连通性 */
    if (req.method === 'POST' && p === '/api/config/test') {
      var cfgT = loadCfg();
      var ready = CFG.checkLlmReady(cfgT);
      if (!ready.ready) {
        return sendJson(res, 200, { ok: false, stage: 'key', message: ready.hint, source: ready.source });
      }
      var t0 = Date.now();
      var probe = await LLM.chat(cfgT.llm, [
        { role: 'system', content: '你是连通性测试端点。只输出 JSON：{"ok":true}' },
        { role: 'user', content: '请只返回 {"ok":true}' }
      ], { maxRetries: 0 });
      if (!probe.ok) {
        return sendJson(res, 200, {
          ok: false, stage: 'request', message: probe.error,
          model: ready.model, baseUrl: ready.baseUrl, source: ready.source, elapsedMs: Date.now() - t0
        });
      }
      var parsedProbe = PR && probe.content ? probe.content.slice(0, 120) : '';
      return sendJson(res, 200, {
        ok: true, model: ready.model, baseUrl: ready.baseUrl, source: ready.source,
        elapsedMs: Date.now() - t0, sample: parsedProbe, usage: probe.usage || null
      });
    }

    /* 代码查询（给界面做「输入代码自动带出名称」） */
    if (req.method === 'GET' && p === '/api/lookup') {
      var code = String(parsed.query.code || '').trim();
      if (!/^\d{6}$/.test(code)) return sendJson(res, 400, { ok: false, message: '请输入 6 位数字代码' });
      var q = await S.fetchQuoteA(code);
      if (!q.ok) return sendJson(res, 200, { ok: false, code: code, message: q.message || ('查不到该代码的行情：' + (q.errors || []).join('；')), notStock: q.notStock || null });
      return sendJson(res, 200, {
        ok: true,
        code: code, name: q.quote.name,
        price: q.quote.price, changePct: q.quote.changePct,
        asOf: q.quote.asOf, source: q.source
      });
    }

    /* 历史记录列表 / 单条 */
    if (req.method === 'GET' && p === '/api/history') {
      return sendJson(res, 200, { ok: true, items: listHistory() });
    }
    if (req.method === 'GET' && p === '/api/history/item') {
      return sendJson(res, 200, { ok: true, payload: readHistory(parsed.query.file) });
    }

    /* 最近一次结果（界面首屏直接展示，不用等分析） */
    if (req.method === 'GET' && p === '/api/latest') {
      var items = listHistory();
      if (!items.length) return sendJson(res, 200, { ok: true, payload: null });
      return sendJson(res, 200, { ok: true, payload: readHistory(items[0].file) });
    }

    /* 分析（SSE 实时进度） */
    if (req.method === 'GET' && p === '/api/analyze') {
      /*
       * 一切拒绝原因都走 SSE 回给前端，而不是 HTTP 4xx。
       * 原因：浏览器 EventSource 拿不到错误响应体，用 4xx 会让用户
       * 只看到「连接中断」却不知道是没密钥还是任务在跑。
       */
      function sseReject(message, stage) {
        res.writeHead(200, {
          'Content-Type': 'text/event-stream; charset=utf-8',
          'Cache-Control': 'no-cache, no-transform',
          'Connection': 'keep-alive'
        });
        res.write('data: ' + JSON.stringify({ type: 'error', message: message, stage: stage || '' }) + '\n\n');
        res.end();
      }

      if (running) {
        return sseReject('已经有一个分析任务在运行，等它结束再点。', 'busy');
      }
      var codes = String(parsed.query.codes || '').split(/[,，\s]+/).map(function (x) { return x.trim(); }).filter(Boolean);
      var mode = String(parsed.query.mode || 'llm');

      var cfgA;
      try { cfgA = loadCfg(); } catch (e) { return sseReject('读配置失败：' + e.message, 'config'); }
      if (!codes.length) codes = (cfgA.analysis.codes || []);
      if (!codes.length) return sseReject('没有要分析的标的。去「标的」页加几个代码。', 'codes');

      var readyA = CFG.checkLlmReady(cfgA);
      if (mode === 'llm' && !readyA.ready) {
        return sseReject('还没配模型密钥。去「设置」填一个，或改点「不调模型，只跑数据」。', 'key');
      }

      res.writeHead(200, {
        'Content-Type': 'text/event-stream; charset=utf-8',
        'Cache-Control': 'no-cache, no-transform',
        'Connection': 'keep-alive',
        'X-Accel-Buffering': 'no'
      });
      res.write('retry: 3000\n\n');

      function sse(obj) {
        try { res.write('data: ' + JSON.stringify(obj) + '\n\n'); } catch (e) { }
      }

      running = { startedAt: S.nowBj(), codes: codes, mode: mode };
      sse({ type: 'hello', codes: codes, mode: mode, model: readyA.model, keySource: readyA.source });

      /* 可信度对账与主流程并发启动，不增加分析的关键路径耗时；
         结果在 finally 统一发出，即使分析本身失败也能拿到数据质量结论。 */
      var qualityPromise = RC.analyzeQuality(codes).catch(function (eQ) {
        return { type: 'quality', scope: 'analyze', status: 'failed',
          message: '可信度核对执行异常：' + eQ.message,
          checked: 0, matched: 0, mismatched: 0, unverified: 0,
          conflicts: [], items: [] };
      });

      var closed = false;
      req.on('close', function () { closed = true; });

      try {
        var payload = await R.runBatch(codes, cfgA, {
          noLlm: mode === 'no-llm',
          dryRun: mode === 'dry-run',
          onEvent: function (ev) {
            if (!closed) sse(ev);
          }
        });

        /* 存档到 data/history，界面「记录」页读这里；不再往 out/ 另写一份 */
        var stampName = 'run-' + new Date().toISOString().replace(/[:.]/g, '-') + '.json';
        var hPath = path.join(HISTORY_DIR, stampName);
        try {
          fs.writeFileSync(hPath, JSON.stringify(payload, null, 2), 'utf8');
          if (!closed) sse({ type: 'archived', file: stampName });
        } catch (e) {
          if (!closed) sse({ type: 'archived', error: e.message });
        }
      } catch (e) {
        if (!closed) sse({ type: 'error', message: e.message });
      } finally {
        /* 核对最多等 8 秒：宁可发一个「未完成核对」事件，也不让 SSE 被拖住或静默略过 */
        var qTimer = null;
        try {
          var qv = await Promise.race([
            qualityPromise,
            new Promise(function (resolve) {
              qTimer = setTimeout(function () {
                resolve({ type: 'quality', scope: 'analyze', status: 'incomplete',
                  message: '可信度核对超过 8 秒未完成，本次不声称已核对',
                  checked: 0, matched: 0, mismatched: 0, unverified: 0,
                  conflicts: [], items: [] });
              }, 8000);
            })
          ]);
          if (!closed) sse(qv);
        } catch (eQ2) { /* 对账彻底失败时不发半个事件，避免前端拿到残缺 JSON */ }
        finally { if (qTimer) clearTimeout(qTimer); }
        running = null;
        try { res.end(); } catch (e) { }
      }
      return;
    }

    /* 导出报告（下载 .md） */
    if (req.method === 'GET' && p === '/api/export') {
      var fileE = String(parsed.query.file || '');
      var payloadE = fileE ? readHistory(fileE) : (function () {
        var it = listHistory();
        if (!it.length) throw new Error('还没有可导出的分析记录');
        return readHistory(it[0].file);
      })();
      var lines = ['# A 股分析报告', '', '- 生成时间：' + payloadE.generatedAtText,
        '- 模型：' + (payloadE.model && payloadE.model.name), '', '---', ''];
      (payloadE.results || []).forEach(function (r) {
        lines.push(PR.renderText({ analysis: r.analysis })); lines.push(''); lines.push('---'); lines.push('');
      });
      var fname = 'astock-' + String(payloadE.generatedAtText || '').replace(/[\/\s:]/g, '-') + '.md';
      res.writeHead(200, {
        'Content-Type': 'text/markdown; charset=utf-8',
        'Content-Disposition': 'attachment; filename="' + fname + '"'
      });
      return res.end(lines.join('\n'));
    }

    /* Sequoia-X 伴侣服务代理（「策略扫描」页消费） */
    if (p.indexOf('/api/sequoia/') === 0) {
      var sub = p.slice('/api/sequoia'.length) + (parsed.search || '');
      var baseU = (loadCfg().sequoiaUrl || 'http://127.0.0.1:8791').replace(/\/+$/, '');
      var fwdUrl = baseU + sub;
      var fwdBody = (req.method === 'POST') ? await readBody(req) : null;
      var fwd = await proxyUrl(fwdUrl, req.method, fwdBody);
      if (!fwd.ok) {
        return sendJson(res, fwd.status || 502, {
          ok: false,
          message: '伴侣服务不可用（' + (fwd.error || fwd.status) + '），请确认 sequoia-service 已启动',
          url: fwdUrl
        });
      }
      return sendJson(res, fwd.status, fwd.json);
    }

    /* ETF 实时监控（腾讯行情 + 东财份额/净申购） */
    if (req.method === 'GET' && p === '/api/etf') {
      var etf = await fetchEtf();
      var etfChecks = await RC.run('etf', etf);
      return sendJson(res, etf.ok ? 200 : 502,
        CT.attach(etf, { scope: 'etf', checks: etfChecks, errorLevel: 'info' }));
    }

    /* 行业强度榜（新浪行业板块）：单源，明确标为未核对 */
    if (req.method === 'GET' && p === '/api/industry') {
      var ind = await fetchIndustry();
      var indChecks = await RC.run('industry', ind);
      return sendJson(res, ind.ok ? 200 : 502,
        CT.attach(ind, { scope: 'industry', checks: indChecks }));
    }

    /* ---------- 个人持仓分析 ---------- */
    if (req.method === 'GET' && p === '/api/portfolio') {
      var pf = PF.load();
      var out = await PF.analyze(pf.positions, { signalMap: loadSignalMap() });
      out.updatedAt = pf.updatedAt;
      var pfChecks = await RC.run('portfolio', out);
      var pfOut = CT.attach(out, { scope: 'portfolio', checks: pfChecks,
        requiredMissing: out.summary && out.summary.missingQuotes > 0 ? out.summary.missingQuotes : 0 });
      return sendJson(res, 200, pfOut);
    }

    if (req.method === 'POST' && p === '/api/portfolio') {
      var body = await readBody(req);
      if (!body || !Array.isArray(body.positions)) {
        return sendJson(res, 400, { ok: false, message: '请求体需要 positions 数组' });
      }
      var clean = PF.normalize(body.positions);
      var bad = (body.positions || []).filter(function (x) {
        return !/^\d{6}$/.test(String(x.code || '').trim()) || !(Number(x.shares) > 0);
      });
      PF.save(clean);
      var resv = await PF.analyze(clean, { signalMap: loadSignalMap() });
      resv.saved = clean.length;
      resv.rejected = bad.length;
      if (bad.length) {
        resv.message = '已保存 ' + clean.length + ' 只；' + bad.length + ' 条记录因代码非 6 位数字或股数无效被忽略。';
      }
      var resvChecks = await RC.run('portfolio', resv);
      return sendJson(res, 200, CT.attach(resv, { scope: 'portfolio', checks: resvChecks,
        requiredMissing: resv.summary && resv.summary.missingQuotes > 0 ? resv.summary.missingQuotes : 0 }));
    }

    /* 持仓模板下载（方便批量录入） */
    if (req.method === 'GET' && p === '/api/portfolio/template') {
      res.writeHead(200, {
        'Content-Type': 'text/csv; charset=utf-8',
        'Content-Disposition': 'attachment; filename="portfolio-template.csv"'
      });
      res.end('code,name,shares,avgCost,buyDate,note\n600519,贵州茅台,100,1400.00,2026-06-01,示例行\n000001,平安银行,1000,11.50,2026-07-15,\n');
      return;
    }

    /* ---------- 美股联动（市场级） ---------- */
    if (req.method === 'GET' && p === '/api/us-linkage') {
      var lk = await LK.build();
      var lkChecks = await RC.run('us-linkage', lk);
      /* 盘前即使两源涨跌幅一致（都归 0），联动结论依然不可用：
         不能出现 status=ok 却同时告诉用户「结论不可用」的自相矛盾。 */
      var preIssues = (lk.us && lk.us.marketState === 'pre-market')
        ? [CT.issue('PREMARKET_NOT_COMPARABLE', 'warn', null,
          lk.verdict.dataCaveat || '美股盘前，联动结论暂不可用', ['美股行情'])]
        : [];
      return sendJson(res, lk.ok ? 200 : 502,
        CT.attach(lk, { scope: 'us-linkage', checks: lkChecks, issues: preIssues }));
    }

    /* ---------- 数据可信度汇总（状态条 / 覆盖清单） ---------- */
    if (req.method === 'GET' && p === '/api/data-quality') {
      var snap = RC.snapshot();
      var warmErr = null;
      /* 只要还没做过预热就补跑一次：不能因为别的接口先被访问，就让覆盖清单里
         列出的「个股行情 / 本地 K 线」变成只列不查的空头条目 */
      if (parsed.query.refresh === '1' || !snap.scopes.warmup) {
        try { await RC.warmup(); } catch (eQ) { warmErr = eQ.message; }
        snap = RC.snapshot();
      }
      snap.warmupError = warmErr;
      return sendJson(res, 200, snap);
    }

    /* ---------- 因子体检（数据来自 Python 侧导出，非实时行情） ---------- */
    if (req.method === 'GET' && p === '/api/factors') {
      var fa = FA.load();
      return sendJson(res, fa.ok ? 200 : 503, fa);
    }

    if (req.method === 'GET' && p === '/api/factors/one') {
      var fname = (parsed.query && parsed.query.name) || '';
      var fone = FA.one(fname);
      return sendJson(res, fone.ok ? 200 : 404, fone);
    }

    /* ---------- 智能选股推荐（数据来自 Python 侧合成，非实时） ---------- */
    if (req.method === 'GET' && p === '/api/picks') {
      var pk = PK.load();
      return sendJson(res, pk.ok ? 200 : 503, pk);
    }

    /* 静态文件 */
    if (req.method === 'GET') return serveStatic(res, p);

    sendJson(res, 404, { ok: false, message: '接口不存在：' + p });
  } catch (e) {
    if (!res.headersSent) sendJson(res, 500, { ok: false, message: e.message });
    else { try { res.end(); } catch (e2) { } }
  }
});

server.listen(PORT, '127.0.0.1', function () {
  ensureConfig();
  var line = '════════════════════════════════════════════';
  console.log(line);
  console.log(' A 股分析工具已启动');
  console.log(' 打开浏览器访问：http://127.0.0.1:' + PORT);
  console.log(' 配置存放：' + CONFIG_PATH);
  console.log(' 停止服务：在本窗口按 Ctrl+C');
  console.log(line);
});

server.on('error', function (e) {
  if (e.code === 'EADDRINUSE') {
    console.error('✗ 端口 ' + PORT + ' 已被占用。换个端口：set ASTOCK_PORT=8791 后重新启动。');
  } else {
    console.error('✗ 启动失败：' + e.message);
  }
  process.exit(1);
});

/* 优雅停机 */
['SIGINT', 'SIGTERM'].forEach(function (sig) {
  process.on(sig, function () {
    console.log('\n正在停止服务…');
    server.close(function () { process.exit(0); });
    setTimeout(function () { process.exit(0); }, 3000);
  });
});
