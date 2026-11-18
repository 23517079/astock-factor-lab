'use strict';
/*
 * tests/e2e.test.js — 端到端：起一个 mock 模型服务，跑完整 CLI 流程
 * 验证：真实取数 → 提示词 → 模型 → 结构化解析 → 固定字段输出 → 落盘
 * 不需要任何真实密钥，也不访问外网模型。
 */

var assert = require('assert');
var fs = require('fs');
var http = require('http');
var path = require('path');
var os = require('os');
var { execFile } = require('child_process');

var ROOT = path.join(__dirname, '..');
var t = 0, fails = 0;
var pending = [];
function ok(name, fn) {
  t++;
  try {
    var r = fn();
    if (r && typeof r.then === 'function') {
      /* 异步断言：必须 await，否则断言会在 Promise 完成前执行 */
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

/* 捕获的请求体，用来验证提示词真的把因子送进去了。
   promptVals 由 mock 服务从**这一次真实抓到的提示词**里解析出来，
   随后既拿来构造模型返回值，又拿来当断言基准 —— 价格天天变，
   但「提示词 → 模型依据 → 输出 JSON」三者必须带上同一个数。 */
var captured = [];
var promptVals = {};
function extractPromptVals(text) {
  function grab(re) { var m = re.exec(text || ''); return m ? m[1] : null; }
  return {
    price: grab(/现价：([0-9]+(?:\.[0-9]+)?)\s*元/),
    date: grab(/行情时间：(\d{4}-\d{2}-\d{2})\s+\d{2}:\d{2}:\d{2}/),
    time: grab(/行情时间：\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2})/),
    ma5: grab(/MA5\s+([0-9]+(?:\.[0-9]+)?)/),
    ma20: grab(/MA20\s+([0-9]+(?:\.[0-9]+)?)/),
    slope: grab(/近 20 日回归斜率\s+([0-9]+(?:\.[0-9]+)?)/)
  };
}

var srv = http.createServer(function (req, res) {
  var body = '';
  req.on('data', function (c) { body += c; });
  req.on('end', function () {
    captured.push(JSON.parse(body || '{}'));
    var last = captured[captured.length - 1];
    var userMsg = (last.messages || []).filter(function (m) { return m.role === 'user'; })[0];
    var content = userMsg ? userMsg.content : '';
    promptVals = extractPromptVals(content);
    var v = promptVals;
    var hasNumbers = !!(v.price && v.date && v.time && v.ma5 && v.ma20 && v.slope) &&
      content.indexOf(v.price) >= 0 && content.indexOf(v.date) >= 0 &&
      content.indexOf(v.ma20) >= 0;
    /* 缺口时 mock 自己也说没拿到数，让断言失败得有原因，
       而不是拿一份「写死的漂亮结论」冒充端到端通过。 */
    var px = v.price || '未知';
    var dt = v.date || '未知日期';
    var ma5 = v.ma5 || px;
    var ma20 = v.ma20 || px;
    var slope = v.slope || '未知';
    var target = v.price ? (Number(v.price) * 1.1).toFixed(2) : '未知';
    var reply = {
      symbol: '688981', name: '中芯国际',
      dataAsOf: dt + ' ' + (v.time || '未知时间'), dataCoverage: '5/5 个维度可用',
      verdict: '持有', conviction: 62,
      positionAdvice: '维持当前仓位，不加仓；跌破 ' + ma20 + ' 元（MA20）再考虑压缩',
      keyLevels: { stopLoss: ma20, takeProfit: target, support: px, resistance: target },
      triggers: [{ condition: '日收盘放量站上 ' + target + ' 元（20 日压力）', value: target + ' 元，量能 > 1.5 倍 20 日均量' }],
      invalidations: [{ condition: '日收盘跌破 ' + ma20 + ' 元（MA20）', value: ma20 + ' 元' }],
      evidenceChain: [{
        dimension: '技术面', factor: '趋势判定',
        observation: dt + ' 收盘 ' + px + ' 元，MA5 ' + ma5 + ' / MA20 ' + ma20 + '，近 20 日斜率 ' + slope,
        implication: '趋势上行但已接近 20 日压力 ' + target + ' 元，追高性价比下降'
      }],
      dimensionScores: { fundamental: 25, capital: 10, technical: 30, sentiment: -5, macro: 0 },
      usChinaRelation: '判定为联动：纳指 +0.44%、科技龙头均值 -0.21%，与该股 +2.2% 同向但幅度差 2.09 个百分点',
      risks: ['融资余额 5 日上升 1.14%，若股价跌破 MA20 存在杠杆被动减仓压力'],
      scenarios: [
        { scenario: '突破延续', condition: '放量站上 ' + target + ' 元', impact: '打开上方空间', action: '沿 MA20 移动止盈，不追高' },
        { scenario: '回踩确认', condition: '回踩 ' + ma20 + ' 元均线不破', impact: '维持原判断', action: '继续持有' }
      ],
      dataGaps: hasNumbers ? [] : ['模型未识别到关键数值'],
      disclaimer: '本分析基于公开数据与量化因子，仅供个人投资纪律参考，不构成投资建议，不承诺收益。'
    };
    res.writeHead(200, { 'Content-Type': 'application/json' });
    res.end(JSON.stringify({
      choices: [{ message: { content: JSON.stringify(reply) } }],
      usage: { total_tokens: 1200 }
    }));
  });
});

function runCli(args, env) {
  return new Promise(function (resolve) {
    execFile(process.execPath, [path.join(ROOT, 'cli.js')].concat(args),
      { cwd: ROOT, env: Object.assign({}, process.env, env || {}), timeout: 180000, maxBuffer: 32 * 1024 * 1024 },
      function (err, stdout, stderr) {
        resolve({ code: err ? (err.code === undefined ? 1 : err.code) : 0, stdout: stdout || '', stderr: stderr || '' });
      });
  });
}

srv.listen(0, '127.0.0.1', async function () {
  var port = srv.address().port;
  var outDir = path.join(os.tmpdir(), 'astock-e2e-' + Date.now());
  var cfgPath = path.join(os.tmpdir(), 'astock-e2e-cfg-' + Date.now() + '.json');

  var cfg = {
    llm: { baseUrl: 'http://127.0.0.1:' + port + '/v1', model: 'mock-model', apiKey: 'sk-mock-e2e-000000',
      timeoutMs: 15000, maxRetries: 1, retryBaseDelayMs: 100 },
    analysis: { codes: ['688981'], klineLimit: 60, concurrency: 1 },
    us: { symbols: { indices: [['int_dji', '道琼斯']], adr: [['gb_baba', '阿里巴巴']] } },
    news: { keywords: { default: ['中芯国际'] } },
    output: { dir: outDir, jsonFile: 'r.json', textFile: 'r.md', saveFactorDump: true }
  };
  fs.writeFileSync(cfgPath, JSON.stringify(cfg, null, 2), 'utf8');

  section('[1] CLI 端到端（真实取数 + mock 模型）');
  var r = await runCli(['--config', cfgPath, '--codes', '688981', '--out', outDir]);

  ok('CLI 正常退出', function () {
    assert.strictEqual(r.code, 0, '退出码 ' + r.code + '\n' + r.stdout.slice(-800) + r.stderr.slice(-400));
  });
  ok('输出文件已生成', function () {
    assert.ok(fs.existsSync(path.join(outDir, 'r.json')), '缺 JSON');
    assert.ok(fs.existsSync(path.join(outDir, 'r.md')), '缺报告');
    assert.ok(fs.existsSync(path.join(outDir, 'factor-dump.json')), '缺因子留档');
  });
  ok('JSON 结果结构完整且字段固定', function () {
    var j = JSON.parse(fs.readFileSync(path.join(outDir, 'r.json'), 'utf8'));
    assert.ok(j.generatedAt && j.generatedAtText);
    assert.ok(j.model && j.model.name === 'mock-model');
    assert.ok(j.marketContext && j.marketContext.usIndices);
    assert.ok(j.summary && j.summary.total === 1);
    assert.strictEqual(j.results.length, 1);
    var a = j.results[0].analysis;
    ['symbol', 'name', 'verdict', 'conviction', 'positionAdvice', 'keyLevels', 'triggers',
      'invalidations', 'evidenceChain', 'dimensionScores', 'usChinaRelation', 'risks',
      'scenarios', 'dataGaps', 'disclaimer'].forEach(function (k) {
      assert.ok(k in a, '缺字段 ' + k);
    });
  });
  ok('模型结论被完整采用', function () {
    var j = JSON.parse(fs.readFileSync(path.join(outDir, 'r.json'), 'utf8'));
    var a = j.results[0].analysis;
    assert.strictEqual(a.verdict, '持有');
    assert.strictEqual(a.conviction, 62);
    assert.ok(promptVals.ma20 && promptVals.price && promptVals.date,
      '提示词里没能解析出真实行情数值，无法做动态断言');
    assert.strictEqual(a.keyLevels.stopLoss, promptVals.ma20);
    assert.strictEqual(a.triggers.length, 1);
    assert.strictEqual(a.evidenceChain.length, 1);
    assert.ok(a.evidenceChain[0].observation.indexOf(promptVals.price) >= 0, '依据未引用现价');
    assert.ok(a.evidenceChain[0].observation.indexOf(promptVals.date) >= 0, '依据未引用行情时间');
    assert.ok(a.evidenceChain[0].observation.indexOf(promptVals.ma20) >= 0, '依据未引用 MA20');
  });
  ok('依据链条引用了真实取到的数值', function () {
    var j = JSON.parse(fs.readFileSync(path.join(outDir, 'r.json'), 'utf8'));
    var f = j.results[0].factors;
    var tech = f.dims.filter(function (d) { return d.key === 'technical'; })[0];
    assert.ok(tech && tech.technical, '缺技术面因子');
    assert.ok(tech.technical.ma20 !== undefined, '缺 MA20');
    assert.ok(tech.technical.support20 !== undefined, '缺支撑位');
    /* 关键：提示词里的数必须等于**这一次实际抓到**的数，
       不能各写各的。价格一变，两边同时变，断言仍然有牙齿。 */
    assert.ok(Math.abs(Number(f.quote.price) - Number(promptVals.price)) < 1e-6,
      '提示词现价 != 实际抓取现价：' + promptVals.price + ' vs ' + f.quote.price);
    assert.ok(Math.abs(Number(tech.technical.ma20) - Number(promptVals.ma20)) < 1e-6,
      '提示词 MA20 != 实际因子 MA20：' + promptVals.ma20 + ' vs ' + tech.technical.ma20);
    assert.strictEqual(tech.technical.asOf, promptVals.date, '因子日期 != 提示词行情日期');
    var link = f.usChinaLink;
    assert.ok(link && link.relations && link.relations.length > 0, '缺中美联动关系');
  });
  ok('模型请求里确实带上了因子数值', function () {
    assert.ok(captured.length >= 1, '未捕获到模型请求');
    var userMsg = captured[captured.length - 1].messages.filter(function (m) { return m.role === 'user'; })[0];
    assert.ok(promptVals.price && userMsg.content.indexOf(promptVals.price) >= 0, '提示词缺现价');
    assert.ok(promptVals.date && userMsg.content.indexOf(promptVals.date) >= 0, '提示词缺行情时间');
    assert.ok(promptVals.ma20 && userMsg.content.indexOf(promptVals.ma20) >= 0, '提示词缺 MA20');
    assert.ok(userMsg.content.indexOf('纳斯达克') >= 0 || userMsg.content.indexOf('美股') >= 0, '提示词缺美股数据');
  });
  ok('文本报告包含全部必需段落', function () {
    var md = fs.readFileSync(path.join(outDir, 'r.md'), 'utf8');
    ['【关键价位】', '【触发条件】', '【失效条件】', '【依据链条】', '【五维打分】',
      '【中美联动】', '【风险提示】', '【情景推演】', '【数据缺口】'].forEach(function (k) {
      assert.ok(md.indexOf(k) >= 0, '报告缺段落 ' + k);
    });
  });
  ok('coverage 区分「缺失」与「不适用」，且不适用项不进缺失清单', function () {
    var j = JSON.parse(fs.readFileSync(path.join(outDir, 'r.json'), 'utf8'));
    var cov = j.results[0].factors.coverage;
    assert.ok(cov && typeof cov.covered === 'number', '缺 coverage');
    assert.ok(Array.isArray(cov.missing), '缺 missing 数组');
    assert.ok(Array.isArray(cov.notApplicable), '缺 notApplicable 数组');
    assert.ok(Array.isArray(cov.notApplicableDims), '缺 notApplicableDims 数组');
    /* 不适用项绝不能同时出现在 missing 里 */
    var missText = cov.missing.join('；');
    cov.notApplicable.forEach(function (x) {
      var key = x.split('（')[0];
      assert.ok(missText.indexOf(key) < 0, '不适用项「' + key + '」混进了 missing');
    });
    /* 提示词里要明确告诉模型「不适用 ≠ 缺失，别扣分」 */
    var userMsg = captured[captured.length - 1].messages.filter(function (m) { return m.role === 'user'; })[0];
    if (cov.notApplicable.length) {
      assert.ok(userMsg.content.indexOf('不适用') >= 0, '提示词未说明不适用项');
    }
  });

  section('[2] 降级路径');
  var badOut = outDir + '-bad';
  var badPath = cfgPath.replace('.json', '-bad.json');
  var badCfg = JSON.parse(JSON.stringify(cfg));
  badCfg.llm.baseUrl = 'http://127.0.0.1:1/v1';
  badCfg.llm.maxRetries = 0;
  badCfg.llm.timeoutMs = 2000;
  fs.writeFileSync(badPath, JSON.stringify(badCfg, null, 2), 'utf8');
  var r2 = await runCli(['--config', badPath, '--codes', '688981', '--out', badOut]);
  ok('模型服务不可达时输出降级结果且字段完整', function () {
    assert.ok(r2.code === 1 || r2.code === 0, '异常退出码 ' + r2.code);
    var j = JSON.parse(fs.readFileSync(path.join(badOut, 'r.json'), 'utf8'));
    var a = j.results[0].analysis;
    ['verdict', 'conviction', 'positionAdvice', 'keyLevels', 'triggers', 'invalidations',
      'evidenceChain', 'dimensionScores', 'risks', 'scenarios', 'dataGaps'].forEach(function (k) {
      assert.ok(k in a, '降级结果缺字段 ' + k);
    });
    assert.strictEqual(a._degraded, true, '未标记降级');
    assert.ok(a._degradedReason && a._degradedReason.length > 5, '缺降级原因');
    assert.ok(['买入', '加仓', '持有', '减仓', '观望'].indexOf(a.verdict) >= 0, 'verdict 非法：' + a.verdict);
  });

  var noKeyPath = cfgPath.replace('.json', '-nokey.json');
  var noKey = JSON.parse(JSON.stringify(cfg));
  noKey.llm.apiKey = '${NOT_SET_KEY_XYZ}';
  fs.writeFileSync(noKeyPath, JSON.stringify(noKey, null, 2), 'utf8');
  var r3 = await runCli(['--config', noKeyPath, '--codes', '688981', '--out', outDir + '-nk']);
  ok('无密钥时拒绝运行并给出环境变量指引', function () {
    assert.strictEqual(r3.code, 2, '应退出码 2，实际 ' + r3.code);
    assert.ok(r3.stdout.indexOf('API Key') >= 0, '未提示密钥问题');
  });

  var beforeNoLlm = captured.length;
  var r4 = await runCli(['--config', cfgPath, '--codes', '688981', '--out', outDir + '-nollm', '--no-llm']);
  ok('--no-llm 不调用模型也能产出完整结构', function () {
    assert.strictEqual(r4.code, 0, '退出码 ' + r4.code);
    assert.strictEqual(captured.length, beforeNoLlm, 'no-llm 模式不应调用模型');
    var j = JSON.parse(fs.readFileSync(path.join(outDir + '-nollm', 'r.json'), 'utf8'));
    var a = j.results[0].analysis;
    assert.ok(a.verdict && a.evidenceChain.length > 0, '降级结果内容为空');
    assert.ok(a._degraded, '应标记为规则降级');
  });

  var beforeDry = captured.length;
  var r5 = await runCli(['--config', cfgPath, '--codes', '688981', '--dry-run']);
  ok('--dry-run 只输出提示词不落盘', function () {
    assert.strictEqual(captured.length, beforeDry, 'dry-run 不应调用模型');
    assert.ok(r5.stdout.indexOf('标的') >= 0, 'dry-run 未输出提示词预览');
  });

  section('[3] 配置与密钥');
  var r6 = await runCli(['--config', cfgPath, '--check-config']);
  ok('--check-config 不泄露密钥内容', function () {
    assert.strictEqual(r6.code, 0, '退出码 ' + r6.code);
    assert.ok(r6.stdout.indexOf('sk-mock-e2e-000000') < 0, '密钥泄露到输出');
    assert.ok(r6.stdout.indexOf('密钥状态') >= 0);
  });

  await Promise.all(pending);
  try { fs.unlinkSync(cfgPath); } catch (e) { }
  srv.close();
  console.log('\n结果：' + (t - fails) + '/' + t + ' 通过');
  if (fails) process.exit(1);
});
