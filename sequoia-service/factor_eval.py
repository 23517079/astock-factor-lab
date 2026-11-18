#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_eval.py — 因子评估（IC / 分组 / 多空 / 换手 / 覆盖 / 衰减 / 冗余 / FDR）

依赖只有 pandas + numpy：
    * Spearman 用 rank() 之后手算 Pearson（环境里没有 scipy，也不想为一个公式装它）；
    * BH-FDR 手写（按 p 升序累积取 min，见 bh_fdr）；
    * 正态 p 值用 math.erfc，不查表。

════════════════════════════════════════════════════════════════════
关于 shift(-n)：本文件里唯一使用负向移位的地方是「未来收益标签」
    fwd = close[t+h] / entry_price - 1
这是要预测的目标，天生就该看未来——它不参与任何因子计算。
因子本身的计算在 factors/compute.py，那里禁止一切 shift(负数)。
════════════════════════════════════════════════════════════════════

两个方法论要点（2026-11-14 由 QA 独立验证后修正，改动前所有结论都失真）：

  1. 重叠标签 → t 值虚高。h 日标签在连续 h 天里高度重叠，日 IC 序列强自相关，
     朴素 t = mean/std*sqrt(n) 会把 t 放大近似 sqrt(h) 倍。
     因此主判定改用 Newey-West 修正的 t（newey_west_t），lag = max(0, horizon-1)：
       · h >= 2：lag = h-1，正好覆盖标签的重叠长度；
       · h == 1：无重叠，lag 必须为 **0**。此时 NW 方差退化成 gamma0，
         与朴素 t 只差一个 ddof 因子。若错写成 lag=1，会把实测为**负**的一阶
         自相关（ret1 -0.065 / vol20 -0.069）算进方差，反而放大 |t| 3~6%，
         属于反保守（实测 ret1 h=1 会从 p=0.050 变成 p=0.043，凭空「显著」）。
     朴素 t 只作为对照保留（字段名 t_value / t_naive）。

  2. 同日执行偏差 → 多空收益虚高/虚低。因子在 T 日收盘后才算完，实盘最早
     T+1 开盘能买。因此默认 entry="s1_open"：T+1 开盘买入，T+h 收盘卖出。
     entry="close" 仅作为对照口径（假设 T 日收盘就能成交），不要用它下结论。

  3. h=1 与 h>=2 的持有期口径不同（读 ic_decay 时必须知道）：
     entry="s1_open" 时
       · h=1：T+1 开盘买、T+1 收盘卖 → **日内收益，不含隔夜**；
       · h>=2：T+1 开盘买、T+h 收盘卖 → 含 h-1 个隔夜 + h 个日内。
     实测隔夜段的 IC 比日内段强 2~3 倍（vol20：-0.0811 vs -0.0355），
     所以 ic_decay 的第一个点天然偏低，衰减曲线开头偏低有一部分是口径造成的，
     不是因子失效。JSON 里 h=1 那一档带 caliber="日内" 标记。

指标口径：
    rank_ic        逐日横截面 Spearman(因子值, 未来 h 日收益)，当日有效样本 < min_stocks 跳过
    ic_mean/std    rank_ic 的均值 / 样本标准差
    icir           ic_mean / ic_std
    t_value        朴素 t = ic_mean / ic_std * sqrt(有效天数)（有重叠标签时偏大，仅对照）
    t_nw           Newey-West 修正 t（**主判定**），lag = max(0, horizon-1)
    p_value_nw     由 t_nw 转的双侧正态 p 值（**主判定**）
    positive       正 IC 天数占比
    groups         十分组；direction=-1 时先乘 -1，保证「第 1 组」始终是预期最好的一组
    monotonicity   组均收益的单调性：组序号(1..10) 与组均收益的 Spearman **取负**，
                   使「+1 = 第 1 组最强、完全单调」成为唯一解读方式（不再需要想符号）
    long_short     第 10 组 − 第 1 组，逐日算再平均；年化 = (1+均值)^(252/h) − 1
    turnover       Top 组相邻调仓期成分的 1 − Jaccard 重叠率
    panel_completeness  面板内数据完整度 = 因子有值数 / 面板股票池当日有行情数
                   （衡量「这个因子在自己池子里缺不缺」）
    market_coverage     市场覆盖 = 因子有值数 / 当日全市场有行情数
                   （衡量「这个面板代表了多少全市场」，--limit 时必然很低）
    ic_decay       horizon = 1/5/10/20 各算一次 IC 均值（h=1 为日内口径，见上文第 3 点）
    corr_matrix    因子两两「逐日横截面相关」的时间均值，用于查冗余
    fdr            对所有因子的 p_value_nw 做 BH-FDR 输出 q 值（主判定），
                   同时保留 q_naive（朴素 p 的 q 值）供对照
    direction_note 两档提示（IC 符号与先验 direction 相反时）：
                   · |t_nw| < 2：不显著，勿按 direction 解读「预期最好组」；
                   · |t_nw| >= 2：显著反号，registry.direction 或分组语义需要复核

用法：
    python factor_eval.py                                   # 全因子 / h=5 / 全部因子
    python factor_eval.py --factor mom20 --horizon 5
    python factor_eval.py --factor ret1 --horizon 5 --entry close      # 对照：同日收盘成交
    python factor_eval.py --factor mom20,mom60 --start 2025-06-03 --out data/factor_eval.json
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sqlite3
import sys
from typing import Any, Iterable, Sequence

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from factors import registry  # noqa: E402

DB = os.path.join(HERE, "data", "sequoia_v2.db")
DEFAULT_OUT = os.path.join(HERE, "data", "factor_eval.json")

# 年化交易日数（与 factors.compute 保持一致）
TRADING_DAYS = 252
# 逐日横截面有效样本下限：低于这个数 Spearman 没有统计意义
MIN_STOCKS = 30
# 十分组
N_GROUPS = 10
# 相关矩阵保留的最近交易日数（在 corr_matrix 内部还会等距抽样到 60 天）
CORR_KEEP = 120


# ────────────────────────────────────────────────────────────
# 统计小工具
# ────────────────────────────────────────────────────────────
def spearman(x: Sequence[float], y: Sequence[float]) -> float:
    """Spearman 秩相关 = 秩的 Pearson 相关。任一序列无波动时返回 nan。

    自己实现是为了不引入 scipy：平均秩由 pandas.Series.rank() 处理（并列取均值秩），
    剩下的就是协方差除以两个标准差。
    """
    a = pd.Series(list(x), dtype="float64")
    b = pd.Series(list(y), dtype="float64")
    both = a.notna() & b.notna()
    a, b = a[both], b[both]
    if len(a) < 3:
        return float("nan")
    ra, rb = a.rank(), b.rank()
    sa, sb = ra.std(ddof=0), rb.std(ddof=0)
    if sa == 0 or sb == 0:
        return float("nan")
    return float(((ra - ra.mean()) * (rb - rb.mean())).sum() / (len(ra) * sa * sb))


