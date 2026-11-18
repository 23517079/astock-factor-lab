'use strict';
/*
 * parse.js — 模型输出的结构化解析与降级
 *
 * 大模型返回的 JSON 不可靠是常态。本模块提供五级降级：
 *   L1 完整 JSON 直接 parse
 *   L2 提取 ```json 代码块
 *   L3 从文本中截取最外层 {...} 或 [...] 再 parse（处理前后有解释文字的情况）
 *   L4 修常见畸形：尾逗号、单引号、中文引号、截断补括号
 *   L5 逐字段正则提取 + 用 schema 默认值补齐
 * 若全部失败，调用方可根据 schema.defaults 生成一份「数据不足」的结构化降级结果，
 * 保证下游批量任务永远拿到固定字段，不会因为模型抽风而中断。
 */

/* 修复 JSON 里常见的畸形 */
function repairJson(text) {
  var s = String(text || '');
  /* 去 BOM 与零宽字符 */
  s = s.replace(/^\uFEFF/, '').replace(/[\u200B-\u200D\uFEFF]/g, '');
  /* 中文引号 → 双引号（只替换像键值分隔的位置，避免破坏正文） */
  s = s.replace(/[“”]/g, '"').replace(/[‘’]/g, "'");
  /* 尾逗号 */
  s = s.replace(/,\s*([}\]])/g, '$1');
  /* 单引号包裹的键名 → 双引号 */
  s = s.replace(/([{,]\s*)'([^']+)'\s*:/g, '$1"$2":');
  /* 单引号包裹的值（不含内部单引号） */
  s = s.replace(/:\s*'([^'"\n]*)'/g, ': "$1"');
  /* 未加引号的字符串值（有空格但不含逗号花括号） */
  s = s.replace(/:\s*([A-Za-z\u4e00-\u9fa5][^,{}\[\]"\n]*?)([,}\]])/g, function (m, v, tail) {
    var t = v.trim();
    if (t === 'true' || t === 'false' || t === 'null' || /^-?\d+(\.\d+)?$/.test(t)) return ': ' + t + tail;
    return ': "' + t.replace(/"/g, '\\"') + '"' + tail;
  });
  return s;
}

/* 截取最外层括号块 */
function extractBalanced(text, open, close) {
  var s = String(text || '');
  var start = s.indexOf(open);
  if (start < 0) return null;
  var depth = 0, inStr = false, esc = false;
  for (var i = start; i < s.length; i++) {
    var c = s[i];
    if (inStr) {
      if (esc) esc = false;
      else if (c === '\\') esc = true;
      else if (c === '"') inStr = false;
      continue;
    }
    if (c === '"') { inStr = true; continue; }
    if (c === open) depth++;
    else if (c === close) {
      depth--;
      if (depth === 0) return s.slice(start, i + 1);
    }
  }
  /* 截断的输出：补齐缺失的右括号 */
  var chunk = s.slice(start);
  var need = depth;
  return chunk + new Array(need > 0 ? need + 1 : 0).join(close);
}

function tryParse(text) {
  try { return { ok: true, value: JSON.parse(text) }; }
  catch (e) { return { ok: false, error: e.message }; }
}

/*
 * 主入口：从模型原始输出解析出对象
 * @param raw 模型返回文本
 * @param schema 字段定义 { field: {type, required, default, path} }
 * @returns { ok, value, level, errors }
 */
function parseModelOutput(raw, schema) {
  schema = schema || {};
  var errors = [];
  var text = String(raw == null ? '' : raw).trim();
  if (!text) {
    return { ok: false, level: 'empty', value: null, errors: ['模型返回为空'] };
  }

  /* L1：直接 parse */
  var r = tryParse(text);
  if (r.ok && isPlainObject(r.value)) return finish(r.value, 'L1-direct', schema, errors);

  /* L2：代码块 */
  var m = text.match(/```(?:json|JSON)?\s*([\s\S]*?)```/);
  if (m) {
    var b = tryParse(m[1].trim());
    if (b.ok && isPlainObject(b.value)) return finish(b.value, 'L2-codeblock', schema, errors);
    var b2 = tryParse(repairJson(m[1].trim()));
    if (b2.ok && isPlainObject(b2.value)) return finish(b2.value, 'L2-codeblock-repaired', schema, errors);
    errors.push('L2 代码块内容修复后仍无法解析');
  } else {
    errors.push('L2 未找到代码块');
  }

  /* L3：截取最外层花括号 */
  var bal = extractBalanced(text, '{', '}');
  if (bal) {
    var c = tryParse(bal);
    if (c.ok && isPlainObject(c.value)) return finish(c.value, 'L3-balanced', schema, errors);
    var c2 = tryParse(repairJson(bal));
    if (c2.ok && isPlainObject(c2.value)) return finish(c2.value, 'L3-balanced-repaired', schema, errors);
    errors.push('L3 括号截取后仍无法解析');
  } else {
    errors.push('L3 未找到花括号块');
  }

  /* L4：整段修复 */
  var d = tryParse(repairJson(text));
  if (d.ok && isPlainObject(d.value)) return finish(d.value, 'L4-repaired', schema, errors);
  errors.push('L4 整段修复后仍无法解析');

  /* L5：逐字段正则提取 */
  var picked = pickFields(text, schema);
  if (picked && Object.keys(picked).length) {
    errors.push('L5 仅按正则提取到部分字段');
    return finish(picked, 'L5-regex-partial', schema, errors);
  }
  errors.push('L5 正则提取为空');

  return { ok: false, level: 'failed', value: null, errors: errors };
}

/* 校验并补齐 schema 字段 */
function finish(value, level, schema, errors) {
  var out = {};
  Object.keys(schema).forEach(function (k) {
    var def = schema[k];
    var v = value[k];
    if (v === undefined || v === null) {
      if (def.required) errors.push('缺必填字段：' + k);
      out[k] = def.default !== undefined ? clone(def.default) : (def.type === 'array' ? [] : (def.type === 'object' ? {} : ''));
      return;
    }
    out[k] = coerce(v, def.type);
  });
  /* 保留 schema 之外的额外字段，方便调试与扩展 */
  Object.keys(value).forEach(function (k) {
    if (!(k in out)) out[k] = value[k];
  });
  return { ok: true, level: level, value: out, errors: errors };
}

function coerce(v, type) {
  if (!type) return v;
  if (type === 'string') return typeof v === 'string' ? v : (typeof v === 'number' ? String(v) : (v && v.text ? String(v.text) : JSON.stringify(v)));
  if (type === 'number') {
    var n = typeof v === 'number' ? v : parseFloat(String(v).replace(/[^\d.\-]/g, ''));
    return isFinite(n) ? n : 0;
  }
  if (type === 'array') {
    if (Array.isArray(v)) return v;
    if (typeof v === 'string') {
      try { var p = JSON.parse(v); return Array.isArray(p) ? p : [v]; } catch (e) { return v.split(/\n|；|;/).filter(Boolean); }
    }
    return [v];
  }
  if (type === 'object') {
    if (v && typeof v === 'object' && !Array.isArray(v)) return v;
    try { var p2 = JSON.parse(String(v)); return (p2 && typeof p2 === 'object') ? p2 : { value: v }; }
    catch (e) { return { value: v }; }
  }
  if (type === 'boolean') return v === true || v === 'true' || v === 1 || v === '1';
  return v;
}

/* L5：按字段名在文本中找值 */
function pickFields(text, schema) {
  var out = {};
  Object.keys(schema).forEach(function (k) {
    var def = schema[k];
    var label = def.label || k;
    var re = new RegExp('["\\u201c]?' + escapeRe(label) + '["\\u201d]?\\s*[:：]\\s*([^\\n\\r，,。]+)', 'i');
    var m = text.match(re);
    if (m) out[k] = m[1].trim().replace(/^["'「『]|["'」』]$/g, '');
    else {
      var re2 = new RegExp('["\\u201c]?' + escapeRe(k) + '["\\u201d]?\\s*[:：]\\s*([^\\n\\r，,。]+)', 'i');
      var m2 = text.match(re2);
      if (m2) out[k] = m2[1].trim().replace(/^["'「『]|["'」』]$/g, '');
    }
  });
  return out;
}

function escapeRe(s) { return String(s).replace(/[.*+?^${}()|[\]\\]/g, '\\$&'); }
function isPlainObject(v) { return v && typeof v === 'object' && !Array.isArray(v); }
function clone(v) {
  if (v === null || typeof v !== 'object') return v;
  return JSON.parse(JSON.stringify(v));
}

/*
 * 生成「数据不足」的降级结构：当模型不可用或输出全废时，
 * 用真实采集到的客观数据 + 明确的降级说明填满固定字段，保证下游不断链。
 */
function degradedResult(schema, context) {
  var out = {};
  Object.keys(schema).forEach(function (k) {
    var def = schema[k];
    out[k] = def.default !== undefined ? clone(def.default) : (def.type === 'array' ? [] : (def.type === 'object' ? {} : ''));
  });
  out._degraded = true;
  out._degradedReason = (context && context.reason) || '模型输出不合法，已按固定字段降级输出';
  out._degradedAt = (context && context.at) || new Date().toISOString();
  if (context && context.fallback && typeof context.fallback === 'object') {
    Object.keys(context.fallback).forEach(function (k) {
      if (k in out) out[k] = context.fallback[k];
    });
  }
  return out;
}

module.exports = {
  parseModelOutput: parseModelOutput,
  repairJson: repairJson,
  extractBalanced: extractBalanced,
  degradedResult: degradedResult,
  coerce: coerce
};
