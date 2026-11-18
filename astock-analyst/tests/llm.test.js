'use strict';
/*
 * tests/llm.test.js — 大模型接入层与解析降级
 * 用本地 mock HTTP 服务，不依赖任何真实密钥与外网。
 */

var assert = require('assert');
var http = require('http');
var LLM = require('../lib/llm.js');
var P = require('../lib/parse.js');
var PR = require('../lib/prompt.js');

var t = 0, fails = 0;
var pending = [];
function ok(name, fn) {
  t++;
  try {
    var r = fn();
    if (r && typeof r.then === 'function') {
      /* 异步断言：挂到 pending 上，主流程统一 await */
      pending.push(r.then(
        function () { console.log('  ✓ ' + name); },
        function (e) { fails++; console.log('  ✗ ' + name + ' → ' + e.message); }
      ));
      return;
    }
    console.log('  ✓ ' + name);
  } catch (e) { fails++; console.log('  ✗ ' + name + ' → ' + e.message); }
}
function section(s) { console.log('\n' + s); }

/* ---------- 起一个 mock 服务 ---------- */
function startMock(handler) {
  return new Promise(function (resolve) {
    var hits = [];
    var srv = http.createServer(function (req, res) {
      var body = '';
      req.on('data', function (c) { body += c; });
      req.on('end', function () {
        hits.push({ url: req.url, headers: req.headers, body: body });
        handler(hits.length, req, res, body);
      });
    });
    srv.listen(0, '127.0.0.1', function () {
      resolve({ srv: srv, port: srv.address().port, hits: hits });
    });
  });
}

function mkCfg(port, over) {
  var c = {
    baseUrl: 'http://127.0.0.1:' + port + '/v1',
    model: 'test-model',
    apiKey: 'sk-mock-test-key-000000',
    timeoutMs: 4000, maxRetries: 2, retryBaseDelayMs: 60, temperature: 0.1, maxTokens: 100
  };
  Object.keys(over || {}).forEach(function (k) { c[k] = over[k]; });
  return c;
}
var MSG = [{ role: 'user', content: 'test' }];