def normal_p_value(t_value: float) -> float:
    """双侧正态 p 值：p = erfc(|t| / sqrt(2))。因子 IC 的样本量大，正态近似足够。"""
    if t_value is None or not np.isfinite(t_value):
        return float("nan")
    return float(math.erfc(abs(t_value) / math.sqrt(2.0)))


def bh_fdr(p_values: Sequence[float]) -> list[float]:
    """Benjamini-Hochberg FDR，返回与输入同序的 q 值。

    步骤：p 升序排列 → q_(i) = p_(i) * m / i → 从大到小取累积最小 → 截断到 1。
    nan 保持 nan（不参与排序，也不占 m 的名额）。
    """
    arr = np.asarray(list(p_values), dtype="float64")
    q = np.full(arr.shape, np.nan, dtype="float64")
    valid = np.isfinite(arr)
    m = int(valid.sum())
    if m == 0:
        return q.tolist()
    idx = np.where(valid)[0]
    order = idx[np.argsort(arr[idx], kind="mergesort")]
    ranked = arr[order] * m / np.arange(1, m + 1)
    # 从后往前取累积最小，保证 q 单调
    ranked = np.minimum.accumulate(ranked[::-1])[::-1]
    q[order] = np.clip(ranked, 0.0, 1.0)
    return q.tolist()


def newey_west_t(ic_series: Sequence[float], lag: int) -> float:
    """IC 序列均值的 Newey-West 修正 t 值：t = mean / sqrt(var_nw / n)。

    为什么需要它：h 日收益标签在连续 h 天内高度重叠（T 日与 T+1 日的 5 日标签
    有 4 天重合），日 IC 序列因此强正自相关，朴素 t = mean/std*sqrt(n) 把有效
    样本量当成 n，会虚高约 sqrt(h) 倍。NW 用 Bartlett 核加权自协方差修正：

        var_nw = γ0 + 2 * Σ_{k=1..lag} (1 − k/(lag+1)) * γk
        γk     = (1/n) Σ_t (x_t − x̄)(x_{t+k} − x̄)

    Args:
        ic_series: 日度 IC 序列（含 NaN 会被剔除）。
        lag: 滞后阶数；惯例取 horizon − 1（重叠长度），调用方负责保证 ≥ 1。

    Returns:
        NW t 值；样本 < 3 或方差为 0 时返回 nan。
        若 var_nw 因负自协方差落到 ≤ 0（极端情况），退回 γ0 并照常返回，
        因为真实数据的 NW 方差为负通常意味着样本太短，而不是真有负方差。
    """
    arr = np.asarray(list(ic_series), dtype="float64")
    arr = arr[np.isfinite(arr)]
    n = int(arr.size)
    if n < 3:
        return float("nan")

    mean = float(arr.mean())
    dev = arr - mean
    gamma0 = float((dev * dev).sum() / n)
    if gamma0 <= 0:
        return float("nan")

    lag = max(0, int(lag))
    var_nw = gamma0
    for k in range(1, min(lag, n - 1) + 1):
        gamma_k = float((dev[k:] * dev[:-k]).sum() / n)
        var_nw += 2.0 * (1.0 - k / float(lag + 1)) * gamma_k
    if var_nw <= 0:
        var_nw = gamma0

    return float(mean / math.sqrt(var_nw / n))


def _annualize(mean_return: float, horizon: int) -> float | None:
    """把 h 日平均收益折算成年化。均值 <= -100% 时返回 -1（避免复数/爆掉）。

    horizon <= 0 直接返回 None：否则 252/h 会 ZeroDivisionError，
    而「拿着非法 horizon 把一个进程炸掉」的代价远大于返回空值。
    """
    if mean_return is None or horizon is None or int(horizon) <= 0:
        return None
    m = float(mean_return)
    if not np.isfinite(m):
        return None
    if m <= -1.0:
        return -1.0
    return float((1.0 + m) ** (TRADING_DAYS / float(horizon)) - 1.0)


def monotonicity_of(group_means: Sequence[float]) -> float | None:
    """组均收益的单调性：组序号(1..n) 与组均收益的 Spearman **取负**。

    为什么取负：组 1 = 按 direction 判断的预期最好组，完全单调时组均收益随组号
    下降，原始相关为 -1；取负后「+1 = 第 1 组最强且完全单调」成为唯一解读方式，
    调用方不需要每次想符号。

    **全库唯一一份定义**（2026-11-16 从 `evaluate_factor` 抽出）：
    `rolling_eval.judge_factor()` 算窗口单调性时也调这里。若滚动验证另写一遍，
    两处的「有效组数下限」或符号约定迟早会分叉，而分叉的表现是
    「滚动验证选出的因子与因子体检页的档位互相矛盾」—— 这类不一致极难排查。

    Args:
        group_means: 长度 = 分组数，组 1 在前；允许含 None / NaN（会被跳过）。

    Returns:
        单调性得分；有效组数 < 3 或组号与收益无波动时返回 None（判不了，
        绝不能返回 0.0 —— 0.0 会被下游当成「算出来了，且不单调」）。
    """
    finite: list[tuple[int, float]] = []
    for index, mean in enumerate(group_means):
        if mean is None:
            continue
        try:
            value = float(mean)
        except (TypeError, ValueError):
            continue
        if np.isfinite(value):
            finite.append((index + 1, value))
    if len(finite) < 3:
        return None
    xs, ys = zip(*finite)
    raw = spearman(xs, ys)
    return None if not np.isfinite(raw) else float(-raw)


# ────────────────────────────────────────────────────────────
# 读数据
# ────────────────────────────────────────────────────────────
def load_price_wide(conn: sqlite3.Connection, column: str,
                    symbols: Iterable[str] | None = None) -> pd.DataFrame:
    """从 stock_daily 读某一列，转成「index=date, columns=symbol」宽表。

    Args:
        column: 只允许 "close" / "open" / "volume"（白名单，避免把列名拼进 SQL
            时留口子）。volume 用于结算探针判定「T+1 成交量为 0 → 买不到」。
        symbols: 限定股票；None 表示全市场。

    Returns:
        宽表；价格缺失格子为 NaN（**绝不能填 0**：0 会被当成跌停价参与收益计算，
        一个 0 就能把整个横截面的相关性算歪）。
        **volume 的 0 必须保留**：它正是「不可买」的判据，转成 NaN 会让探针
        把它当成缺失数据，两种原因在页面上是完全不同的结论。
    """
    allowed = ("close", "open", "volume")
    if column not in allowed:
        raise ValueError("load_price_wide 只支持 %s，收到 %r" % (allowed, column))

    sql = "SELECT symbol, date, %s FROM stock_daily" % column
    params: list[Any] = []
    if symbols is not None:
        symbol_list = list(symbols)
        if not symbol_list:
            return pd.DataFrame()
        sql += " WHERE symbol IN (%s)" % ",".join("?" * len(symbol_list))
        params.extend(symbol_list)
    df = pd.read_sql(sql, conn, params=params)
    if df.empty:
        return pd.DataFrame()
    df[column] = pd.to_numeric(df[column], errors="coerce")
    if column == "volume":
        # 成交量 0 是有效观测（停牌/一字板），必须保留给 settle_check 判不可买。
        df.loc[df[column] < 0, column] = np.nan
    else:
        # 价格为 0 是脏数据（停牌占位），按缺失处理
        df.loc[df[column] <= 0, column] = np.nan
    wide = df.pivot(index="date", columns="symbol", values=column).sort_index()
    return wide.astype("float64")


