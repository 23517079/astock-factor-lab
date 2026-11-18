'use strict';
/*
 * picks.js — 智能选股推荐数据读取（「智能选股」页消费）
 *
 * 数据由 Python 侧导出：sequoia-service/factor_score.py -> data/factor_score.json
 * 这里只负责按 mtime 缓存读取 + 文件缺失时给出可操作提示。
 *
 * 为什么合成和信号都在 Python 侧做：
 *   打分涉及因子方向修正、去冗余、横截面 rank 标准化，以及时间切分检验。
 *   这些逻辑一旦在 JS 里再实现一遍，两边一定会在某个阈值上分叉，
 *   页面显示的推荐和实际回测用的推荐就不是同一个东西了。
 */

var fs = require('fs');
var path = require('path');

var DEFAULT_FILE = path.join(__dirname, '..', '..', 'sequoia-service', 'data', 'factor_score.json');

var cache = { mtime: 0, data: null };

function filePath() {
  return process.env.ASTOCK_PICKS_FILE || DEFAULT_FILE;
}

function load() {
  var fp = filePath();
  var st;
  try {
    st = fs.statSync(fp);
  } catch (e) {
    return {
      ok: false,
      message: '推荐数据尚未生成',
      hint: '在 sequoia-service 目录运行：python factor_score.py',
      path: fp
    };
  }
  if (cache.data && cache.mtime === st.mtimeMs) return cache.data;

  var parsed;
  try {
    parsed = JSON.parse(fs.readFileSync(fp, 'utf8'));
  } catch (e) {
    return { ok: false, message: '推荐数据解析失败：' + e.message, path: fp };
  }
  parsed.ok = true;
  parsed.fileUpdatedAt = new Date(st.mtimeMs).toISOString().replace('T', ' ').slice(0, 19);
  cache = { mtime: st.mtimeMs, data: parsed };
  return parsed;
}

module.exports = { load: load, filePath: filePath };
