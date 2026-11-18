'use strict';
/*
 * llm.js — 大模型接入层（OpenAI 兼容协议）
 *
 * 设计要点：
 *  1. apiKey 优先从环境变量读取，配置里的 key 只作为最后的兜底，且绝不写进日志/产物。
 *  2. 失败重试：区分「可重试」（网络抖动、429、5xx）与「不可重试」（401/403/400 参数错），
 *     可重试走指数退避，不可重试立刻失败，避免把配额错误重试成风暴。
 *  3. 超时：每次请求独立 AbortController 计时。
 *  4. 结构化输出：强制 JSON 模式（response_format），但模型不配合时由 parse.js 兜底。
 */

var http = require('http');
var https = require('https');
var { execFile } = require('child_process');
var { URL } = require('url');

/* 环境变量别名表：按优先级从高到低依次尝试 */
var KEY_ENV_ALIASES = [
  'ASTOCK_LLM_API_KEY',
  'LLM_API_KEY',
  'OPENAI_API_KEY',
  'DEEPSEEK_API_KEY',
  'MOONSHOT_API_KEY',
  'DASHSCOPE_API_KEY',
  'QWEN_API_KEY',
  'ZHIPUAI_API_KEY',
  'HUNYUAN_API_KEY',
  'AZURE_OPENAI_API_KEY',
  'GEMINI_API_KEY'
];

var BASE_URL_ENV_ALIASES = ['ASTOCK_LLM_BASE_URL', 'LLM_BASE_URL', 'OPENAI_BASE_URL', 'OPENAI_API_BASE'];
var MODEL_ENV_ALIASES = ['ASTOCK_LLM_MODEL', 'LLM_MODEL', 'OPENAI_MODEL'];

/* 常见模型的默认基址，省略 baseUrl 时按模型名推断 */
var MODEL_BASE_HINTS = [
  { match: /deepseek/i, base: 'https://api.deepseek.com/v1' },
  { match: /moonshot|kimi/i, base: 'https://api.moonshot.cn/v1' },
  { match: /qwen|通义/i, base: 'https://dashscope.aliyuncs.com/compatible-mode/v1' },
  { match: /glm|智谱/i, base: 'https://open.bigmodel.cn/api/paas/v4' },
  { match: /hunyuan|混元/i, base: 'https://api.hunyuan.cloud.tencent.com/v1' },
  { match: /gemini/i, base: 'https://generativelanguage.googleapis.com/v1beta/openai' },
  { match: /gpt|o1|o3/i, base: 'https://api.openai.com/v1' }
];

function envFirst(names) {
  for (var i = 0; i < names.length; i++) {
    var v = process.env[names[i]];
    if (v && String(v).trim()) return String(v).trim();
  }
  return '';
}

/* 解析密钥来源，返回 { key, source }，source 用于日志说明（不含密钥本身） */
function resolveApiKey(cfg) {
  var fromEnv = envFirst(KEY_ENV_ALIASES);
  if (fromEnv) return { key: fromEnv, source: '环境变量' };
  if (cfg && cfg.apiKey && String(cfg.apiKey).trim()) {
    var raw = String(cfg.apiKey).trim();
    // 支持 ${ENV_NAME} 形式从配置里间接引用环境变量
    var m = raw.match(/^\$\{([A-Z0-9_]+)\}$/);
    if (m) {
      var v = process.env[m[1]];
      if (v && String(v).trim()) return { key: String(v).trim(), source: '环境变量 ' + m[1] };
      // 引用的环境变量不存在：视为未配置，绝不把 "${...}" 字面量当密钥发出去
      return { key: '', source: '未配置（配置引用了环境变量 ' + m[1] + '，但该变量未设置）' };
    }
    if (raw.toLowerCase().indexOf('your-') === 0 || raw.toLowerCase().indexOf('sk-xxx') === 0
      || raw.indexOf('${') >= 0) {
      return { key: '', source: '未配置' };
    }
    return { key: raw, source: '配置文件（不推荐，建议改用环境变量）' };
  }
  return { key: '', source: '未配置' };
}