def load_close_wide(conn: sqlite3.Connection,
                    symbols: Iterable[str] | None = None) -> pd.DataFrame:
    """全历史收盘价宽表（index=date 升序，columns=symbol）。

    多读一点历史没坏处：标签需要 end 之后的 horizon 天，宽表整段给了就都能算。
    """
    return load_price_wide(conn, "close", symbols)


def load_open_wide(conn: sqlite3.Connection,
                   symbols: Iterable[str] | None = None) -> pd.DataFrame:
    """全历史开盘价宽表（T+1 开盘建仓口径的买入价来源）。"""
    return load_price_wide(conn, "open", symbols)


# 建仓口径（entry）：决定「T 日算出的因子」在什么时候真正成交
ENTRY_MODES: tuple[str, ...] = ("s1_open", "s1_close", "close")
ENTRY_NOTES: dict[str, str] = {
    "s1_open": "T+1 开盘建仓，T+h 收盘平仓（默认，最贴近实盘）",
    "s1_close": "T+1 收盘建仓，T+h 收盘平仓（保守一点的实盘近似）",
    "close": "T 日收盘建仓（同日执行，仅作对照，实盘做不到）",
}


def load_forward_returns(close_wide: pd.DataFrame, horizon: int,
                         entry: str = "s1_open",
                         open_wide: pd.DataFrame | None = None) -> pd.DataFrame:
    """未来 horizon 日收益宽表（可切换建仓口径）。

    ⚠ 这是**标签**不是因子：负向移位在这里是定义性前视，见文件头说明。

    口径（T = 因子取值日）：
        entry="close"   ：close[T+h] / close[T] - 1        （T 日收盘成交，仅对照）
        entry="s1_close"：close[T+h] / close[T+1] - 1      （T+1 收盘成交）
        entry="s1_open" ：close[T+h] / open[T+1] - 1       （T+1 开盘成交，默认）

    为什么默认改成 s1_open：因子在 T 日收盘后（拿到完整日线）才算得出，
    实盘最早只能在 T+1 开盘下单。用 close 口径等于假设「同一收盘价既拿到因子
    又完成成交」，对反转类因子（ret1/rev5）的偏差可达十几个百分点。

    Args:
        close_wide: 收盘价宽表（index=date, columns=symbol）。
        horizon: 持有期（交易日），必须 >= 1。
        entry: 建仓口径，取值见 ENTRY_MODES。
        open_wide: 开盘价宽表；entry="s1_open" 时必需。

    Returns:
        与 close_wide 同 index/columns 的收益宽表，无法计算的格子为 NaN。
    """
    step = int(horizon)
    if step < 1:
        raise ValueError("horizon 必须 >= 1，收到 %r" % horizon)
    if entry not in ENTRY_MODES:
        raise ValueError("entry 只支持 %s，收到 %r" % (ENTRY_MODES, entry))
    if entry == "s1_close" and step == 1:
        # 平仓价 = close[T+1]，建仓价也是 close[T+1] → 分子分母同价，收益恒为 0。
        # 这种「静默全 0」会在表格里显示为 +0.000%，比报错危险得多，直接拦掉。
        raise ValueError(
            "entry=s1_close 在 horizon=1 时分子分母同价（恒为 0），请改用 s1_open 或 horizon>=2")

    exit_price = close_wide.shift(-step)              # T+h 收盘（平仓价）

    if entry == "close":
        entry_price = close_wide                      # T 日收盘（同日执行，仅对照）
    elif entry == "s1_close":
        entry_price = close_wide.shift(-1)            # T+1 收盘
    else:                                             # "s1_open"
        if open_wide is None or open_wide.empty:
            raise ValueError("entry='s1_open' 需要 open_wide（开盘价宽表）")
        # 按面板的日期/股票轴对齐：缺失的票留 NaN，绝不 fillna(0)
        aligned = open_wide.reindex(index=close_wide.index, columns=close_wide.columns)
        entry_price = aligned.shift(-1)                # T+1 开盘

    fwd = exit_price / entry_price - 1.0
    return fwd.replace([np.inf, -np.inf], np.nan)


def decay_caliber(entry: str, horizon: int) -> str:
    """IC 衰减各档的持有期口径标注。

    entry="s1_open" 且 horizon=1 时，收益是「T+1 开盘买、T+1 收盘卖」，
    只含日内、不含隔夜；horizon>=2 才含隔夜。两者不能直接比高低，
    所以每一档都要带口径，避免把「口径变窄」误读成「因子在短周期上失效」。
    """
    return "日内" if (entry == "s1_open" and int(horizon) == 1) else "含隔夜"


def load_panel_wide(conn: sqlite3.Connection, factor: str,
                    start: str | None = None, end: str | None = None,
                    symbols: Iterable[str] | None = None) -> pd.DataFrame:
    """取单个因子的面板宽表（index=date，columns=symbol，values=因子值）。

    为什么不用 pd.read_sql + pivot
    ------------------------------
    同一份 646 万行实测：pd.read_sql 163 秒、cursor.fetchall 73 秒、
    pivot 1.5 秒。慢的不是透视，是 pandas 逐行把 SQLite 的 tuple 构造成
    object 列。这里改成直接吃 cursor 的结果、用整数下标写进预分配的 numpy
    矩阵，把单因子耗时压到原来的 45% 左右（面板越大差距越明显）。

    全量查询（不带 start/end/symbols）会先试磁盘缓存：同一份数据被评估、
    选股、分板块三个脚本反复读取，缓存命中时从 116 秒降到 1 秒内。
    """
    if start is None and end is None and symbols is None:
        try:
            import panel_cache
            hit = panel_cache.load(conn, factor)
            if hit is not None:
                return hit
        except Exception:
            pass          # 缓存不可用就当没有，绝不因为它挡住正常取数

    sql = "SELECT date, symbol, value FROM factor_panel WHERE factor = ?"
    params: list[Any] = [factor]
    if start:
        sql += " AND date >= ?"
        params.append(start)
    if end:
        sql += " AND date <= ?"
        params.append(end)
    if symbols is not None:
        symbol_list = list(symbols)
        if not symbol_list:
            return pd.DataFrame()
        sql += " AND symbol IN (%s)" % ",".join("?" * len(symbol_list))
        params.extend(symbol_list)
    sql += " ORDER BY date"

    rows = conn.execute(sql, params).fetchall()
    if not rows:
        return pd.DataFrame()

    d_order: dict[Any, int] = {}
    s_order: dict[Any, int] = {}
    d_get, s_get = d_order.get, s_order.get
    di_l: list[int] = []
    si_l: list[int] = []
    v_l: list[float] = []
    for d, s, v in rows:
        di = d_get(d)
        if di is None:
            di = d_order[d] = len(d_order)
        si = s_get(s)
        if si is None:
            si = s_order[s] = len(s_order)
        di_l.append(di)
        si_l.append(si)
        v_l.append(v)

    mat = np.full((len(d_order), len(s_order)), np.nan, dtype="float64")
    mat[np.asarray(di_l, dtype=np.int64), np.asarray(si_l, dtype=np.int64)] = \
        np.asarray(v_l, dtype="float64")
    # ORDER BY date 保证「首次出现顺序」就是日期升序，不用再 sort
    dates = list(d_order.keys())
    cols = list(s_order.keys())
    return pd.DataFrame(mat, index=pd.Index(dates, name="date"),
                        columns=pd.Index(cols, name="symbol"))


