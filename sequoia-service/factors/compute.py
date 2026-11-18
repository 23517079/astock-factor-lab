#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factors.compute — 因子计算（逐日时间序列 + 最新截面取值）

════════════════════════════════════════════════════════════════════
前视偏差铁律（本文件最重要的一条，改代码前先读）
────────────────────────────────────────────────────────────────────
只允许使用「截至当日（含当日）」的信息。落地为三条硬约束：

  1. 禁止 shift(负数)：只允许 shift(正数)，正数 = 往回看；
  2. 禁止 iloc[i+1] / series[j] 这类向未来索引；
  3. 禁止 bfill() / interpolate() 这类用未来值回填空值。

所有窗口统计一律用 rolling(...)（天然右对齐，窗口尾部 = 当日），
所有取值一律取窗口**最后**一个值。想验证的话，tests/test_factors.py 第 1 组
会把最后 50 日改成极端值，要求前 250 日的因子值一字不变。

接口设计：
    compute_series(df, names) -> DataFrame  # 逐日时间序列（评估/调试/画图）
    compute_one(df, names)    -> dict       # 最新一日截面（落库用）

compute_one 就是 compute_series 的最后一行，二者共用同一份计算逻辑，
因此不可能出现「落库的值和回测的值不是一套公式」这种经典事故。

════════════════════════════════════════════════════════════════
新增因子的硬约束（2026-11-16 增量面板上线后，改代码前先读）
────────────────────────────────────────────────────────────────
面板增量重算只读「目标日期往前 WARMUP_BARS=300 根 K 线」（见 panel_incr.py），
而不是读全历史。这能成立的前提是：**因子的取值只依赖长度有界的因果窗口，
或用到的绝对水平对「起点变化」不敏感**。因此：

  * 新增因子必须满足下面任意一条，否则**必须走全量重算**（rebuild_panel.py）：
      (a) 只用 rolling(窗口) / shift(常数) / diff 这类窗口有界的运算；
          且窗口 ≤ 250 日（dist_high250 的窗口，也是当前最大值）；
      (b) 用到了 cumsum / cummax 这类**依赖起点**的累计量，但因子取值
          对常数偏移不变。例：`obv_slope20` 用 cumsum 算 OBV，OBV 的绝对水平
          随截断起点变化，但因子取的是 20 日 OLS 的**斜率**，对常数偏移不变，
          所以截断等价（T02 的比对用例强制覆盖它，用实测证明而非推理）。
  * 反例（禁止直接加进增量路径）：`close / close.iloc[0]`（依赖首个样本）、
    `(obv - obv.min())`（依赖全序列极值，不是常数偏移）、
    任何 `expanding()` 后取绝对水平、任何全样本 z-score / 分位数。
  * 新增因子若窗口 > 250 日，必须同步调大 panel_incr.WARMUP_BARS，
    否则增量结果与全量重算不等价（check_panel 第 3 层会抓出来）。
