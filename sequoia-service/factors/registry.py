#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factors.registry — 因子元数据登记表

为什么要把「公式说明 / 方向 / 最小样本数」和计算代码分开登记：
    * 评估环节需要知道 direction 才能把「第 1 组」统一成预期最好的一组，
      否则每次看十分组都要人工回忆「这个因子是大的好还是小的好」；
    * 面板构建需要 min_bars 判断「这只票上市够不够久」，避免写入一堆 NaN；
    * data_deps 让调用方可以按需只取必要的列，也为将来换数据源留接口。

约定（重要）：
    direction = +1  —— 因子值越大，预期未来收益越高；
    direction = -1  —— 因子值越小，预期未来收益越高。
    direction 是**先验假设**，不是事实。真实方向请以 factor_eval 的 IC 为准，
    若 IC 与先验长期反号，应当改这里的 direction 而不是改计算逻辑。

因子计算一律只用「截至当日」的数据（见 factors/compute.py 的前视偏差铁律）。
"""

from __future__ import annotations

from typing import Any

# 因子分类（顺序即打印顺序）
CATEGORIES: tuple[str, ...] = (
    "momentum",          # 动量
    "reversal",          # 反转
    "volatility",        # 波动
    "liquidity",         # 流动性
    "trend",             # 趋势
    "position",          # 位置
    "volume_price",      # 量价
    "return_structure",  # 收益结构
)

# 分类中文名（打印用）
CATEGORY_LABELS: dict[str, str] = {
    "momentum": "动量",
    "reversal": "反转",
    "volatility": "波动",
    "liquidity": "流动性",
    "trend": "趋势",
    "position": "位置",
    "volume_price": "量价",
    "return_structure": "收益结构",
}

# ── 因子定义 ──
# 字段说明：
#   name         因子唯一名（落库主键的一部分，改名等于新因子）
#   label        中文名（打印/报告用）
#   category     所属分类，取值必须在上面的 CATEGORIES 里
#   direction    +1 / -1，先验方向
#   min_bars     计算该因子所需的最少交易日数，不足则返回 None
#   formula_desc 公式说明（人话 + 伪代码，便于复核是否偷看未来）
#   data_deps    依赖的 stock_daily 列
FACTOR_DEFS: list[dict[str, Any]] = [
    # ───────────── 动量（剔除最近 20 日，规避短期反转污染） ─────────────
    {
        "name": "mom20",
        "label": "20日动量(跳过近20日)",
        "category": "momentum",
        "direction": 1,
        "min_bars": 41,
        "formula_desc": "close[t-20] / close[t-40] - 1，跳过最近 20 个交易日",
        "data_deps": ["close"],
    },
    {
        "name": "mom60",
        "label": "60日动量(跳过近20日)",
        "category": "momentum",
        "direction": 1,
        "min_bars": 81,
        "formula_desc": "close[t-20] / close[t-80] - 1，跳过最近 20 个交易日",
        "data_deps": ["close"],
    },
    {
        "name": "mom120",
        "label": "120日动量(跳过近20日)",
        "category": "momentum",
        "direction": 1,
        "min_bars": 141,
        "formula_desc": "close[t-20] / close[t-140] - 1，跳过最近 20 个交易日",
        "data_deps": ["close"],
    },
    # ───────────── 反转 ─────────────
    {
        "name": "ret1",
        "label": "1日收益(短期反转)",
        "category": "reversal",
        "direction": -1,
        "min_bars": 2,
        "formula_desc": "close[t] / close[t-1] - 1；日频反转，值越大预期后续越差",
        "data_deps": ["close"],
    },
    {
        "name": "rev5",
        "label": "5日收益(反转)",
        "category": "reversal",
        "direction": -1,
        "min_bars": 6,
        "formula_desc": "close[t] / close[t-5] - 1；短期涨多者后续弱",
        "data_deps": ["close"],
    },
    {
        "name": "rev10",
        "label": "10日收益(反转)",
        "category": "reversal",
        "direction": -1,
        "min_bars": 11,
        "formula_desc": "close[t] / close[t-10] - 1",
        "data_deps": ["close"],
    },
    # ───────────── 波动 ─────────────
    {
        "name": "vol20",
        "label": "20日波动率(年化)",
        "category": "volatility",
        "direction": -1,
        "min_bars": 21,
        "formula_desc": "std(日收益, 20) * sqrt(252)；低波溢价",
        "data_deps": ["close"],
    },
    {
        "name": "vol_ratio_20_60",
        "label": "短长波动比(20/60)",
        "category": "volatility",
        "direction": -1,
        "min_bars": 61,
        "formula_desc": "vol20 / vol60；比值高=波动正在放大，视为风险抬升",
        "data_deps": ["close"],
    },
    {
        "name": "atr_pct14",
        "label": "ATR占价比(14日)",
        "category": "volatility",
        "direction": -1,
        "min_bars": 15,
        "formula_desc": "mean(TR, 14) / close，TR = max(H-L, |H-C_prev|, |L-C_prev|)",
        "data_deps": ["high", "low", "close"],
    },
    {
        "name": "max_ret20",
        "label": "20日最大单日涨幅",
        "category": "volatility",
        "direction": -1,
        "min_bars": 21,
        "formula_desc": "max(日收益, 20)；彩票偏好，极端单日大涨后易回落",
        "data_deps": ["close"],
    },
    # ───────────── 流动性 ─────────────
    {
        "name": "turn20",
        "label": "20日均成交额(亿元)",
        "category": "liquidity",
        "direction": -1,
        "min_bars": 20,
        # 说明：stock_daily.turnover 列在当前腾讯源下存的是「成交额估算值」，
        # 不是换手率（腾讯接口不提供流通股本）。因此本因子的语义是成交额规模，
        # 用作流动性/市值代理，方向取 -1（小盘溢价）。
        "formula_desc": "mean(turnover, 20) / 1e8；turnover 列为成交额(元)",
        "data_deps": ["turnover"],
    },
    {
        "name": "amihud20",
        "label": "Amihud非流动性(20日)",
        "category": "liquidity",
        "direction": 1,
        "min_bars": 21,
        "formula_desc": "1e9 * mean(|日收益| / 成交额, 20)；越大=越不流动，非流动性溢价取 +1",
        "data_deps": ["close", "turnover"],
    },
    {
        "name": "vol_ratio5v20",
        "label": "量比(5日均量/20日均量)",
        "category": "liquidity",
        "direction": 1,
        "min_bars": 20,
        "formula_desc": "mean(volume, 5) / mean(volume, 20)；放量=资金关注度提升",
        "data_deps": ["volume"],
    },
    {
        "name": "turn_cv20",
        "label": "成交额变异系数(20日)",
        "category": "liquidity",
        "direction": -1,
        "min_bars": 20,
        "formula_desc": "std(turnover, 20) / mean(turnover, 20)；越大=关注度越不稳定",
        "data_deps": ["turnover"],
    },
    # ───────────── 趋势 ─────────────
    {
        "name": "ma_gap20",
        "label": "20日均线乖离",
        "category": "trend",
        "direction": -1,
        "min_bars": 20,
        "formula_desc": "close / mean(close, 20) - 1；乖离过大有均值回归压力",
        "data_deps": ["close"],
    },
    {
        "name": "ma_gap60",
        "label": "60日均线乖离",
        "category": "trend",
        "direction": -1,
        "min_bars": 60,
        "formula_desc": "close / mean(close, 60) - 1",
        "data_deps": ["close"],
    },
    {
        "name": "slope20",
        "label": "20日价格斜率(归一)",
        "category": "trend",
        "direction": 1,
        "min_bars": 20,
        "formula_desc": "OLS(close ~ t, 20日) 的斜率 / close；趋势强度",
        "data_deps": ["close"],
    },
    {
        "name": "r2_20",
        "label": "20日趋势拟合度R²",
        "category": "trend",
        "direction": 1,
        "min_bars": 20,
        "formula_desc": "上面那条回归的 R²；越接近 1 说明涨跌越「直」",
        "data_deps": ["close"],
    },
    # ───────────── 位置 ─────────────
    {
        "name": "dist_high250",
        "label": "距250日最高价",
        "category": "position",
        "direction": 1,
        "min_bars": 60,
        # 窗口 250 日；不足 250 日时用可用窗口（min_periods=60），
        # 即「上市不足一年」的票用上市以来最高价，落在同一因子里会有一点口径差异，
        # 所以面板构建时建议 --start 选在数据充足之后。
        "formula_desc": "close / max(high, 250日, 至少60日) - 1；越接近 0 越靠近新高",
        "data_deps": ["high", "close"],
    },
    {
        "name": "dist_low250",
        "label": "距250日最低价",
        "category": "position",
        "direction": 1,
        "min_bars": 60,
        "formula_desc": "close / min(low, 250日, 至少60日) - 1；越大=离底部越远（趋势延续）",
        "data_deps": ["low", "close"],
    },
    # ───────────── 量价 ─────────────
    {
        "name": "corr_pv20",
        "label": "量价相关(20日)",
        "category": "volume_price",
        "direction": -1,
        "min_bars": 21,
        "formula_desc": "corr(close, volume, 20)；量价同步上行时相关高，取 -1（量价背离更优）",
        "data_deps": ["close", "volume"],
    },
    {
        "name": "obv_slope20",
        "label": "OBV斜率(20日,归一)",
        "category": "volume_price",
        "direction": 1,
        "min_bars": 21,
        "formula_desc": "OLS(OBV ~ t, 20日) 斜率 / mean(volume, 20)；OBV 按涨跌方向累计成交量",
        "data_deps": ["close", "volume"],
    },
    # ───────────── 收益结构 ─────────────
    {
        "name": "overnight_ret20",
        "label": "隔夜收益均值(20日)",
        "category": "return_structure",
        "direction": -1,
        "min_bars": 21,
        "formula_desc": "mean(open[t]/close[t-1] - 1, 20)；隔夜情绪高涨后易反转",
        "data_deps": ["open", "close"],
    },
    {
        "name": "intraday_ret20",
        "label": "日内收益均值(20日)",
        "category": "return_structure",
        "direction": 1,
        "min_bars": 20,
        "formula_desc": "mean(close/open - 1, 20)；日内承接强=资金主动买入",
        "data_deps": ["open", "close"],
    },
]

# 内部索引：name -> 定义
_BY_NAME: dict[str, dict[str, Any]] = {d["name"]: d for d in FACTOR_DEFS}


def get(name: str) -> dict[str, Any]:
    """按名称取因子定义，未登记则抛 KeyError（带可用名单，便于排错）。"""
    try:
        return _BY_NAME[name]
    except KeyError:
        raise KeyError("未登记的因子：%r；可用因子：%s" % (name, ", ".join(all_names())))


def all_names() -> list[str]:
    """按登记顺序返回全部因子名。"""
    return [d["name"] for d in FACTOR_DEFS]


def by_category() -> dict[str, list[dict[str, Any]]]:
    """按分类分组返回因子定义（分类顺序与 CATEGORIES 一致，空分类不出现）。"""
    out: dict[str, list[dict[str, Any]]] = {}
    for cat in CATEGORIES:
        items = [d for d in FACTOR_DEFS if d["category"] == cat]
        if items:
            out[cat] = items
    return out


def directions() -> dict[str, int]:
    """name -> direction 的便捷映射（评估环节用）。"""
    return {d["name"]: int(d["direction"]) for d in FACTOR_DEFS}


def min_bars_map() -> dict[str, int]:
    """name -> min_bars 的便捷映射（面板构建用）。"""
    return {d["name"]: int(d["min_bars"]) for d in FACTOR_DEFS}


__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "FACTOR_DEFS",
    "all_names",
    "by_category",
    "directions",
    "get",
    "min_bars_map",
]
