'use strict';
/*
 * factors.js — 因子体检数据读取（「因子体检」页消费）
 *
 * 数据由 Python 侧导出：sequoia-service/export_factor_api.py -> data/factor_api.json
 * 这里只做两件事：按 mtime 缓存读取、文件缺失时给出可操作的提示。
 *
 * 为什么不在这里算统计量：
 *   「值得复核 / 方向需翻转」这类判定规则只能有一处定义。Node 再实现一遍，
 *   改阈值时必然漏改一边，页面和报告就会给出互相矛盾的结论。
 */

var fs = require('fs');
var path = require('path');

var DEFAULT_FILE = path.join(__dirname, '..', '..', 'sequoia-service', 'data', 'factor_api.json');

var cache = { mtime: 0, data: null };

function filePath() {
  return process.env.ASTOCK_FACTOR_FILE || DEFAULT_FILE;
}

/** 读取并缓存；文件更新后自动重读，避免每次请求都解析几 MB JSON。 */
function load() {
  var fp = filePath();
  var st;
  try {
    st = fs.statSync(fp);
  } catch (e) {
    return {
      ok: false,
      message: '因子数据尚未导出',
      hint: '在 sequoia-service 目录运行：python export_factor_api.py',
      path: fp
    };
  }
  if (cache.data && cache.mtime === st.mtimeMs) return cache.data;

  var parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(fp, 'utf8'));
  } catch (e) {
    return { ok: false, message: '因子数据解析失败：' + e.message, path: fp };
  }
  parsed.fileUpdatedAt = new Date(st.mtimeMs).toISOString().replace('T', ' ').slice(0, 19);
  cache = { mtime: st.mtimeMs, data: parsed };
  return parsed;
}

/** 单个因子详情（页面点击某一行时用；数据量小，直接从整包里筛）。 */
function one(name) {
  var all = load();
  if (!all.ok) return all;
  var hit = (all.factors || []).filter(function (f) { return f.name === name; })[0];
  if (!hit) return { ok: false, message: '没有这个因子：' + name };
  var segRows = ((all.segments || {}).rows || []).filter(function (r) { return r.factor === name; });
  var corr = (all.corrPairs || []).filter(function (p) { return p.a === name || p.b === name; });
  return {
    ok: true,
    generatedAt: all.generatedAt,
    meta: all.meta,
    factor: hit,
    segments: segRows,
    correlated: corr
  };
}

module.exports = { load: load, one: one, filePath: filePath };