════════════════════════════════════════════════════════════════
"""

from __future__ import annotations

from typing import Any, Callable, Iterable

import numpy as np
import pandas as pd

from factors import registry

# 年化系数：A 股一年约 244 个交易日，这里沿用业内常用的 252（对横截面排序影响可忽略）
TRADING_DAYS = 252

# 计算所需的基础列
_REQUIRED_COLS: tuple[str, ...] = ("date", "open", "high", "low", "close", "volume", "turnover")


# ────────────────────────────────────────────────────────────
# 小工具
# ────────────────────────────────────────────────────────────
def _ret(close: pd.Series) -> pd.Series:
    """日收益：close[t]/close[t-1]-1。

    手写而不用 pct_change()：pandas 各版本对 fill_method 的默认值反复横跳，
    手写既避免版本差异，也明摆着写出「只往前看一格」。
    """
    return close / close.shift(1) - 1.0


def _linreg(y: pd.Series, n: int) -> tuple[pd.Series, pd.Series]:
    """窗口 n 内 y 对「时间序号」做 OLS，返回 (slope, r2)。

    用 rolling 和公式展开（不依赖 scipy/statsmodels）：
        slope = (n*Σxy - Σx*Σy) / (n*Σx² - (Σx)²)
        r2    = (n*Σxy - Σx*Σy)² / ((n*Σx²-(Σx)²) * (n*Σy²-(Σy)²))

    x 取绝对行号而非「窗口内 0..n-1」：斜率与 R² 对 x 的平移不变
    （分子分母同时消掉平移项），因此可以安全地对绝对行号做 rolling 求和。
    窗口尾部即当日，仍然只用到历史。
    """
    idx = pd.Series(np.arange(len(y), dtype="float64"), index=y.index)
    m = n
    mp = n                                   # 必须满窗口，否则斜率没有意义
    sy = y.rolling(m, min_periods=mp).sum()
    sxy = (y * idx).rolling(m, min_periods=mp).sum()
    sxx = (idx * idx).rolling(m, min_periods=mp).sum()
    sx = idx.rolling(m, min_periods=mp).sum()
    syy = (y * y).rolling(m, min_periods=mp).sum()

    denom = m * sxx - sx * sx                # 常数 m²(m²-1)/12 > 0
    num = m * sxy - sx * sy
    slope = num / denom
    denom_y = m * syy - sy * sy
    r2 = (num * num) / (denom * denom_y)
    # 分母为 0（窗口内价格完全不变）时得到 inf/nan，一律视为无效
    r2 = r2.replace([np.inf, -np.inf], np.nan).clip(lower=0.0, upper=1.0)
    return slope, r2


def _safe_div(a: pd.Series, b: pd.Series) -> pd.Series:
    """除法并屏蔽 inf（分母为 0 时 pandas 给 inf，会让后续排序出错）。"""
    out = a / b
    return out.replace([np.inf, -np.inf], np.nan)


# ────────────────────────────────────────────────────────────
# 各因子计算器：输入 ctx（基础序列字典），输出与 df 等长的 Series
# ────────────────────────────────────────────────────────────
def _f_mom(skip: int, n: int) -> Callable[[dict[str, pd.Series]], pd.Series]:
    """动量：跳过最近 skip 日，再取 n 日涨幅（close[t-skip]/close[t-skip-n]-1）。"""

    def _calc(ctx: dict[str, pd.Series]) -> pd.Series:
        close = ctx["close"]
        return close.shift(skip) / close.shift(skip + n) - 1.0

    return _calc


def _f_rev(n: int) -> Callable[[dict[str, pd.Series]], pd.Series]:
    """反转：过去 n 日收益（原始值，方向由 registry.direction 表达）。"""

    def _calc(ctx: dict[str, pd.Series]) -> pd.Series:
        close = ctx["close"]
        return close / close.shift(n) - 1.0

    return _calc


def _asm(series: pd.Series, n: int) -> pd.Series:
    """窗口 n 的均值，要求满窗口（不足即 NaN，由 min_bars 兜底）。"""
    return series.rolling(n, min_periods=n).mean()


def _asd(series: pd.Series, n: int) -> pd.Series:
    """窗口 n 的标准差（样本标准差 ddof=1），要求满窗口。"""
    return series.rolling(n, min_periods=n).std(ddof=1)


def _calc_vol20(ctx: dict[str, pd.Series]) -> pd.Series:
    return _asd(ctx["ret"], 20) * np.sqrt(TRADING_DAYS)


def _calc_vol_ratio_20_60(ctx: dict[str, pd.Series]) -> pd.Series:
    return _safe_div(_asd(ctx["ret"], 20), _asd(ctx["ret"], 60))


def _calc_atr_pct14(ctx: dict[str, pd.Series]) -> pd.Series:
    """ATR(14)/close。TR = max(H-L, |H-C_prev|, |L-C_prev|)，只用当日与昨日。"""
    high, low, close = ctx["high"], ctx["low"], ctx["close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs(),
    ], axis=1).max(axis=1, skipna=True)
    return _safe_div(_asm(tr, 14), close)


def _calc_max_ret20(ctx: dict[str, pd.Series]) -> pd.Series:
    return ctx["ret"].rolling(20, min_periods=20).max()


def _calc_turn20(ctx: dict[str, pd.Series]) -> pd.Series:
    """20 日均成交额（亿元）。turnover 列是成交额（元），见 registry 里的说明。"""
    return _asm(ctx["turnover"], 20) / 1e8


def _asm_partial(series: pd.Series, n: int) -> pd.Series:
    """窗口 n 的宽松版均值：允许 20% 的格子缺失（min_periods = 0.8n）。

    专给 Amihud 用：成交额为 0（停牌占位/脏数据）会被置为 NaN，若坚持满窗口，
    单日脏数据会把其后整整 20 天拖成 NaN（实测 40 个 NaN），白白损失样本。
    """
    return series.rolling(n, min_periods=max(1, int(n * 0.8))).mean()


def _calc_amihud20(ctx: dict[str, pd.Series]) -> pd.Series:
    """Amihud 非流动性：mean(|ret| / 成交额)，乘 1e9 让数值落在可读量级。

    窗口用 _asm_partial（min_periods=16）：容忍少量成交额为 0 的脏格子，
    否则 1 天脏数据会污染其后 20 天。
    """
    amount = ctx["turnover"].where(ctx["turnover"] > 0)
    illiq = _safe_div(ctx["ret"].abs(), amount)
    return _asm_partial(illiq, 20) * 1e9


def _calc_vol_ratio5v20(ctx: dict[str, pd.Series]) -> pd.Series:
    volume = ctx["volume"]
    return _safe_div(_asm(volume, 5), _asm(volume, 20))


def _calc_turn_cv20(ctx: dict[str, pd.Series]) -> pd.Series:
    amount = ctx["turnover"]
    return _safe_div(_asd(amount, 20), _asm(amount, 20))


def _calc_ma_gap20(ctx: dict[str, pd.Series]) -> pd.Series:
    close = ctx["close"]
    return _safe_div(close, _asm(close, 20)) - 1.0


def _calc_ma_gap60(ctx: dict[str, pd.Series]) -> pd.Series:
    close = ctx["close"]
    return _safe_div(close, _asm(close, 60)) - 1.0


def _calc_slope20(ctx: dict[str, pd.Series]) -> pd.Series:
    slope, _ = _linreg(ctx["close"], 20)
    return _safe_div(slope, ctx["close"])


def _calc_r2_20(ctx: dict[str, pd.Series]) -> pd.Series:
    _, r2 = _linreg(ctx["close"], 20)
    return r2


def _calc_dist_high250(ctx: dict[str, pd.Series]) -> pd.Series:
    """距 250 日最高价。不足 250 日用可用窗口（至少 60 日），口径差异已在 registry 注明。"""
    highest = ctx["high"].rolling(250, min_periods=60).max()
    return _safe_div(ctx["close"], highest) - 1.0


def _calc_dist_low250(ctx: dict[str, pd.Series]) -> pd.Series:
    lowest = ctx["low"].rolling(250, min_periods=60).min()
    return _safe_div(ctx["close"], lowest) - 1.0


def _calc_corr_pv20(ctx: dict[str, pd.Series]) -> pd.Series:
    """收盘价与成交量的 20 日相关系数（rolling.corr 右对齐，窗口尾部=当日）。"""
    close, volume = ctx["close"], ctx["volume"]
    corr = close.rolling(20, min_periods=20).corr(volume)
    return corr.replace([np.inf, -np.inf], np.nan)


def _calc_obv_slope20(ctx: dict[str, pd.Series]) -> pd.Series:
    """OBV 的 20 日回归斜率 / 20 日均量（去掉量纲，便于横截面比较）。

    OBV_t = Σ sign(日收益) × volume，逐日累加，只依赖当日及以前。
    """
    ret, volume = ctx["ret"], ctx["volume"]
    direction = np.sign(ret.fillna(0.0))
    obv = (direction * volume.fillna(0.0)).cumsum()
    slope, _ = _linreg(obv, 20)
    return _safe_div(slope, _asm(volume, 20))


def _calc_overnight_ret20(ctx: dict[str, pd.Series]) -> pd.Series:
    """隔夜收益 = 今日开盘 / 昨日收盘 - 1，取 20 日均值（只用昨日收盘，无前视）。"""
    overnight = _safe_div(ctx["open"], ctx["close"].shift(1)) - 1.0
    return _asm(overnight, 20)


def _calc_intraday_ret20(ctx: dict[str, pd.Series]) -> pd.Series:
    """日内收益 = 今收 / 今开 - 1，取 20 日均值。"""
    intraday = _safe_div(ctx["close"], ctx["open"]) - 1.0
    return _asm(intraday, 20)


# 因子名 -> 计算器（必须与 registry.FACTOR_DEFS 一一对应，见文件末尾 _check_registry）
_CALCULATORS: dict[str, Callable[[dict[str, pd.Series]], pd.Series]] = {
    "mom20": _f_mom(20, 20),
    "mom60": _f_mom(20, 60),
    "mom120": _f_mom(20, 120),
    "ret1": _f_rev(1),
    "rev5": _f_rev(5),
    "rev10": _f_rev(10),
    "vol20": _calc_vol20,
    "vol_ratio_20_60": _calc_vol_ratio_20_60,
    "atr_pct14": _calc_atr_pct14,
    "max_ret20": _calc_max_ret20,
    "turn20": _calc_turn20,
    "amihud20": _calc_amihud20,
    "vol_ratio5v20": _calc_vol_ratio5v20,
    "turn_cv20": _calc_turn_cv20,
    "ma_gap20": _calc_ma_gap20,
    "ma_gap60": _calc_ma_gap60,
    "slope20": _calc_slope20,
    "r2_20": _calc_r2_20,
    "dist_high250": _calc_dist_high250,
    "dist_low250": _calc_dist_low250,
    "corr_pv20": _calc_corr_pv20,
    "obv_slope20": _calc_obv_slope20,
    "overnight_ret20": _calc_overnight_ret20,
    "intraday_ret20": _calc_intraday_ret20,
}


def _check_registry() -> None:
    """启动即自检：登记表与计算器必须双向一一对应，杜绝「登记了但没实现」。"""
    missing = [n for n in registry.all_names() if n not in _CALCULATORS]
    extra = [n for n in _CALCULATORS if n not in registry.all_names()]
    if missing or extra:
        raise RuntimeError("因子登记表与计算器不一致：缺少实现 %s，多余实现 %s" % (missing, extra))


_check_registry()


# ────────────────────────────────────────────────────────────
# 对外接口
# ────────────────────────────────────────────────────────────
def _prepare(df: pd.DataFrame) -> pd.DataFrame:
    """规整输入：校验列、按 date 升序、数值化、去重。

    * 必须含 date 与 OHLCV 六列；缺失直接抛 ValueError（早失败优于静默算错）。
    * 按 date 排序后重置索引，保证 rolling 的方向是「由旧到新」。
    * 同一 date 重复出现时保留最后一条（后到的覆盖先到的），与 INSERT OR REPLACE 语义一致。
    """
    missing = [c for c in _REQUIRED_COLS if c not in df.columns]
    if missing:
        raise ValueError("因子计算缺少必需列：%s" % ", ".join(missing))
    d = df.loc[:, list(_REQUIRED_COLS)].copy()
    d["date"] = d["date"].astype(str)
    for col in ("open", "high", "low", "close", "volume", "turnover"):
        d[col] = pd.to_numeric(d[col], errors="coerce").astype("float64")
    d = d.sort_values("date", kind="mergesort")
    d = d.drop_duplicates(subset=["date"], keep="last").reset_index(drop=True)
    return d


def compute_series(df: pd.DataFrame, names: Iterable[str] | None = None) -> pd.DataFrame:
    """计算因子的逐日时间序列。

    Args:
        df: 单只股票行情，需含 date/open/high/low/close/volume/turnover，
            内部会按 date 升序重排，调用方不必预先排序。
        names: 要计算的因子名；None 表示全部。

    Returns:
        DataFrame，index 为日期字符串（升序），columns 为因子名。
        第 i 行的值只使用第 0..i 行数据；数据不足的位置为 NaN。

    Raises:
        ValueError: 缺列。
        KeyError: names 里有未登记的因子。
    """
    requested = list(names) if names is not None else registry.all_names()
    for name in requested:
        registry.get(name)                      # 未登记直接抛 KeyError

    d = _prepare(df)
    out = pd.DataFrame(index=pd.Index(d["date"].astype(str), name="date"))
    if d.empty:
        for name in requested:
            out[name] = pd.Series(dtype="float64")
        return out

    close = d["close"]
    ctx: dict[str, pd.Series] = {
        "open": d["open"],
        "high": d["high"],
        "low": d["low"],
        "close": close,
        "volume": d["volume"],
        "turnover": d["turnover"],
        "ret": _ret(close),
    }

    min_bars = registry.min_bars_map()
    n_rows = len(d)
    for name in requested:
        # 样本不够就整列置 NaN，不抛异常（调用方决定是否落库）
        if n_rows < int(min_bars[name]):
            out[name] = np.nan
            continue
        series = _CALCULATORS[name](ctx)
        series = series.replace([np.inf, -np.inf], np.nan)
        out[name] = series.to_numpy(dtype="float64")

    return out


def compute_one(df: pd.DataFrame, names: Iterable[str] | None = None) -> dict[str, Any]:
    """计算「最新一日」的因子截面值（落库 / 打分用）。

    Args:
        df: 单只股票行情（同 compute_series）。
        names: 因子名列表；None 表示全部。

    Returns:
        {factor_name: float 或 None}。数据不足、或最后一日的值无效（停牌、
        成交额为 0 等）时返回 None，绝不抛异常、绝不填 0——
        填 0 会在排序时把「无数据」当成「极值」，这是最隐蔽的因子污染。
    """
    requested = list(names) if names is not None else registry.all_names()
    result: dict[str, Any] = {name: None for name in requested}
    if df is None or len(df) == 0:
        return result

    series = compute_series(df, requested)
    if series.empty:
        return result

    last = series.iloc[-1]
    for name in requested:
        value = last.get(name, np.nan)
        if value is None:
            continue
        try:
            fvalue = float(value)
        except (TypeError, ValueError):
            continue
        result[name] = fvalue if np.isfinite(fvalue) else None
    return result


__all__ = ["compute_one", "compute_series", "TRADING_DAYS"]