function resolveBaseUrl(cfg) {
  var fromEnv = envFirst(BASE_URL_ENV_ALIASES);
  if (fromEnv) return fromEnv;
  if (cfg && cfg.baseUrl && String(cfg.baseUrl).trim()) {
    var raw = String(cfg.baseUrl).trim();
    var m = raw.match(/^\$\{([A-Z0-9_]+)\}$/);
    if (m) {
      var v = process.env[m[1]];
      if (v) return String(v).trim();
    }
    return raw;
  }
  var model = (cfg && cfg.model) || envFirst(MODEL_ENV_ALIASES);
  for (var i = 0; i < MODEL_BASE_HINTS.length; i++) {
    if (MODEL_BASE_HINTS[i].match.test(model || '')) return MODEL_BASE_HINTS[i].base;
  }
  return 'https://api.openai.com/v1';
}

function resolveModel(cfg) {
  var fromEnv = envFirst(MODEL_ENV_ALIASES);
  if (fromEnv) return fromEnv;
  if (cfg && cfg.model && String(cfg.model).trim()) return String(cfg.model).trim();
  return 'gpt-4o-mini';
}

/* 敏感信息脱敏：错误信息里可能带上请求头 */
function scrub(text) {
  if (!text) return '';
  return String(text)
    .replace(/sk-[A-Za-z0-9_\-]{8,}/g, 'sk-***')
    .replace(/(Bearer\s+)[A-Za-z0-9_\-\.]{8,}/g, '$1***')
    .replace(/("?(?:api[_-]?key|authorization)"?\s*[:=]\s*"?)[^"\s,}]{6,}/gi, '$1***');
}

/*
 * 一次 HTTP 请求。优先用 curl（本机有 HTTPS_PROXY，Node 内置 fetch 不读代理），
 * 无 curl 时退回 https.request。
 */
function httpRequest(urlStr, body, headers, timeoutMs) {
  return new Promise(function (resolve, reject) {
    var u = new URL(urlStr);
    var payload = body === null ? '' : JSON.stringify(body);
    var args = ['-sS', '--compressed', '-m', String(Math.ceil(timeoutMs / 1000) + 2),
      '-X', 'POST', '-H', 'Content-Type: application/json'];
    /* 本机 / 内网地址绕过系统代理，否则会被代理拦截返回 502 */
    var isLocal = /^https?:\/\/(127\.0\.0\.1|localhost|0\.0\.0\.0|\[::1\])(:|\/|$)/i.test(urlStr);
    if (isLocal) args.push('--noproxy', '*');
    Object.keys(headers || {}).forEach(function (k) {
      args.push('-H', k + ': ' + headers[k]);
    });
    args.push('--data-binary', '@-');
    /* 用 -w 追加 HTTP 状态码，curl 本身不在 stdout 里回传状态 */
    args.push('-w', '\n__HTTP__%{http_code}');
    /* URL 必须放在所有选项之后 */
    args.push(urlStr);

    var child;
    try {
      child = execFile('curl', args, { maxBuffer: 32 * 1024 * 1024, timeout: timeoutMs + 3000 },
        function (err, stdout, stderr) {
          var out = String(stdout || '');
          var mm = out.match(/\n__HTTP__(\d{3})\s*$/);
          var status = mm ? parseInt(mm[1], 10) : 0;
          var bodyText = mm ? out.slice(0, mm.index) : out;
          if (err && !bodyText) {
            var msg = scrub((stderr || err.message || '').trim());
            reject(new Error('网络请求失败：' + (msg || 'curl exit ' + err.code)));
            return;
          }
          resolve({ status: status, body: bodyText, raw: (stderr || '').trim() });
        });
    } catch (e) {
      reject(new Error('网络请求失败：无法启动 curl（' + scrub(e.message) + '）'));
      return;
    }
    child.stdin.on('error', function () {});
    child.stdin.end(payload);
  });
}