# ────────────────────────────────────────────────────────────
# 核心：逐日 IC（向量化）
# ────────────────────────────────────────────────────────────
def daily_ic(panel_wide: pd.DataFrame, fwd_wide: pd.DataFrame,
             min_stocks: int = MIN_STOCKS) -> pd.Series:
    """逐日横截面 Spearman IC（秩相关，按日在公共样本上计算）。

    做法：先把两边的缺失位置对齐（只在「同一天既有因子值又有未来收益」处比较），
    再按行取秩，最后按行算 Pearson —— 行内缺失由 skipna 处理。

    Args:
        panel_wide: index=date, columns=symbol 的因子值。
        fwd_wide: 同形状的未来收益。
        min_stocks: 当日有效样本下限，不足则丢弃该日。

    Returns:
        Series(index=date, values=ic)，仅含有效日期。
    """
    if panel_wide.empty or fwd_wide.empty:
        return pd.Series(dtype="float64")

    symbols = panel_wide.columns.intersection(fwd_wide.columns)
    dates = panel_wide.index.intersection(fwd_wide.index)
    p = panel_wide.reindex(index=dates, columns=symbols)
    f = fwd_wide.reindex(index=dates, columns=symbols)

    mask = p.notna() & f.notna()
    n = mask.sum(axis=1)
    p = p.where(mask)
    f = f.where(mask)

    pr = p.rank(axis=1)
    fr = f.rank(axis=1)
    dp = pr.sub(pr.mean(axis=1), axis=0)
    df_ = fr.sub(fr.mean(axis=1), axis=0)
    cov = (dp * df_).sum(axis=1, skipna=True)
    sp = np.sqrt((dp * dp).sum(axis=1, skipna=True))
    sf = np.sqrt((df_ * df_).sum(axis=1, skipna=True))
    ic = cov / (sp * sf)
    ic = ic.replace([np.inf, -np.inf], np.nan)
    ic[n < min_stocks] = np.nan
    ic = ic.dropna()
    ic.name = "ic"
    return ic


def group_returns(panel_wide: pd.DataFrame, fwd_wide: pd.DataFrame, direction: int,
                  n_groups: int = N_GROUPS, min_stocks: int = MIN_STOCKS
                  ) -> tuple[pd.DataFrame, pd.Series]:
    """十分组收益。

    统一「组号语义」是这一步的关键，否则每次看分组都要人工回忆方向：
        · 先把因子值按 direction 归正（direction=-1 时乘 -1），使 adj 越大 = 预期越好；
        · 按 adj 从大到小排列，第 1 组 = 预期最好，第 n_groups 组 = 预期最差。
    于是无论因子方向如何，「第 1 组」永远可以直接解读为「预测未来最强的组」。

    返回:
        (groups_df, top_members)
        groups_df: index=date，columns=1..n_groups，值为该组当日等权平均未来收益
        top_members: Series(index=date, values=第 1 组的成分 symbol 集合)，用于算换手
    """
    symbols = panel_wide.columns.intersection(fwd_wide.columns)
    dates = panel_wide.index.intersection(fwd_wide.index)
    p = panel_wide.reindex(index=dates, columns=symbols)
    f = fwd_wide.reindex(index=dates, columns=symbols)
    mask = p.notna() & f.notna()
    n = mask.sum(axis=1)
    keep = n >= min_stocks
    p = p.where(mask)[keep]
    f = f.where(mask)[keep]

    if p.empty:
        return pd.DataFrame(columns=list(range(1, n_groups + 1))), pd.Series(dtype="object")

    # direction 归正：adj 越大 = 预期越好
    adj = p * (1.0 if int(direction) >= 0 else -1.0)
    # 按行升序排名，pct=True 得到 (0,1]；数值越大 → pct 越接近 1 → 组号越小
    rk = adj.rank(axis=1, pct=True, na_option="keep")
    grp = np.ceil((1.0 - rk) * n_groups + 1e-9).clip(1, n_groups)

    groups = pd.DataFrame(index=p.index, columns=list(range(1, n_groups + 1)), dtype="float64")
    top_members: dict[str, set[str]] = {}
    for g in range(1, n_groups + 1):
        member = grp == g
        groups[g] = f.where(member).mean(axis=1, skipna=True)
    member1 = grp == 1
    for date in p.index:
        row = member1.loc[date]
        cols = row.index[row.to_numpy(dtype=bool)].tolist() if row.any() else []
        top_members[date] = set(cols)

    return groups, pd.Series(top_members, dtype="object")


def _group_turnover(members: pd.Series) -> float | None:
    """Top 组相邻期成分换手：1 − Jaccard 重叠率，取各期平均。"""
    values = [s for s in members.tolist() if isinstance(s, set) and s]
    if len(values) < 2:
        return None
    scores: list[float] = []
    for prev, cur in zip(values[:-1], values[1:]):
        union = prev | cur
        if not union:
            continue
        inter = prev & cur
        scores.append(1.0 - len(inter) / len(union))
    return float(np.mean(scores)) if scores else None


