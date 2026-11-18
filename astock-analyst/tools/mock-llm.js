#!/usr/bin/env node
'use strict';
/*
 * mock-llm.js — 本地假模型，没密钥时也能把流程跑一遍
 *
 * 什么时候用：你还没申请到 API 密钥，但想先看看界面长什么样、结果怎么读。
 *
 * 它会起一个 OpenAI 兼容的假服务，用真实收到的提示词拼出一份格式合法的回复，
 * 并把每次请求的提示词打印到控制台 —— 能看清到底把什么喂给了模型。
 *
 * 用法：
 *   1. 双击运行本文件所在的窗口，或在项目根目录执行：
 *        node tools/mock-llm.js
 *   2. 打开界面 → 设置 → 填：
 *        模型名称：mock-model
 *        API 地址：http://127.0.0.1:8799/v1
 *        API 密钥：随便填几个字符（假服务不校验）
 *   3. 点「测试连接」应显示成功，然后就能点「开始分析」了
 *
 * 注意：假模型不产生真实分析。它的作用只有一个 —— 让你在配密钥之前，
 *       先确认数据能不能取到、界面能不能用、结果长什么样。
 */

var http = require('http');

var PORT = 8799;
for (var i = 2; i < process.argv.length; i++) {
  if (process.argv[i] === '--port') PORT = parseInt(process.argv[++i], 10) || PORT;
}

var captured = [];

/* 从提示词里抠出关键数值，让 mock 回复贴近真实语境 */
function grab(prompt, re, fallback) {
  var m = prompt && prompt.match(re);
  return m ? m[1] : fallback;
}

function buildReply(prompt) {
  var close = grab(prompt, /收盘\s+([\d.]+)\s*元/, '111.72');
  var ma20 = grab(prompt, /MA20\s+([\d.]+)/, '104.2');
  var res20 = grab(prompt, /20\s*日压力\s+([\d.]+)/, '114.9');
  var sup20 = grab(prompt, /20\s*日支撑\s+([\d.]+)/, '110.0');
  var dense = grab(prompt, /成交密集区：([\d.]+)–([\d.]+)\s*元.*?占近\s*20\s*日成交量\s*([\d.]+)%/, null);

  var ev = [];
  ev.push({
    dimension: '技术面', factor: '趋势判定',
    observation: '收盘 ' + close + ' 元，MA20 ' + ma20 + '，20 日压力 ' + res20 + ' 元',
    implication: '趋势结构由均线排列决定，接近压力位时追高性价比下降'
  });
  if (dense) {
    ev.push({
      dimension: '技术面', factor: '成交密集区',
      observation: '成交量最集中的价格带 ' + dense[1] + '–' + dense[2] + ' 元，占近 20 日成交量 ' + dense[3] + '%',
      implication: '该价格带是主要持仓成本区，回到此区间易获支撑，跌破则抛压集中'
    });
  }

  return {
    symbol: '', name: '',
    dataAsOf: '', dataCoverage: '',
    verdict: '持有', conviction: 58,
    positionAdvice: '维持当前仓位不加仓；回踩 ' + sup20 + ' 元不破可继续持有，放量站上 ' + res20 + ' 元再考虑加仓',
    keyLevels: {
      stopLoss: ma20, takeProfit: res20,
      support: sup20, resistance: res20
    },
    triggers: [{
      condition: '日收盘放量站上 20 日压力位 ' + res20 + ' 元',
      value: res20 + ' 元，量能需大于 1.5 倍 20 日均量'
    }],
    invalidations: [{
      condition: '日收盘跌破 20 日支撑位 ' + ma20 + ' 元',
      value: ma20 + ' 元'
    }],
    evidenceChain: ev,
    dimensionScores: { fundamental: 20, capital: 10, technical: 25, sentiment: 0, macro: 0 },
    usChinaRelation: '判定依据见因子清单中的「中美联动」段落，此处按同一逻辑复述：以外盘科技基准方向对照该股当日表现。',
    risks: [
      '主力资金单日净流入不能代表趋势，单日数据易被大单进出扭曲',
      '若跌破 20 日支撑 ' + ma20 + ' 元，前述持有判断失效'
    ],
    scenarios: [
      { scenario: '突破延续', condition: '放量站上 ' + res20 + ' 元', impact: '打开上方空间', action: '沿均线移动止盈，不追高' },
      { scenario: '回踩确认', condition: '回踩 ' + sup20 + ' 元密集区不破', impact: '维持原判断', action: '继续持有，不减' }
    ],
    dataGaps: [],
    disclaimer: '本分析基于公开数据与量化因子，仅供个人投资纪律参考，不构成投资建议，不承诺收益。'
  };
}

var srv = http.createServer(function (req, res) {
  var body = '';
  req.on('data', function (c) { body += c; });
  req.on('end', function () {
    var parsed = {};
    try { parsed = JSON.parse(body || '{}'); } catch (e) { parsed = {}; }
    var userMsg = (parsed.messages || []).filter(function (m) { return m.role === 'user'; })[0];
    var prompt = userMsg ? userMsg.content : '';

    captured.push(prompt);
    console.log('\n──── 收到第 ' + captured.length + ' 次请求 ────');
    console.log('提示词长度：' + prompt.length + ' 字符');
    console.log('提示词前 1200 字：');
    console.log(prompt.slice(0, 1200));
    console.log('…（其余省略）');

    var reply = buildReply(prompt);
    var payload = {
      id: 'mock-' + Date.now(),
      model: parsed.model || 'mock-model',
      choices: [{
        index: 0,
        finish_reason: 'stop',
        message: { role: 'assistant', content: JSON.stringify(reply) }
      }],
      usage: { prompt_tokens: Math.round(prompt.length / 2), completion_tokens: 600, total_tokens: Math.round(prompt.length / 2) + 600 }
    };
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify(payload));
  });
});

srv.listen(PORT, '127.0.0.1', function () {
  console.log('==============================================');
  console.log(' 假模型已启动，保持这个窗口开着');
  console.log('');
  console.log(' 在界面「设置」里填：');
  console.log('   模型名称：mock-model');
  console.log('   API 地址：http://127.0.0.1:' + PORT + '/v1');
  console.log('   API 密钥：随便填几个字符（不校验）');
  console.log('');
  console.log(' 然后回「分析」页点「开始分析」即可。');
  console.log(' 关掉这个窗口（或 Ctrl+C）即停止。');
  console.log('==============================================');
});
