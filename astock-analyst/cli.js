#!/usr/bin/env node
'use strict';
/*
 * cli.js — 命令行入口（界面之外的高级用法）
 *
 * 日常使用请双击 start.bat 打开界面。这个文件是给批量/定时任务用的。
 *
 * 用法：
 *   node cli.js --config config.json --codes 688981,159206 --out out
 *   node cli.js --codes 688981 --no-llm        只算因子不调模型（不消耗额度）
 *   node cli.js --codes 688981 --dry-run       只打印将要提交给模型的内容
 *   node cli.js --check-config                 只检查模型接入配置
 *
 * 退出码：0 全部成功；1 有降级/失败；2 配置错误
 */

var fs = require('fs');
var path = require('path');

var R = require('./lib/runner.js');
var LLM = require('./lib/llm.js');
var PR = require('./lib/prompt.js');
var CFG = require('./lib/config.js');
var S = require('./lib/sources.js');

function parseArgs(argv) {
  var out = { codes: [], config: null, out: null, noLlm: false, dryRun: false, checkConfig: false, quiet: false };
  for (var i = 2; i < argv.length; i++) {
    var a = argv[i];
    if (a === '--config' || a === '-c') out.config = argv[++i];
    else if (a === '--codes' || a === '-C') out.codes = String(argv[++i] || '').split(/[,，\s]+/).filter(Boolean);
    else if (a === '--out' || a === '-o') out.out = argv[++i];
    else if (a === '--no-llm') out.noLlm = true;
    else if (a === '--dry-run') out.dryRun = true;
    else if (a === '--check-config') out.checkConfig = true;
    else if (a === '--quiet' || a === '-q') out.quiet = true;
    else if (a === '--help' || a === '-h') out.help = true;
  }
  return out;
}

function log(m) { process.stdout.write(m + '\n'); }