# ────────────────────────────────────────────────────────────
# 单因子评估
# ────────────────────────────────────────────────────────────
def evaluate_factor(panel_wide: pd.DataFrame, fwd_wide: pd.DataFrame,
                    direction: int = 1, horizon: int = 5,
                    n_groups: int = N_GROUPS, min_stocks: int = MIN_STOCKS,
                    universe_wide: pd.DataFrame | None = None) -> dict[str, Any]:
    """评估单个因子，返回完整指标字典。

    Args:
        panel_wide: index=date, columns=symbol 的因子值。
        fwd_wide: 同形状的未来 horizon 日收益（口径由 load_forward_returns 的 entry 决定）。
        direction: +1 / -1，先验方向（见 factors.registry）。
        horizon: 收益周期（交易日），同时决定 Newey-West 的 lag = max(0, horizon-1)。
        n_groups: 分组数，默认 10。
        min_stocks: 逐日最小有效样本。
        universe_wide: 收盘价宽表（非空=当日有行情），**应传全市场**：
            同时用于两个覆盖率口径——
              panel_completeness = 因子有值 / 面板股票池当日有行情（交集列）
              market_coverage    = 因子有值 / 全市场当日有行情（全部列）
            None 则两者都是 None。
    """
    lag = max(0, int(horizon) - 1)          # 重叠长度 = h-1；h=1 无重叠 → lag=0（见下）
    empty: dict[str, Any] = {
        "n_days": 0, "n_obs": 0,
        "rank_ic": {"mean": None, "std": None, "icir": None,
                    "t_value": None, "p_value": None,
                    "t_nw": None, "p_value_nw": None, "nw_lag": lag,
                    "positive_ratio": None, "n_days": 0},
        "groups": [], "monotonicity": None,
        "long_short": {"mean": None, "annualized": None, "win_ratio": None},
        "turnover": None,
        "panel_completeness": None, "market_coverage": None,
        "direction_note": None, "direction_sign_mismatch": False, "ic_series": [],
    }
    if panel_wide.empty or fwd_wide.empty:
        return empty

    ic = daily_ic(panel_wide, fwd_wide, min_stocks=min_stocks)
    n_days = int(len(ic))
    ic_mean = float(ic.mean()) if n_days else None
    ic_std = float(ic.std(ddof=1)) if n_days > 1 else None
    icir = float(ic_mean / ic_std) if (ic_mean is not None and ic_std) else None
    # 朴素 t：把重叠标签当成独立样本，会虚高约 sqrt(h) 倍，仅作对照
    t_value = (float(ic_mean / ic_std * math.sqrt(n_days))
               if (ic_mean is not None and ic_std) else None)
    # Newey-West 修正 t：主判定
    t_nw = newey_west_t(ic.to_numpy(), lag) if n_days >= 3 else float("nan")
    t_nw = float(t_nw) if np.isfinite(t_nw) else None
    positive = float((ic > 0).mean()) if n_days else None

    groups, members = group_returns(panel_wide, fwd_wide, direction,
                                    n_groups=n_groups, min_stocks=min_stocks)
    group_stats: list[dict[str, Any]] = []
    group_means: list[float] = []
    for g in range(1, n_groups + 1):
        col = groups[g] if g in groups.columns else pd.Series(dtype="float64")
        valid = col.dropna()
        mean = float(valid.mean()) if len(valid) else float("nan")
        group_means.append(mean)
        group_stats.append({
            "group": g,
            "label": "预期最好" if g == 1 else ("预期最差" if g == n_groups else ""),
            "mean": None if not np.isfinite(mean) else mean,
            "annualized": _annualize(mean, horizon) if np.isfinite(mean) else None,
            "n_days": int(len(valid)),
        })

    # 组号 1 = 预期最好，收益应最高 → 与组号负相关；取反后正数表示「越靠前越好」。
    # 定义只有一处（monotonicity_of），滚动验证也调它，禁止在此处另写一份。
    monotonicity = monotonicity_of(group_means)

    # 多空：第 1 组（预期最好）− 第 n_groups 组（预期最差）
    ls_series = (groups[1] - groups[n_groups]).dropna() if (
        1 in groups.columns and n_groups in groups.columns) else pd.Series(dtype="float64")
    ls_mean = float(ls_series.mean()) if len(ls_series) else None
    win_ratio = float((ls_series > 0).mean()) if len(ls_series) else None

    turnover = _group_turnover(members)

    # 两个覆盖率口径，别再混成一个字段（QA 实测两者能差 20 倍）：
    #   panel_completeness：因子在自己池子里缺不缺（分母=面板股票池当日有行情数）
    #   market_coverage   ：这个面板代表了多少全市场（分母=全市场当日有行情数）
    panel_completeness = None
    market_coverage = None
    if universe_wide is not None and not universe_wide.empty:
        cols = panel_wide.columns
        dates = panel_wide.index
        counted = panel_wide.notna().sum(axis=1).astype("float64")
        pool_total = (universe_wide.reindex(index=dates, columns=cols).notna()
                      .sum(axis=1).astype("float64"))
        market_total = universe_wide.reindex(index=dates).notna().sum(axis=1).astype("float64")

        def _ratio(num: pd.Series, den: pd.Series) -> float | None:
            ratio = (num / den.where(den > 0)).replace([np.inf, -np.inf], np.nan).dropna()
            ratio = ratio[ratio <= 1.0]      # 理论上不会 >1，出现说明数据有重复，剔除
            return float(ratio.mean()) if len(ratio) else None

        panel_completeness = _ratio(counted, pool_total)
        market_coverage = _ratio(counted, market_total)

    # 方向提示，分两档——「反号且显著」比「反号且不显著」严重得多：
    #   显著的负 IC + direction=+1 → 若还按先验把第 1 组标成「预期最好」，
    #   等于把最差的一组推荐给用户（实测 slope20 第 1 组年化 -23.8%）。
    direction_note = None
    direction_sign_mismatch = False
    if ic_mean is not None and np.isfinite(ic_mean) and ic_mean != 0:
        direction_sign_mismatch = (ic_mean > 0) != (int(direction) > 0)
        if direction_sign_mismatch:
            if t_nw is None or abs(t_nw) < 2.0:
                direction_note = "方向与先验相反且不显著，勿按 direction 解读预期最好组"
            else:
                direction_note = ("方向与先验相反且显著（t_NW=%+.2f），请复核 registry.direction，"
                                  "当前分组语义可能整体反向" % t_nw)

    # 有效样本总数：同一天既有因子值又有未来收益的格子数
    common_dates = panel_wide.index.intersection(fwd_wide.index)
    common_symbols = panel_wide.columns.intersection(fwd_wide.columns)
    mask_obs = (panel_wide.reindex(index=common_dates, columns=common_symbols).notna()
                & fwd_wide.reindex(index=common_dates, columns=common_symbols).notna())

    return {
        "n_days": n_days,
        "n_obs": int(mask_obs.to_numpy().sum()),
        "rank_ic": {
            "mean": ic_mean,
            "std": ic_std,
            "icir": icir,
            "t_value": t_value,                       # 朴素 t（对照，勿用于判定）
            "p_value": None if t_value is None else normal_p_value(t_value),
            "t_nw": t_nw,                             # NW 修正 t（主判定）
            "p_value_nw": None if t_nw is None else normal_p_value(t_nw),
            "nw_lag": lag,
            "positive_ratio": positive,
            "n_days": n_days,
        },
        "groups": group_stats,
        "monotonicity": monotonicity,
        "long_short": {
            "mean": ls_mean,
            "annualized": _annualize(ls_mean, horizon) if ls_mean is not None else None,
            "win_ratio": win_ratio,
        },
        "turnover": turnover,
        "panel_completeness": panel_completeness,
        "market_coverage": market_coverage,
        "direction_note": direction_note,
        "direction_sign_mismatch": direction_sign_mismatch,
        "ic_series": [[str(d), float(v)] for d, v in ic.items()],
    }