function httpsRequest(urlStr, body, headers, timeoutMs) {
  return new Promise(function (resolve, reject) {
    var u = new URL(urlStr);
    var payload = body === null ? '' : JSON.stringify(body);
    var mod = u.protocol === 'http:' ? http : https;
    var req = mod.request({
      method: 'POST', hostname: u.hostname, port: u.port || (u.protocol === 'http:' ? 80 : 443),
      path: u.pathname + u.search, headers: Object.assign({ 'Content-Length': Buffer.byteLength(payload) }, headers),
      timeout: timeoutMs
    }, function (res) {
      var chunks = [];
      res.on('data', function (c) { chunks.push(c); });
      res.on('end', function () {
        resolve({ status: res.statusCode, body: Buffer.concat(chunks).toString('utf8') });
      });
    });
    req.on('timeout', function () { req.destroy(new Error('请求超时')); });
    req.on('error', function (e) { reject(new Error('网络请求失败：' + scrub(e.message))); });
    req.end(payload);
  });
}

/* 判断错误是否值得重试 */
function classify(status, bodyText) {
  var t = String(bodyText || '');
  if (status === 401 || status === 403) return { retryable: false, kind: '鉴权失败', hint: 'API Key 无效或无权限，请检查密钥与模型权限' };
  if (status === 400 || status === 404 || status === 422) return { retryable: false, kind: '请求被拒', hint: '接口地址、模型名或请求体不合法' };
  if (status === 429) return { retryable: true, kind: '触发限流', hint: '已放慢节奏重试；持续出现请检查配额' };
  if (status >= 500) return { retryable: true, kind: '服务端错误', hint: '上游暂时异常，通常重试可恢复' };
  if (status === 0) return { retryable: true, kind: '网络异常', hint: '本机网络或代理问题' };
  if (t.indexOf('insufficient_quota') >= 0) return { retryable: false, kind: '配额不足', hint: '账户余额或配额不足，请充值或换用其他模型' };
  if (t.indexOf('rate_limit') >= 0) return { retryable: true, kind: '触发限流', hint: '稍后重试' };
  return { retryable: false, kind: '未知错误', hint: '' };
}

/*
 * 调用大模型。
 * @param cfg  { baseUrl, model, apiKey, timeoutMs, maxRetries, retryBaseDelayMs, temperature, maxTokens, jsonMode }
 * @param messages [{role, content}]
 * @param opts { onAttempt } 用于日志
 * @returns { ok, status, content, raw, attempts, error }
 */
