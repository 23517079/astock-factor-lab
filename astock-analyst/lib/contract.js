/**
 * lib/contract.js — 统一状态契约（L1）
 *
 * 设计约束（方向确定会上定的三条不可越线）：
 *  1. 只增不改：attach() 返回浅拷贝，原有 ok / errors / cards / positions 等字段
 *     一律保持原值与原语义，不覆盖、不重命名。ok 是旧版布尔，status 是本版状态，
 *     两者可以不同（例如 ok=true 但 status=degraded）。
 *  2. 单源不伪装：没有第二数据源的字段计入 unverified，绝不写进 matched。
 *  3. 冲突不静默：mismatch 的字段同时保留主源值、第二源值、来源与差值，
 *     状态降级但数字照常出（用户选定的策略），由用户自己判断。
 *
 * 四态：
 *   ok          数据完整、所有可核对字段两源一致
 *   degraded    能出结论，但存在两源冲突或部分字段缺失
 *   incomplete  必需数据缺失（例如定增策略缺 akshare）
 *   failed      主流程失败或数据源全废
 */
'use strict';

var STATUS = {
  OK: 'ok',
  DEGRADED: 'degraded',
  INCOMPLETE: 'incomplete',
  FAILED: 'failed'
};

function num(v) { return typeof v === 'number' && isFinite(v); }

function fmt(v) {
  if (!num(v)) return '—';
  if (Math.abs(v) >= 1000) return v.toLocaleString('zh-CN', { maximumFractionDigits: 2 });
  return String(Math.round(v * 10000) / 10000);
}

function issue(code, level, field, message, sources) {
  return {
    code: code,
    level: level || 'info',
    field: field || null,
    message: String(message),
    sources: (sources || []).filter(Boolean)
  };
}

function countChecks(checks) {
  var c = { checked: checks.length, matched: 0, mismatched: 0, unverified: 0, conflicts: [] };
  checks.forEach(function (x) {
    if (x.result === 'match') c.matched += 1;
    else if (x.result === 'mismatch') { c.mismatched += 1; c.conflicts.push(x); }
    else c.unverified += 1;
  });
  return c;
}

/**
 * 给一个旧接口返回值套上状态契约。
 * @param {Object} payload 原返回值（不会被修改）
 * @param {Object} opt {scope, checks[], issues[], errorLevel, requiredMissing, fatal, asOf}
 */
function attach(payload, opt) {
  opt = opt || {};
  var src = payload || {};
  var out = {};
  Object.keys(src).forEach(function (k) { out[k] = src[k]; });

  var scope = opt.scope || 'unknown';
  var checks = opt.checks || [];
  var issues = [];
  (src.issues || []).forEach(function (x) { issues.push(x); });
  (opt.issues || []).forEach(function (x) { issues.push(x); });

  /* 旧 errors[] 转成 issues，但保留原数组不动 */
  (src.errors || []).forEach(function (msg) {
    issues.push(issue('SOURCE_ERROR', opt.errorLevel || 'warn', null, String(msg), [scope]));
  });

  var c = countChecks(checks);
  c.conflicts.forEach(function (x) {
    var p = x.primary || {}, s = x.secondary || {};
    issues.push(issue('SOURCE_CONFLICT', 'bad', x.field,
      x.label + '：' + (p.source || '主源') + ' ' + fmt(p.value) +
      ' 与 ' + (s.source || '第二源') + ' ' + fmt(s.value) +
      ' 不一致（差 ' + fmt(x.delta) + '，容差 ' + fmt(x.toleranceUsed) + '）',
      [p.source, s.source]));
  });
  /* 时点错位（time-skew）与「没有第二源」是两回事：前者有第二源但不可比，
     必须单独说清原因与影响，不能混进 NO_SECOND_SOURCE 里含糊带过。 */
  var skewCount = checks.filter(function (x) { return x.comparability === 'time-skew'; }).length;
  var noSecond = Math.max(0, c.unverified - skewCount);
  if (noSecond) {
    issues.push(issue('NO_SECOND_SOURCE', 'info', null,
      noSecond + ' 个字段未做独立核对（无第二源或主源取数失败），不计入「已验证」', [scope]));
  }
  if (skewCount) {
    issues.push(issue('TIME_SKEW_UNCOMPARABLE', 'warn', null,
      skewCount + ' 项因两源行情时点不可比未做一致性判定（新交易日盘前切换，' +
      '或美股盘中按交易时段推断的报价时间差）；口径不可比，不计入已验证也不算数据冲突，' +
      '该时段的联动结论不可用。每一项的两源数值与具体原因见 verification.items', [scope]));
  }

  var status = STATUS.OK;
  if (opt.fatal || src.ok === false) status = STATUS.FAILED;
  else if (opt.requiredMissing > 0) status = STATUS.INCOMPLETE;
  else if (c.mismatched > 0 || issues.some(function (i) { return i.level === 'bad' || i.level === 'warn'; })) {
    status = STATUS.DEGRADED;
  }

  out.status = status;
  out.issues = issues;
  out.verification = {
    scope: scope,
    asOf: opt.asOf || new Date().toISOString(),
    checked: c.checked,
    matched: c.matched,
    mismatched: c.mismatched,
    unverified: c.unverified,
    timeSkew: skewCount,
    conflicts: c.conflicts,
    items: checks,
    note: 'ok 是旧版布尔字段，status 是本版状态；两源冲突时两者可能不同'
  };
  return out;
}

/** 汇总若干 scope 的 verification，给 /api/data-quality 用 */
function summarize(scopes) {
  var t = { checked: 0, matched: 0, mismatched: 0, unverified: 0 };
  Object.keys(scopes || {}).forEach(function (k) {
    var v = scopes[k] || {};
    t.checked += v.checked || 0;
    t.matched += v.matched || 0;
    t.mismatched += v.mismatched || 0;
    t.unverified += v.unverified || 0;
  });
  return t;
}

module.exports = {
  STATUS: STATUS,
  attach: attach,
  issue: issue,
  countChecks: countChecks,
  summarize: summarize,
  fmt: fmt
};