# ────────────────────────────────────────────────────────────
# 因子冗余：逐日横截面相关的均值
# ────────────────────────────────────────────────────────────
def daily_cross_corr(a: pd.DataFrame, b: pd.DataFrame) -> pd.Series:
    """两个因子逐日横截面 Pearson 相关。

    做法：按日（行）标准化后逐元素相乘再按日取均值 —— 数学上等于当日横截面
    Pearson 相关；缺失值按「跳过」处理（与成对删除略有差异，查冗余足够）。
    """
    symbols = a.columns.intersection(b.columns)
    dates = a.index.intersection(b.index)
    a = a.reindex(index=dates, columns=symbols)
    b = b.reindex(index=dates, columns=symbols)
    both = a.notna() & b.notna()
    n = both.sum(axis=1)

    def _z(frame: pd.DataFrame) -> pd.DataFrame:
        mean = frame.mean(axis=1)
        std = frame.std(axis=1, ddof=0)
        return frame.sub(mean, axis=0).div(std, axis=0)

    prod = (_z(a) * _z(b)).where(both)
    corr = prod.sum(axis=1, skipna=True) / n.where(n > 0)
    corr = corr.replace([np.inf, -np.inf], np.nan)
    return corr[n >= MIN_STOCKS].dropna()


def corr_matrix(panels: dict[str, pd.DataFrame],
                max_dates: int = 60) -> dict[str, dict[str, float | None]]:
    """因子两两相关矩阵（日度横截面相关的平均）。

    Args:
        panels: factor -> 宽表。
        max_dates: 最多取多少个交易日（等距抽样）。查冗余不需要全样本，
            抽样能把内存和耗时压到可接受范围；默认 60 天足够稳定。
    """
    names = [n for n in panels if not panels[n].empty]
    out: dict[str, dict[str, float | None]] = {n: {} for n in names}

    # 统一日期轴并等距抽样
    all_dates = None
    for n in names:
        idx = panels[n].index
        all_dates = idx if all_dates is None else all_dates.intersection(idx)
    if all_dates is None or len(all_dates) == 0:
        for n in names:
            for m in names:
                out[n][m] = None
        return out

    dates = list(all_dates.sort_values())
    if max_dates and len(dates) > max_dates:
        step = len(dates) / float(max_dates)
        dates = [dates[int(i * step)] for i in range(max_dates)]
    sampled = {n: panels[n].reindex(index=dates) for n in names}

    for i, n in enumerate(names):
        for m in names[i:]:
            if n == m:
                out[n][m] = 1.0
                continue
            corr = daily_cross_corr(sampled[n], sampled[m])
            value = float(corr.mean()) if len(corr) else None
            out[n][m] = value
            out[m][n] = value
    return out


# ────────────────────────────────────────────────────────────
# 总流程
# ────────────────────────────────────────────────────────────
def run(db_path: str = DB,
        factors: Iterable[str] | None = None,
        horizon: int = 5,
        start: str | None = None,
        end: str | None = None,
        out: str | None = DEFAULT_OUT,
        n_groups: int = N_GROUPS,
        min_stocks: int = MIN_STOCKS,
        horizons: Sequence[int] = (1, 5, 10, 20),
        do_corr: bool = True,
        symbols: Iterable[str] | None = None,
        entry: str = "s1_open",
        verbose: bool = True) -> dict[str, Any]:
    """跑完整个因子评估流程，返回结果字典（并可选写 JSON）。

    Args:
        entry: 建仓口径（s1_open / s1_close / close），默认 s1_open。
            所有 horizon 的标签都用同一个 entry，避免 IC 衰减图里口径漂移。
        horizons: IC 衰减用的周期列表。

    Returns:
        {
          "meta": {...}, "factors": {name: {...指标...}}, "corr_matrix": {...},
          "fdr": {name: {"t_nw","p","q","t_naive","p_naive","q_naive"}}
        }
    """
    factor_names = list(factors) if factors else registry.all_names()
    for name in factor_names:
        registry.get(name)
    if int(horizon) < 1:
        raise ValueError("horizon 必须 >= 1，收到 %r" % horizon)
    if entry not in ENTRY_MODES:
        raise ValueError("entry 只支持 %s，收到 %r" % (ENTRY_MODES, entry))

    t_start = pd.Timestamp.now()
    conn = sqlite3.connect(db_path, timeout=60)
    try:
        close_wide = load_close_wide(conn, symbols)
        if close_wide.empty:
            raise SystemExit("stock_daily 无数据，先跑 bulk_backfill.py")
        open_wide = load_open_wide(conn, symbols)
        fwd_by_h = {h: load_forward_returns(close_wide, h, entry=entry, open_wide=open_wide)
                    for h in horizons}

        directions = registry.directions()
        panels: dict[str, pd.DataFrame] = {}
        results: dict[str, Any] = {}

        for name in factor_names:
            if verbose:
                print("  评估 %-18s" % name, end="", flush=True)
            wide = load_panel_wide(conn, name, start, end, symbols)
            if wide.empty:
                results[name] = {"error": "面板无数据"}
                if verbose:
                    print("  面板无数据", flush=True)
                continue
            # 相关矩阵只用最近一段估计（相关是局部属性），因此不保留全样本宽表：
            # 24 个因子 × 1420 天 × 5030 只全留在内存里会到 GB 级，是纯浪费。
            panels[name] = wide.iloc[-CORR_KEEP:] if len(wide) > CORR_KEEP else wide
            fwd = fwd_by_h.get(horizon)
            if fwd is None:
                fwd = load_forward_returns(close_wide, horizon, entry=entry,
                                           open_wide=open_wide)
                fwd_by_h[horizon] = fwd

            res = evaluate_factor(wide, fwd, direction=directions[name], horizon=horizon,
                                  n_groups=n_groups, min_stocks=min_stocks,
                                  universe_wide=close_wide)
            # IC 衰减：其余 horizon 只算 IC 均值，不重复算分组（省时间）
            decay: dict[str, float | None] = {}
            for h in horizons:
                f_h = fwd_by_h.get(h)
                if f_h is None:
                    f_h = load_forward_returns(close_wide, h, entry=entry,
                                               open_wide=open_wide)
                    fwd_by_h[h] = f_h
                ic_h = daily_ic(wide, f_h, min_stocks=min_stocks)
                decay[str(h)] = float(ic_h.mean()) if len(ic_h) else None
            res["ic_decay"] = decay
            # 口径标注：h=1 是日内（不含隔夜），h>=2 含隔夜，两者不能直接比高低
            res["ic_decay_caliber"] = {str(h): decay_caliber(entry, h) for h in horizons}
            results[name] = res
            if verbose:
                ic_mean = res["rank_ic"]["mean"]
                t_nw = res["rank_ic"]["t_nw"]
                print("  IC=%-9s t_nw=%-7s 天=%3d" % (
                    "None" if ic_mean is None else "%+.4f" % ic_mean,
                    "None" if t_nw is None else "%+.2f" % t_nw, res["n_days"]), flush=True)

        # 冗余矩阵
        matrix: dict[str, dict[str, float | None]] = {}
        if do_corr and len(panels) >= 2:
            if verbose:
                print("  计算因子相关矩阵（抽样日期）…", flush=True)
            matrix = corr_matrix(panels)

        # BH-FDR：主判定用 Newey-West 的 p 值，朴素 p 的 q 值保留为 q_naive 供对照
        names_ok = [n for n in factor_names
                    if isinstance(results.get(n), dict) and results[n].get("rank_ic")]
        t_nw_values: list[float] = []
        t_naive_values: list[float] = []
        for n in names_ok:
            t_nw = results[n]["rank_ic"]["t_nw"]
            t_naive = results[n]["rank_ic"]["t_value"]
            t_nw_values.append(float(t_nw) if t_nw is not None and np.isfinite(t_nw)
                               else float("nan"))
            t_naive_values.append(float(t_naive) if t_naive is not None
                                  and np.isfinite(t_naive) else float("nan"))

        p_nw = [normal_p_value(t) for t in t_nw_values]
        p_naive = [normal_p_value(t) for t in t_naive_values]
        q_nw = bh_fdr(p_nw)
        q_naive = bh_fdr(p_naive)
        fdr: dict[str, dict[str, float | None]] = {}
        for i, n in enumerate(names_ok):
            fdr[n] = {
                "t_nw": None if not np.isfinite(t_nw_values[i]) else t_nw_values[i],
                "p": None if not np.isfinite(p_nw[i]) else float(p_nw[i]),
                "q": None if not np.isfinite(q_nw[i]) else float(q_nw[i]),
                "t_naive": None if not np.isfinite(t_naive_values[i]) else t_naive_values[i],
                "p_naive": None if not np.isfinite(p_naive[i]) else float(p_naive[i]),
                "q_naive": None if not np.isfinite(q_naive[i]) else float(q_naive[i]),
            }

        meta = {
            "db": db_path,
            "start": start,
            "end": end,
            "horizon": horizon,
            "n_groups": n_groups,
            "min_stocks": min_stocks,
            "factors": factor_names,
            "entry": entry,
            "entry_note": ENTRY_NOTES[entry],
            "entry_note_h1": "horizon=1 为日内口径（T+1 开盘买、T+1 收盘卖，不含隔夜），"
                             "与 horizon>=2 不可直接比较",
            "stat_note": "t/q 的主判定为 Newey-West 修正值（t_nw/q），lag = max(0, horizon-1)；"
                         "t_naive/q_naive 为重叠标签未修正的对照值",
            "generated_at": t_start.strftime("%Y-%m-%d %H:%M:%S"),
            "elapsed_sec": round((pd.Timestamp.now() - t_start).total_seconds(), 2),
        }
        payload: dict[str, Any] = {
            "meta": meta, "factors": results, "corr_matrix": matrix, "fdr": fdr,
        }

        if out:
            os.makedirs(os.path.dirname(os.path.abspath(out)), exist_ok=True)
            with open(out, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=False, indent=2)
            if verbose:
                print("  已写 %s" % out, flush=True)
        return payload
    finally:
        conn.close()