async function chat(cfg, messages, opts) {
  opts = opts || {};
  var base = resolveBaseUrl(cfg);
  var model = resolveModel(cfg);
  var keyInfo = resolveApiKey(cfg);
  var timeoutMs = num(cfg.timeoutMs, 90000);
  var maxRetries = Math.max(0, num(cfg.maxRetries, 2));
  var delayBase = Math.max(200, num(cfg.retryBaseDelayMs, 1200));
  var url = base.replace(/\/+$/, '') + '/chat/completions';

  if (!keyInfo.key) {
    return {
      ok: false, status: 0, content: '', attempts: 0, model: model, baseUrl: base,
      error: '未找到 API Key。请设置环境变量 ' + KEY_ENV_ALIASES.slice(0, 4).join(' / ') +
        ' 之一，或在配置文件中填写 apiKey（支持 ${ENV_NAME} 形式）。',
      errorKind: '未配置密钥', degraded: true
    };
  }

  var headers = { 'Content-Type': 'application/json', 'Authorization': 'Bearer ' + keyInfo.key };
  if (cfg.apiKeyHeader) {
    headers['Authorization'] = cfg.apiKeyHeader + ' ' + keyInfo.key;
    if (cfg.apiKeyHeader === 'x-api-key') delete headers['Authorization'];
  }

  var body = {
    model: model,
    messages: messages,
    temperature: num(cfg.temperature, 0.2),
    max_tokens: num(cfg.maxTokens, 4000)
  };
  /* 部分服务端不支持 response_format，降级时由 parse.js 处理 */
  if (cfg.jsonMode !== false) body.response_format = { type: 'json_object' };

  var attempts = 0, lastErr = null, lastStatus = 0, lastBody = '';
  for (var i = 0; i <= maxRetries; i++) {
    attempts++;
    var started = Date.now();
    var resp;
    try {
      resp = await httpRequest(url, body, headers, timeoutMs);
    } catch (e) {
      lastErr = scrub(e.message);
      lastStatus = 0;
      if (opts.onAttempt) opts.onAttempt({ attempt: attempts, ok: false, error: lastErr, elapsedMs: Date.now() - started });
      if (i < maxRetries) { await sleep(delayBase * Math.pow(2, i)); continue; }
      break;
    }

    lastStatus = resp.status;
    lastBody = resp.body || '';
    if (resp.status >= 200 && resp.status < 300) {
      var parsed;
      try { parsed = JSON.parse(resp.body); }
      catch (e) {
        lastErr = '响应不是合法 JSON：' + scrub(resp.body.slice(0, 200));
        if (opts.onAttempt) opts.onAttempt({ attempt: attempts, ok: false, error: lastErr, elapsedMs: Date.now() - started });
        if (i < maxRetries) { await sleep(delayBase * Math.pow(2, i)); continue; }
        break;
      }
      var content = extractContent(parsed);
      if (opts.onAttempt) opts.onAttempt({ attempt: attempts, ok: true, elapsedMs: Date.now() - started });
      return {
        ok: true, status: resp.status, content: content, raw: parsed, attempts: attempts,
        model: model, baseUrl: base, keySource: keyInfo.source,
        usage: parsed && parsed.usage ? parsed.usage : null
      };
    }

    var cls = classify(resp.status, resp.body);
    lastErr = cls.kind + '：' + cls.hint + '（HTTP ' + resp.status + '）';
    if (opts.onAttempt) opts.onAttempt({ attempt: attempts, ok: false, error: lastErr, elapsedMs: Date.now() - started });
    if (!cls.retryable || i >= maxRetries) break;
    await sleep(delayBase * Math.pow(2, i));
  }

  return {
    ok: false, status: lastStatus, content: '', attempts: attempts, model: model, baseUrl: base,
    error: lastErr || '未知失败', errorKind: classify(lastStatus, lastBody).kind,
    degraded: true, rawBody: scrub(String(lastBody || '').slice(0, 500))
  };
}

/* 兼容多种返回结构：choices[0].message.content / choices[0].text / output_text */
function extractContent(parsed) {
  if (!parsed) return '';
  if (typeof parsed === 'string') return parsed;
  if (parsed.output_text) return String(parsed.output_text);
  if (Array.isArray(parsed.choices) && parsed.choices.length) {
    var c = parsed.choices[0];
    if (c && c.message) {
      if (typeof c.message.content === 'string') return c.message.content;
      if (Array.isArray(c.message.content)) {
        return c.message.content.map(function (x) { return x && x.text ? x.text : ''; }).join('');
      }
    }
    if (c && typeof c.text === 'string') return c.text;
  }
  /* 部分代理会返回 { result: ... } */
  if (parsed.result && typeof parsed.result === 'string') return parsed.result;
  return '';
}

function sleep(ms) { return new Promise(function (r) { setTimeout(r, ms); }); }
function num(v, d) { var n = parseFloat(v); return isFinite(n) ? n : d; }

module.exports = {
  chat: chat,
  resolveApiKey: resolveApiKey,
  resolveBaseUrl: resolveBaseUrl,
  resolveModel: resolveModel,
  classify: classify,
  scrub: scrub,
  extractContent: extractContent,
  KEY_ENV_ALIASES: KEY_ENV_ALIASES,
  BASE_URL_ENV_ALIASES: BASE_URL_ENV_ALIASES,
  MODEL_ENV_ALIASES: MODEL_ENV_ALIASES
};