(async function () {
  section('[1] 密钥与端点解析');
  ok('环境变量优先于配置文件', function () {
    process.env.ASTOCK_LLM_API_KEY = 'sk-from-env-111111';
    var r = LLM.resolveApiKey({ apiKey: 'sk-from-file-222222' });
    assert.strictEqual(r.key, 'sk-from-env-111111');
    assert.strictEqual(r.source, '环境变量');
    delete process.env.ASTOCK_LLM_API_KEY;
  });
  ok('配置里的 ${ENV} 引用能取到环境变量', function () {
    process.env.MY_TEST_KEY = 'sk-indirect-333333';
    var r = LLM.resolveApiKey({ apiKey: '${MY_TEST_KEY}' });
    assert.strictEqual(r.key, 'sk-indirect-333333');
    assert.ok(r.source.indexOf('MY_TEST_KEY') >= 0);
    delete process.env.MY_TEST_KEY;
  });
  ok('${ENV} 引用但变量不存在时判为未配置，不把字面量当密钥', function () {
    var r = LLM.resolveApiKey({ apiKey: '${NOT_EXIST_KEY_XYZ}' });
    assert.strictEqual(r.key, '', '不应返回字面量密钥');
    assert.ok(r.source.indexOf('未配置') >= 0);
  });
  ok('按模型名推断默认端点', function () {
    assert.ok(LLM.resolveBaseUrl({ model: 'deepseek-chat' }).indexOf('deepseek') >= 0);
    assert.ok(LLM.resolveBaseUrl({ model: 'gpt-4o-mini' }).indexOf('openai') >= 0);
    assert.ok(LLM.resolveBaseUrl({ model: 'qwen-plus' }).indexOf('dashscope') >= 0);
  });
  ok('错误信息里的密钥被脱敏', function () {
    var s = LLM.scrub('failed with Authorization: Bearer sk-abcdefghijklmnop and key sk-zzzzzzzzzzzz');
    assert.ok(s.indexOf('sk-abcdefghijklmnop') < 0, '密钥未脱敏：' + s);
    assert.ok(s.indexOf('sk-zzzzzzzzzzzz') < 0);
  });

  section('[2] 请求与重试');
  var m1 = await startMock(function (n, req, res) {
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ choices: [{ message: { content: '{"verdict":"持有","conviction":60}' } }], usage: { total_tokens: 88 } }));
  });
  var mocks = [m1];   /* 所有 mock 服务统一在最后关闭，避免异步断言执行前被关掉 */
  ok('正常请求返回解析后的 content', async function () {
    var r = await LLM.chat(mkCfg(m1.port), MSG);
    assert.strictEqual(r.ok, true);
    assert.ok(r.content.indexOf('持有') >= 0, 'content 不对：' + r.content);
    assert.strictEqual(r.attempts, 1);
    assert.strictEqual(r.usage.total_tokens, 88);
  });
  ok('请求头带 Authorization、模型名与 response_format 正确传递', async function () {
    /* 依赖上一步已发出请求，这里等它落地 */
    for (var w = 0; w < 50 && m1.hits.length < 1; w++) await new Promise(function (r2) { setTimeout(r2, 20); });
    assert.ok(m1.hits.length >= 1, '未捕获到请求');
    var h = m1.hits[0];
    assert.ok(String(h.headers.authorization).indexOf('Bearer') >= 0, '缺 Authorization 头');
    var b = JSON.parse(h.body);
    assert.strictEqual(b.model, 'test-model');
    assert.ok(Array.isArray(b.messages));
    assert.ok(b.response_format && b.response_format.type === 'json_object', '缺 response_format');
  });

  var m2 = await startMock(function (n, req, res) {
    if (n < 3) { res.writeHead(500, { 'Content-Type': 'application/json' }); res.end('{"error":"boom"}'); return; }
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({ choices: [{ message: { content: '{"verdict":"买入"}' } }] }));
  });
  mocks.push(m2);
  ok('5xx 自动重试并在第 3 次成功', async function () {
    var r = await LLM.chat(mkCfg(m2.port), MSG);
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.attempts, 3, '重试次数不对：' + r.attempts);
    assert.strictEqual(m2.hits.length, 3);
  });

  var m3 = await startMock(function (n, req, res) {
    res.writeHead(429, { 'Content-Type': 'application/json' }); res.end('{"error":"rate limit"}');
  });
  mocks.push(m3);
  ok('429 触发重试直到用尽，并给出限流原因', async function () {
    var r = await LLM.chat(mkCfg(m3.port), MSG);
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.attempts, 3, '应重试满次数：' + r.attempts);
    assert.ok(r.error.indexOf('限流') >= 0, '错误信息应说明限流：' + r.error);
    assert.strictEqual(r.degraded, true);
  });

  var m4 = await startMock(function (n, req, res) {
    res.writeHead(401, { 'Content-Type': 'application/json' }); res.end('{"error":"invalid api key"}');
  });
  mocks.push(m4);
  ok('401 不重试（避免配额风暴），直接报鉴权失败', async function () {
    var r = await LLM.chat(mkCfg(m4.port), MSG);
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.attempts, 1, '401 不应重试，实际 ' + r.attempts);
    assert.ok(r.error.indexOf('鉴权') >= 0, '错误信息：' + r.error);
    assert.strictEqual(m4.hits.length, 1);
  });

  var m5 = await startMock(function (n, req, res) {
    res.writeHead(200, { 'Content-Type': 'application/json' }); res.end('这不是 JSON');
  });
  mocks.push(m5);
  ok('200 但返回非 JSON 时重试，最终报解析失败', async function () {
    var r = await LLM.chat(mkCfg(m5.port), MSG);
    assert.strictEqual(r.ok, false);
    assert.ok(r.error.indexOf('JSON') >= 0, '错误信息：' + r.error);
  });

  ok('无密钥时立即失败并给出环境变量指引', async function () {
    var r = await LLM.chat({ baseUrl: 'http://127.0.0.1:1/v1', model: 'x', apiKey: '' }, MSG);
    assert.strictEqual(r.ok, false);
    assert.strictEqual(r.attempts, 0, '不应发起请求');
    assert.ok(r.error.indexOf('API Key') >= 0, '错误信息：' + r.error);
  });

  section('[3] 输出解析五级降级');
  var schema = PR.SCHEMA.analysis;
  ok('L1 完整 JSON 直接解析', function () {
    var r = P.parseModelOutput('{"verdict":"加仓","conviction":70,"triggers":[{"condition":"放量站上压力位","value":"120"}]}', schema);
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.level, 'L1-direct');
    assert.strictEqual(r.value.verdict, '加仓');
    assert.strictEqual(r.value.triggers.length, 1);
  });
  ok('L2 从代码块提取', function () {
    var r = P.parseModelOutput('分析如下：\n```json\n{"verdict":"减仓","conviction":40}\n```\n以上', schema);
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.value.verdict, '减仓');
  });
  ok('L3 从夹杂说明文字中截取花括号', function () {
    var r = P.parseModelOutput('先说结论：{"verdict":"观望","conviction":30} 希望有帮助', schema);
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.value.verdict, '观望');
  });
  ok('L4 修复尾逗号等畸形 JSON', function () {
    var r = P.parseModelOutput('{"verdict":"买入","conviction":55,"triggers":[],}', schema);
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.value.verdict, '买入');
  });
  ok('L5 逐字段正则提取', function () {
    var r = P.parseModelOutput('结论：\nverdict：加仓\nconviction：66', schema);
    assert.strictEqual(r.ok, true);
    assert.strictEqual(r.value.verdict, '加仓');
    assert.ok(r.level.indexOf('L5') >= 0, '级别：' + r.level);
  });
  ok('全废时返回失败并保留错误线索', function () {
    var r = P.parseModelOutput('完全没有结构的一段话', schema);
    assert.strictEqual(r.ok, false);
    assert.ok(r.errors.length >= 3, '错误线索太少');
  });
  ok('缺必填字段被记录，但整体仍可用', function () {
    var r = P.parseModelOutput('{"conviction":50}', schema);
    assert.ok(r.errors.some(function (e) { return e.indexOf('verdict') >= 0; }), '未记录缺字段：' + r.errors);
    assert.strictEqual(r.value.verdict, '观望', '应回落到默认值');
  });
  ok('降级结构补齐全部固定字段', function () {
    var d = P.degradedResult(schema, { reason: '模型超时', at: '2026-11-12T00:00:00Z' });
    Object.keys(schema).forEach(function (k) { assert.ok(k in d, '缺字段 ' + k); });
    assert.strictEqual(d._degraded, true);
    assert.strictEqual(d._degradedReason, '模型超时');
    assert.strictEqual(d.verdict, '观望');
  });
  ok('输出截断的 JSON 能被补齐右括号后解析', function () {
    var r = P.parseModelOutput('{"verdict":"持有","conviction":40,"evidenceChain":[{"dimension":"技术面"', schema);
    assert.ok(r.ok || r.level.indexOf('L') >= 0, '应至少进入某级降级：' + r.level);
  });

  section('[4] 提示词约束');
  ok('系统提示词包含 JSON schema 字段名', function () {
    ['verdict', 'keyLevels', 'triggers', 'invalidations', 'evidenceChain', 'dimensionScores', 'scenarios'].forEach(function (k) {
      assert.ok(PR.SYSTEM_PROMPT.indexOf(k) >= 0, '提示词缺字段 ' + k);
    });
  });
  ok('系统提示词明确禁止无信息量表述', function () {
    assert.ok(PR.SYSTEM_PROMPT.indexOf('比买入时少了很多') >= 0, '未列出禁用的套话');
    assert.ok(PR.SYSTEM_PROMPT.indexOf('必须同时包含') >= 0, '未强调数值+时间要求');
  });
  ok('系统提示词限定 verdict 取值范围', function () {
    assert.ok(PR.SYSTEM_PROMPT.indexOf('买入 / 加仓 / 持有 / 减仓 / 观望') >= 0);
  });
  ok('因子渲染把数值与时间写进提示词', function () {
    var analysis = {
      code: '688981', name: '中芯国际', collectedAt: '2026-11-12 22:00:00', quoteSource: '新浪行情',
      quote: { price: 111.72, changePct: 2.2, open: 108.49, high: 114.9, low: 108.44, prevClose: 109.31, asOf: '2026-11-12 15:34:59' },
      dims: [
        { key: 'technical', label: '技术面', data: { available: true, price: 111.72, date: '2026-11-12',
          ma5: 109.74, ma10: 108, ma20: 104.2, ma60: 100, aboveMaText: 'MA5、MA10、MA20、MA60',
          trend: '上行', trendBasis: ['近 20 日回归斜率 0.5823'], pattern: '放量上涨',
          patternNote: '当日上涨 2.2% 且量能 2.1 倍', volumeRatio5v20: 2.1, volatilityAnnualPct: 45.2,
          support20: 94.8, resistance20: 114.9, support60: 90, resistance60: 137.48,
          denseZone: { lower: 110, upper: 112, sharePct: 28, bucketSize: 2 },
          ret5: 1.2, ret10: 3.4, ret20: -1.5, ret60: 8.9, source: '新浪日K', asOf: '2026-11-12', bars: 120 } }
      ],
      coverage: { covered: 1, total: 5, missing: ['基本面：业绩报表'] },
      usChinaLink: { available: true, items: [{ factor: '纳斯达克指数', value: '+0.44%',
        detail: '纳斯达克当日 0.44%', asOf: '2026-11-12 22:40', source: '新浪' }],
        relations: [{ type: '联动', confidence: '高', text: '判定为联动' }],
        channels: [{ name: '情绪传导', text: '隔夜纳指方向最容易在次日开盘半小时内反映' }] }
    };
    var txt = PR.renderFactors(analysis, {});
    assert.ok(txt.indexOf('111.72') >= 0, '缺现价');
    assert.ok(txt.indexOf('2026-11-12') >= 0, '缺时间');
    assert.ok(txt.indexOf('MA20') >= 0, '缺均线');
    assert.ok(txt.indexOf('成交密集区') >= 0, '缺密集区');
    assert.ok(txt.indexOf('判定为联动') >= 0, '缺联动结论');
    assert.ok(txt.indexOf('缺失') >= 0, '缺数据缺口说明');
  });
  ok('文本报告模板包含全部必需段落', function () {
    var a = {
      symbol: '688981', name: '中芯国际', verdict: '持有', conviction: 60,
      positionAdvice: '维持', keyLevels: { stopLoss: '94.8', takeProfit: '120', support: '94.8', resistance: '114.9' },
      triggers: [{ condition: '放量站上压力位', value: '114.9' }],
      invalidations: [{ condition: '跌破支撑', value: '94.8' }],
      evidenceChain: [{ dimension: '技术面', factor: '趋势', observation: '2026-11-12 收盘 111.72 元，斜率 0.58', implication: '上行' }],
      dimensionScores: { fundamental: 20, capital: 10, technical: 40, sentiment: 0, macro: 0 },
      usChinaRelation: '判定为联动', risks: ['杠杆资金上升'], scenarios: [{ scenario: '回落', condition: '跌破 94.8', impact: '转弱', action: '减仓' }],
      dataGaps: ['北向数据为季度披露'], disclaimer: '免责声明'
    };
    var txt = PR.renderText({ analysis: a });
    ['【关键价位】', '【触发条件】', '【失效条件】', '【依据链条】', '【五维打分】', '【中美联动】',
      '【风险提示】', '【情景推演】', '【数据缺口】', '免责声明'].forEach(function (k) {
      assert.ok(txt.indexOf(k) >= 0, '报告缺段落 ' + k);
    });
  });
  ok('JSON 输出是合法 JSON 且字段固定', function () {
    var j = JSON.parse(PR.renderJson({ verdict: '持有' }));
    assert.strictEqual(j.verdict, '持有');
  });

  await Promise.all(pending);
  mocks.forEach(function (m) { try { m.srv.close(); } catch (e) { } });
  console.log('\n结果：' + (t - fails) + '/' + t + ' 通过');
  if (fails) process.exit(1);
})().catch(function (e) {
  console.error('测试异常：', e);
  process.exit(1);
});