# ────────────────────────────────────────────────────────────
# 打印
# ────────────────────────────────────────────────────────────
def print_table(payload: dict[str, Any]) -> None:
    """把结果打印成一张可读表格（不依赖 rich，终端直接能看）。

    两个 t 并列显示：t_naive 是重叠标签未修正值（会虚高 sqrt(h) 倍），
    t_NW / q_NW 才是判定依据。表尾会写明这一点，避免表格被误读。
    """
    meta = payload.get("meta", {})
    header = ("%-18s %-10s %8s %7s %8s %8s %8s %7s %8s %7s %7s %7s"
              % ("因子", "分类", "IC均值", "t朴素", "t_NW", "p_NW", "q_NW",
                 "单调性", "多空年化", "换手", "完整度", "覆盖"))
    print("\n因子评估（horizon=%s 交易日 | entry=%s | %s ~ %s）"
          % (meta.get("horizon"), meta.get("entry", "?"),
             meta.get("start") or "最早", meta.get("end") or "最新"))
    print("建仓口径：%s" % meta.get("entry_note", "-"))
    print("-" * len(header))
    print(header)
    print("-" * len(header))

    def _fmt(value: float | None, fmt: str = "%.4f") -> str:
        return "-" if value is None else fmt % value

    for name, res in payload.get("factors", {}).items():
        if "error" in res:
            continue
        d = registry.get(name)
        ic = res["rank_ic"]
        fdr = payload.get("fdr", {}).get(name, {})
        print("%-18s %-10s %8s %7s %8s %8s %8s %7s %8s %7s %7s %7s"
              % (name, registry.CATEGORY_LABELS[d["category"]],
                 _fmt(ic["mean"]),
                 _fmt(ic["t_value"], "%+.2f"),
                 _fmt(ic["t_nw"], "%+.2f"),
                 _fmt(ic.get("p_value_nw"), "%.4f"),
                 _fmt(fdr.get("q"), "%.4f"),
                 _fmt(res["monotonicity"], "%+.2f"),
                 "-" if res["long_short"]["annualized"] is None
                 else "%+.1f%%" % (res["long_short"]["annualized"] * 100),
                 _fmt(res["turnover"], "%.2f"),
                 "-" if res.get("panel_completeness") is None
                 else "%.1f%%" % (res["panel_completeness"] * 100),
                 "-" if res.get("market_coverage") is None
                 else "%.1f%%" % (res["market_coverage"] * 100)))

    if payload.get("fdr"):
        sig = [n for n, v in payload["fdr"].items()
               if v.get("q") is not None and v["q"] < 0.05]
        sig_naive = [n for n, v in payload["fdr"].items()
                     if v.get("q_naive") is not None and v["q_naive"] < 0.05]
        print("-" * len(header))
        print("BH-FDR（NW 口径，主判定）q<0.05：%d 个 %s"
              % (len(sig), ", ".join(sig) if sig else "无"))
        print("BH-FDR（朴素 t，仅供对照，勿用于判定）q<0.05：%d 个" % len(sig_naive))

    print("口径说明：t_NW 的 lag = max(0, horizon-1)；"
          "ic_decay 中 h=1 为日内（T+1 开盘买、T+1 收盘卖，不含隔夜），"
          "与 h>=2 不可直接比较。")
    if meta.get("horizon") == 1:
        print("注意：本次 horizon=1，收益为**日内**口径，与 h>=2 的「含隔夜」结果不可混用。")
    if meta.get("entry") == "close":
        print("注意：本次 entry=close 是「T 日收盘同日成交」的对照口径，实盘做不到，勿据此下结论。")


def _num(value: float | None, fmt: str = "%+.4f") -> str:
    """数值格式化：None / NaN / inf 一律显示 "-"，与 print_table 保持一致。

    绝不能用 `value or 0.0` 这种写法：None 会被打成 "+0.0000"，
    读者会以为「IC 真的是 0」，而实际情况是「根本算不出 IC」。
    """
    if value is None:
        return "-"
    try:
        fvalue = float(value)
    except (TypeError, ValueError):
        return "-"
    if not np.isfinite(fvalue):
        return "-"
    return fmt % fvalue