async function main() {
  var args = parseArgs(process.argv);

  if (args.help) {
    log([
      'A 股分析工具 · 命令行模式',
      '',
      '日常使用请双击 start.bat 打开界面；这里是批量/定时任务用法。',
      '',
      '  node cli.js --config config.json --codes 688981,159206',
      '  node cli.js --codes 688981 --no-llm        仅算客观因子，不调模型',
      '  node cli.js --codes 688981 --dry-run       打印将要提交给模型的内容',
      '  node cli.js --check-config                 检查模型接入配置',
      '',
      '参数：',
      '  --config, -c    配置文件路径',
      '  --codes, -C     股票代码，逗号分隔',
      '  --out, -o       输出目录（默认 out/）',
      '  --no-llm        不调大模型',
      '  --dry-run       只输出提示词预览',
      '  --check-config  只检查配置',
      '  --quiet, -q     少打印',
      '',
      '密钥环境变量：' + LLM.KEY_ENV_ALIASES.slice(0, 6).join(' / ')
    ].join('\n'));
    return 0;
  }

  var cfg;
  try {
    cfg = CFG.loadConfig(args.config);
  } catch (e) {
    log('✗ 配置加载失败：' + e.message);
    return 2;
  }

  var codes = args.codes.length ? args.codes : (cfg.analysis.codes || []);
  codes = codes.map(function (c) { return String(c).trim(); }).filter(Boolean);

  var ready = CFG.checkLlmReady(cfg);

  if (args.checkConfig) {
    log('配置文件：' + cfg._loadedFrom);
    log('模型名称：' + ready.model);
    log('API 地址：' + ready.baseUrl);
    log('密钥来源：' + ready.source);
    log('密钥状态：' + (ready.ready ? '✓ 已就绪（不显示密钥内容）' : '✗ 未配置'));
    if (!ready.ready) { log('提示：' + ready.hint); return 2; }
    log('超时：' + cfg.llm.timeoutMs + 'ms｜重试：' + cfg.llm.maxRetries + ' 次');
    return 0;
  }

  if (!codes.length) {
    log('✗ 未指定股票代码。用 --codes 688981,159206 或在配置 analysis.codes 里填写。');
    return 2;
  }

  if (!args.noLlm && !args.dryRun && !ready.ready) {
    log('✗ ' + ready.hint);
    log('  可先用 --no-llm 只跑因子自检。');
    return 2;
  }

  if (!args.quiet) {
    log('════════════════════════════════════════════');
    log(' A 股分析  ' + S.nowBj());
    log(' 标的：' + codes.join('、'));
    log(' 模型：' + ready.model + ' @ ' + ready.baseUrl);
    log(' 密钥：' + (args.noLlm || args.dryRun ? '本次不调用模型' : ready.source));
    log('════════════════════════════════════════════');
  }

  var payload = await R.runBatch(codes, cfg, {
    noLlm: args.noLlm,
    dryRun: args.dryRun,
    onEvent: function (ev) {
      if (args.quiet) return;
      if (ev.type === 'market') {
        log('');
        log('▶ 美股：' + ev.usIndices.map(function (x) { return x.name + ' ' + x.changePct + '%'; }).join('、'));
        log('  中概：' + ev.adr.slice(0, 4).map(function (x) { return x.name + ' ' + x.changePct + '%'; }).join('、'));
        log('');
      } else if (ev.type === 'item-start') {
        log('  [' + ev.index + '/' + ev.total + '] ' + ev.code + ' …');
      } else if (ev.type === 'attempt') {
        log('    尝试 ' + ev.attempt + '：' + (ev.ok ? '成功（' + ev.elapsedMs + 'ms）' : '失败 ' + ev.error));
      } else if (ev.type === 'item-done') {
        if (ev.ok) {
          log('    ✓ ' + ev.name + '：' + ev.verdict + '｜确信度 ' + (ev.conviction || 0) + '｜' + ev.elapsedMs + 'ms');
        } else {
          log('    ! ' + ev.name + ' 降级：' + (ev.reason || '未知'));
        }
      }
    }
  });

  /* 落盘 */
  var outDir = path.resolve(args.out || cfg.output.dir);
  if (!fs.existsSync(outDir)) fs.mkdirSync(outDir, { recursive: true });

  var jsonPath = path.join(outDir, cfg.output.jsonFile);
  fs.writeFileSync(jsonPath, JSON.stringify(payload, null, 2), 'utf8');

  var md = ['# A 股分析报告', '', '- 生成时间：' + payload.generatedAtText,
    '- 模型：' + payload.model.name + ' @ ' + payload.model.baseUrl,
    '- 密钥来源：' + payload.model.keySource,
    '- 成功 ' + payload.summary.succeeded + ' / ' + payload.summary.total +
    '（降级 ' + payload.summary.degraded + '）', '', '---', ''];
  payload.results.forEach(function (r) {
    md.push(PR.renderText({ analysis: r.analysis }));
    md.push(''); md.push('---'); md.push('');
  });
  var mdPath = path.join(outDir, cfg.output.textFile);
  fs.writeFileSync(mdPath, md.join('\n'), 'utf8');

  if (cfg.output.saveFactorDump) {
    fs.writeFileSync(path.join(outDir, 'factor-dump.json'),
      JSON.stringify(payload.results.map(function (r) {
        return { symbol: r.symbol, name: r.name, factors: r.factors };
      }), null, 2), 'utf8');
  }

  if (!args.quiet) {
    log('');
    log('════════════════════════════════════════════');
    log(' 完成：成功 ' + payload.summary.succeeded + ' / ' + payload.summary.total +
      '，降级 ' + payload.summary.degraded);
    log(' JSON：' + jsonPath);
    log(' 报告：' + mdPath);
    log('════════════════════════════════════════════');
  }

  return payload.summary.degraded > 0 ? 1 : 0;
}

main().then(function (code) {
  process.exit(code || 0);
}).catch(function (e) {
  log('✗ 运行异常：' + e.message);
  if (process.env.DEBUG) log(e.stack);
  process.exit(1);
});
