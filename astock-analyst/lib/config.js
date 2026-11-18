'use strict';
/*
 * config.js — 配置加载与密钥注入
 *
 * 优先级：
 *   环境变量 > 配置文件 > 内置默认值
 * 密钥优先读环境变量；配置文件里的 apiKey 支持 ${ENV_NAME} 间接引用，避免密钥落盘。
 */

var fs = require('fs');
var path = require('path');
var llm = require('./llm.js');

var DEFAULTS = {
  llm: {
    baseUrl: '',            /* 留空则按模型名自动推断 */
    model: 'gpt-4o-mini',
    apiKey: '',             /* 留空则读环境变量；也可写 ${ENV_NAME} */
    apiKeyHeader: 'Authorization',
    timeoutMs: 90000,
    maxRetries: 2,
    retryBaseDelayMs: 1200,
    temperature: 0.2,
    maxTokens: 4000,
    jsonMode: true
  },
  analysis: {
    codes: [],              /* 要分析的股票代码列表 */
    newsLookbackHours: 72,
    klineLimit: 120,
    concurrency: 2,         /* 并发分析数量，避免触发上游限流 */
    extraContext: ''        /* 追加到提示词的自定义上下文 */
  },
  us: {
    symbols: {
      /* gb_$ 系列才是实时/最新收盘；int_* 是过期快照，勿用 */
      indices: [['gb_$dji', '道琼斯'], ['gb_$ixic', '纳斯达克'], ['gb_$inx', '标普500']],
      adr: [['gb_baba', '阿里巴巴'], ['gb_pdd', '拼多多'], ['gb_jd', '京东'], ['gb_nio', '蔚来'],
        ['gb_bidu', '百度'], ['gb_xpev', '小鹏汽车'], ['gb_li', '理想汽车'], ['gb_tme', '腾讯音乐']]
    },
    tencentLeaders: ['usDJI', 'usIXIC', 'usINX', 'usNVDA', 'usAMD', 'usTSM', 'usAVGO', 'usTSLA', 'usAAPL', 'usMSFT']
  },
  news: {
    keywords: {
      '688981': ['中芯国际', '晶圆', '代工'],
      'default': ['A股', '上市公司']
    },
    source: '东方财富资讯搜索'
  },
  output: {
    dir: 'out',
    jsonFile: 'analysis-results.json',
    textFile: 'analysis-report.md',
    saveFactorDump: true
  }
};

function deepMerge(base, over) {
  if (!over || typeof over !== 'object') return base;
  Object.keys(over).forEach(function (k) {
    var v = over[k];
    if (v && typeof v === 'object' && !Array.isArray(v) && base && typeof base[k] === 'object' && !Array.isArray(base[k])) {
      base[k] = deepMerge(base[k], v);
    } else {
      base[k] = v;
    }
  });
  return base;
}

function loadConfig(configPath) {
  var cfg = JSON.parse(JSON.stringify(DEFAULTS));
  var loadedFrom = '内置默认值';
  if (configPath) {
    var p = path.resolve(configPath);
    if (!fs.existsSync(p)) throw new Error('配置文件不存在：' + p);
    var raw = JSON.parse(fs.readFileSync(p, 'utf8'));
    cfg = deepMerge(cfg, raw);
    loadedFrom = p;
  }
  /*
   * 优先级：环境变量 > 配置文件 > 内置默认。
   * 环境变量里没有显式设置端点时，不能覆盖配置文件里已填的 baseUrl
   *（否则用户配置的自建网关/本地代理会被静默改掉）。
   */
  var envModel = process.env.ASTOCK_LLM_MODEL || process.env.LLM_MODEL || process.env.OPENAI_MODEL;
  if (envModel) cfg.llm.model = envModel;
  var envBase = process.env.ASTOCK_LLM_BASE_URL || process.env.LLM_BASE_URL || process.env.OPENAI_BASE_URL || process.env.OPENAI_API_BASE;
  if (envBase) cfg.llm.baseUrl = envBase;
  else if (!cfg.llm.baseUrl) cfg.llm.baseUrl = llm.resolveBaseUrl({ baseUrl: '', model: cfg.llm.model });

  cfg._loadedFrom = loadedFrom;
  return cfg;
}

/* 检查密钥可用性，返回状态说明（不含密钥） */
function checkLlmReady(cfg) {
  var info = llm.resolveApiKey(cfg.llm);
  return {
    ready: !!info.key,
    source: info.source,
    model: llm.resolveModel(cfg.llm),
    baseUrl: llm.resolveBaseUrl(cfg.llm),
    hint: info.key ? '' :
      '未找到 API Key。请设置环境变量（优先）：' + llm.KEY_ENV_ALIASES.slice(0, 5).join(' / ') +
      '；或在配置文件的 llm.apiKey 填写，或写 ${OPENAI_API_KEY} 形式间接引用。'
  };
}

module.exports = { DEFAULTS: DEFAULTS, loadConfig: loadConfig, checkLlmReady: checkLlmReady, deepMerge: deepMerge };