def print_single(payload: dict[str, Any], name: str) -> None:
    """单因子明细：两个 t 并列 + 十分组 + IC 衰减 + 覆盖率两口径 + 相关最高。"""
    res = payload["factors"].get(name)
    if not res or "error" in res:
        print("  %s 无结果：%s" % (name, res))
        return
    d = registry.get(name)
    ic = res["rank_ic"]
    print("\n%s（%s / direction=%+d / %s）"
          % (name, d["label"], d["direction"], d["formula_desc"]))
    print("  IC均值 %s | ICIR %s | 有效天数 %d | 样本 %s | 正IC %s"
          % (_num(ic["mean"]), _num(ic["icir"], "%+.3f"), res["n_days"],
             f'{res["n_obs"]:,}', _num(None if ic["positive_ratio"] is None
                                      else ic["positive_ratio"] * 100, "%.1f%%")))
    print("  t 值：朴素 %s (p=%s)  |  Newey-West(lag=%s) %s (p=%s)  ← 判定用后者"
          % (_num(ic["t_value"], "%+.2f"),
             _num(ic.get("p_value"), "%.4f"),
             ic.get("nw_lag"),
             _num(ic["t_nw"], "%+.2f"),
             _num(ic.get("p_value_nw"), "%.4f")))

    note = res.get("direction_note")
    if note:
        print("  ⚠ %s" % note)
    reversed_sign = bool(res.get("direction_sign_mismatch"))

    # 反号时不能再写「按 direction 判断的预期最好组」，否则等于把最差组标成最好组
    if reversed_sign:
        print("  十分组（IC 与先验方向相反：组号仅表按先验方向的排序，第 1 组未必最好）：")
    else:
        print("  十分组（第 1 组 = 按 direction 判断的预期最好组）：")
    for g in res["groups"]:
        label = g["label"]
        if reversed_sign and label == "预期最好":
            label = "按先验"
        elif reversed_sign and label == "预期最差":
            label = "按先验"
        print("     组%-2d %-6s 平均 %8s  年化 %9s  天数 %d"
              % (g["group"], label,
                 _num(None if g["mean"] is None else g["mean"] * 100, "%+7.3f%%"),
                 _num(None if g["annualized"] is None else g["annualized"] * 100, "%+8.2f%%"),
                 g["n_days"]))

    ls_ann = res["long_short"]["annualized"]
    print("  单调性 %s | 多空年化 %s | 换手 %s"
          % (_num(res["monotonicity"], "%+.3f"),
             _num(None if ls_ann is None else ls_ann * 100, "%+.2f%%"),
             _num(res["turnover"], "%.3f")))
    if ls_ann is not None and ls_ann < 0 and not reversed_sign:
        print("  注：按先验方向的多空为负（%.2f%%），说明实际方向与登记相反，"
              "不宜直接当多头/空头信号使用" % (ls_ann * 100))
    print("  覆盖：面板完整度 %s（自己池子缺不缺） | 市场覆盖 %s（面板代表多少全市场）"
          % (_num(None if res.get("panel_completeness") is None
                  else res["panel_completeness"] * 100, "%.2f%%"),
             _num(None if res.get("market_coverage") is None
                  else res["market_coverage"] * 100, "%.2f%%")))
    decay = res.get("ic_decay") or {}
    caliber = res.get("ic_decay_caliber") or {}
    print("  IC 衰减：%s" % "  ".join(
        "h=%s:%s%s" % (h, "-" if v is None else "%+.4f" % v,
                       "(%s)" % caliber[h] if caliber.get(h) == "日内" else "")
        for h, v in decay.items()))
    if caliber.get("1") == "日内":
        print("  注：h=1 为日内口径（不含隔夜），与 h>=2 不可直接比较高低")

    matrix = payload.get("corr_matrix") or {}
    if matrix.get(name):
        pairs = [(abs(v), k, v) for k, v in matrix[name].items() if k != name and v is not None]
        pairs.sort(reverse=True)
        if pairs:
            print("  相关最高：%s" % ", ".join("%s %+.2f" % (k, v) for _, k, v in pairs[:5]))


def main() -> int:
    ap = argparse.ArgumentParser(description="因子评估（IC / 分组 / 多空 / 换手 / FDR）")
    ap.add_argument("--factor", default=None, help="逗号分隔的因子名；默认全部")
    ap.add_argument("--horizon", type=int, default=5, help="收益周期（交易日，>=1），默认 5")
    ap.add_argument("--start", default=None, help="评估起始日期 YYYY-MM-DD")
    ap.add_argument("--end", default=None, help="评估结束日期 YYYY-MM-DD")
    ap.add_argument("--out", default=DEFAULT_OUT, help="JSON 输出路径")
    ap.add_argument("--groups", type=int, default=N_GROUPS, help="分组数，默认 10")
    ap.add_argument("--min-stocks", type=int, default=MIN_STOCKS, help="逐日最小有效样本")
    ap.add_argument("--horizons", default="1,5,10,20", help="IC 衰减用的周期列表")
    ap.add_argument("--entry", choices=list(ENTRY_MODES), default="s1_open",
                    help="建仓口径，默认 s1_open（T+1 开盘成交）；close 仅作对照")
    ap.add_argument("--no-corr", action="store_true", help="跳过因子相关矩阵（更快）")
    ap.add_argument("--db", default=DB, help="数据库路径")
    args = ap.parse_args()

    # 提前拦住非法 horizon：让分数在参数校验阶段就失败，而不是在算年化时炸
    if args.horizon < 1:
        raise SystemExit("--horizon 必须 >= 1，收到 %d" % args.horizon)
    if args.groups < 2:
        raise SystemExit("--groups 必须 >= 2，收到 %d" % args.groups)

    factors = [x.strip() for x in args.factor.split(",") if x.strip()] if args.factor else None
    horizons = tuple(int(x) for x in args.horizons.split(",") if x.strip())
    if any(h < 1 for h in horizons):
        raise SystemExit("--horizons 里的周期必须都 >= 1，收到 %s" % (horizons,))

    try:
        payload = run(db_path=args.db, factors=factors, horizon=args.horizon,
                      start=args.start, end=args.end, out=args.out,
                      n_groups=args.groups, min_stocks=args.min_stocks,
                      horizons=horizons, do_corr=not args.no_corr,
                      entry=args.entry, verbose=True)
    except ValueError as exc:            # 参数组合非法（如 s1_close + h=1），干净退出
        print("参数错误：%s" % exc, file=sys.stderr)
        return 2

    print_table(payload)
    targets = factors or [n for n in payload["factors"] if "error" not in payload["factors"][n]]
    if len(targets) <= 3:                 # 因子少的时候把明细也打出来
        for name in targets:
            print_single(payload, name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
