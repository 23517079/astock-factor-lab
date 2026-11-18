#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""trade_cost.py — 交易成本的**纯数值**内核（T07）

职责边界
--------
本模块只做数字：费率表、成本因子、净收益、结算探针、纸面交易。
**不碰 DB、不碰 factor_ts、不写任何文件**，因此可以在合成数据上做破坏性实验。

为什么要单独一层
----------------
成本一旦散落在打分/验证/页面三处，三处的费率档位迟早分叉，而分叉的
表现是「页面说扣费后有效、回测说扣费后归零」，谁也查不出差在哪个减号。
所以：**费率表只有 `rate_table()` 一处、成本因子只有 `adjust_factor()` 一处、
净收益只有 `net_forward()` 一处**。

约束 A 在成本上的延伸
---------------------
费率档位按**卖出日 `dates[i+h]`** 选（`rate_at()` 是唯一入口），
不是按评分日、也不是按「今天」选。用今天的费率倒灌全历史，等于在 2021 年
就知道了 2023-08-28 的印花税下调 —— 那是新前视，比 IC 那处更隐蔽。

统一费率对 rank IC 是单调变换
----------------------------
`fwd_net = (1+fwd) * k[d] - 1`：同一天所有股票乘同一个 `k[d]`，
横截面排名一分不变 → **Spearman IC 天然不含任何成本信息**。
所以本模块绝不拿 IC 证明「扣费后有效」，扣费证据只看净年化与 NW t。

未覆盖（必须写在输出里，不能装作覆盖了）
----------------------------------------
* 一字涨停/跌停无法从日线判定 —— 只按 `open<=0` 与 `volume==0` 剔除明确不可买的票；
* 冲击成本随成交额变化，本模块只给固定 10bp/边的保守基线；
* 经手费/证管费不单列（已含在佣金公示费率里，单列是重复计费）。
"""

from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

import numpy as np
import pandas as pd

from factor_eval import _annualize, newey_west_t


class CostProfileError(ValueError):
    """费率档位/倍数配置非法。

    单独成类而不是裸 `ValueError`，是为了让 `run_factor_pipeline.step_score`
    能显式捕获并映射到 `EXIT_INVALID=2` —— 配置写错属于校验失败，
    绝不能被当成「没数据、等下次」的 exit 4。继承 ValueError 保持向后兼容。
    """


# ── 公开规则的生效日（含当天）──────────────────────────────────────────
# 过户费：2022-04-29 起 0.02‰ → 0.01‰（双向）
TRANSFER_CUTOFF = "2022-04-29"
TRANSFER_BEFORE = 0.00002
TRANSFER_AFTER = 0.00001
# 印花税：2023-08-28 起 卖出 1‰ → 0.5‰（仅卖出端）
STAMP_CUTOFF = "2023-08-28"
STAMP_BEFORE = 0.001
STAMP_AFTER = 0.0005

# 默认口径（当前一轮实测 31.2bp/往返：买 13.1 + 卖 18.1）
#   买：佣金 3bp + 过户 0.1bp + 滑点 10bp
#   卖：佣金 3bp + 印花 5bp + 过户 0.1bp + 滑点 10bp
SLIPPAGE_BPS = 10.0

COST_PROFILES: Dict[str, Dict[str, Any]] = {
    "a_share": {
        # `profileId` 是与页面/报告对接的稳定名字；`a_share` 是 CLI 内部短名。
        # 两者必须同时存在，否则前端拿到的 profile 会随后端重构漂移。
        "profileId": "a_share_retail_v1",
        "label": "A股：佣金万三双边（起点5元）+ 印花税卖出 + 过户费双向 + 滑点10bp/边",
        "commissionRate": 0.0003,
        "commissionMin": 5.0,
        "slippageBps": SLIPPAGE_BPS,
        "assumption": True,
        "source": "无官方费率，保守基线（佣金/印花税/过户费取公开规则；10bp 滑点为主观保守假设）",
        "sources": [
            "佣金万三、单笔最低5元：行业公开零售费率，非监管定价",
            "印花税卖出 1‰→0.5‰（2023-08-28 起）：财政部/税务总局公告",
            "过户费 0.02‰→0.01‰（2022-04-29 起）：中国结算公告",
            "滑点 10bp/边：无官方费率，保守基线假设；已用全成本翻倍做敏感性兜底",
            "经手费/证管费：已含在佣金公示费率内，不单列（单列会重复计费）",
        ],
    },
    "zero": {
        "profileId": "zero_diagnostic",
        "label": "零成本（仅做毛口径诊断，不是可交易口径）",
        "commissionRate": 0.0,
        "commissionMin": 0.0,
        "slippageBps": 0.0,
        "assumption": False,
        "source": "诊断用零成本对照，不能作为扣费后有效性的证据",
        "sources": ["诊断用零成本对照，不能作为扣费后有效性的证据"],
    },
}

# 纸面交易每个买入日投入的名义额：用于判定最低佣金是否绑定。
# 2 万元 × 万三 = 6 元 > 5 元起点 → minCommissionBinding=False。
ASSUMED_NOTIONAL = 20_000.0

NOT_COVERED: List[str] = [
    "一字涨停/跌停无法从日线判定（这类票可能在 open 上有值却根本买不到）",
    "冲击成本随成交额/流动性变化，本模块只用固定 10bp/边的保守基线",
    "经手费与证管费不单列：已含在佣金公示费率内，单列会重复计费",
    "费率档位按卖出日选；买入端的过户费率随之取同一档（保守简化，不倒灌今天费率）",
]


# ────────────────────────────────────────────────────────────
# 费率表（唯一入口）
# ────────────────────────────────────────────────────────────
def _profile(profile: str) -> Dict[str, Any]:
    if profile not in COST_PROFILES:
        raise CostProfileError("未知成本档位 %r；可用：%s" % (profile, sorted(COST_PROFILES)))
    return COST_PROFILES[profile]


def _valid_multiplier(multiplier: float) -> float:
    try:
        got = float(multiplier)
    except (TypeError, ValueError):
        raise CostProfileError("cost multiplier 必须是正数，收到 %r" % (multiplier,))
    if not np.isfinite(got) or got <= 0:
        raise CostProfileError("cost multiplier 必须是正数，收到 %r" % (multiplier,))
    return got


def rate_table(profile: str = "a_share", multiplier: float = 1.0) -> List[Dict[str, Any]]:
    """按生效日升序的费率档位表（`from` 含、`to` 不含）。

    Returns:
        `[{"from","to","commission","commissionMin","stampSell",
           "transferBuy","transferSell","slippage"}]`
        `from=None` 表示足够早，`to=None` 表示至今。所有费率项都已乘 `multiplier`
        —— 翻倍是**全成本项一起乘2**（含最低佣金），不是只翻滑点。
    """
    p = _profile(profile)
    m = _valid_multiplier(multiplier)
    if profile == "zero":
        return [{
            "from": None, "to": None,
            "commission": 0.0, "commissionMin": 0.0,
            "stampSell": 0.0, "transferBuy": 0.0, "transferSell": 0.0,
            "slippage": 0.0,
        }]
    return [
        {"from": None, "to": TRANSFER_CUTOFF,
         "commission": float(p["commissionRate"]) * m,
         "commissionMin": float(p["commissionMin"]) * m,
         "stampSell": STAMP_BEFORE * m,
         "transferBuy": TRANSFER_BEFORE * m, "transferSell": TRANSFER_BEFORE * m,
         "slippage": float(p["slippageBps"]) / 10000.0 * m},
        {"from": TRANSFER_CUTOFF, "to": STAMP_CUTOFF,
         "commission": float(p["commissionRate"]) * m,
         "commissionMin": float(p["commissionMin"]) * m,
         "stampSell": STAMP_BEFORE * m,
         "transferBuy": TRANSFER_AFTER * m, "transferSell": TRANSFER_AFTER * m,
         "slippage": float(p["slippageBps"]) / 10000.0 * m},
        {"from": STAMP_CUTOFF, "to": None,
         "commission": float(p["commissionRate"]) * m,
         "commissionMin": float(p["commissionMin"]) * m,
         "stampSell": STAMP_AFTER * m,
         "transferBuy": TRANSFER_AFTER * m, "transferSell": TRANSFER_AFTER * m,
         "slippage": float(p["slippageBps"]) / 10000.0 * m},
    ]


def rate_at(profile: str, day: str, multiplier: float = 1.0) -> Dict[str, Any]:
    """某一天生效的费率档位。**费率档位的唯一选择入口**。"""
    d = str(day)[:10]
    for row in rate_table(profile, multiplier):
        if row["from"] is not None and d < str(row["from"]):
            continue
        if row["to"] is not None and d >= str(row["to"]):
            continue
        return row
    raise ValueError("费率表没有覆盖日期 %s（档位定义有洞，请修 rate_table）" % d)


def one_way_bps(profile: str = "a_share", multiplier: float = 1.0) -> float:
    """当前档位往返总成本（bp）：a_share ×1 = 31.2bp，×2 = 62.4bp。"""
    r = rate_at(profile, "2099-01-01", multiplier)  # 当前档：印花 0.5‰ / 过户 0.01‰
    buy = r["commission"] + r["transferBuy"] + r["slippage"]
    sell = r["commission"] + r["stampSell"] + r["transferSell"] + r["slippage"]
    return float((buy + sell) * 10000.0)


def profile_payload(profile: str = "a_share", multiplier: float = 1.0,
                    notional: float = ASSUMED_NOTIONAL) -> Dict[str, Any]:
    """给 `factor_score.json` 的 `cost` 段（架构契约 §7.1 的确切字段）。

    这里的 `rates` 九键是前端/报告的唯一读取路径；旧的扁平别名
    （commissionRate / stampRateSell / slippageBps / oneWayBps …）保留，
    供既有脚本对照，不参与任何判定。

    印花税按**卖出日**选 before/after，买入恒 0；费率档位按卖出日选的原则
    在这里只做展示，真正的数值选择仍然只在 `rate_at()`。
    """
    p = _profile(profile)
    m = _valid_multiplier(multiplier)
    r_now = rate_at(profile, "2099-01-01", m)
    commission = {
        "buy": float(r_now["commission"]), "sell": float(r_now["commission"]),
        "minPerOrder": float(r_now["commissionMin"]),
        "includes": ["经手费", "证管费"], "assumption": bool(p["assumption"]),
    }
    transfer = {"before": TRANSFER_BEFORE * m, "after": TRANSFER_AFTER * m,
                "changeDate": TRANSFER_CUTOFF}
    stamp = {"buy": 0.0, "before": STAMP_BEFORE * m, "after": STAMP_AFTER * m,
             "changeDate": STAMP_CUTOFF}
    slip = {"buy": float(r_now["slippage"]), "sell": float(r_now["slippage"]),
            "assumption": bool(p["assumption"])}
    round_trip_bps = one_way_bps(profile, m)
    return {
        "profile": str(p["profileId"]),
        "profileKey": profile,
        "multiplier": m,
        "entry": "s1_open",
        "rates": {"commission": commission, "transferFee": transfer,
                  "stampDuty": stamp, "slippage": slip},
        "roundTripBps": round_trip_bps,
        "assumedPositionNotional": float(notional),
        "minCommissionBinding": bool(
            min_commission_shortfall(notional, profile, m) > 0),
        "priceAdjust": "qfq",
        "sources": list(p["sources"]),
        "label": str(p["label"]),
        "assumption": bool(p["assumption"]),
        "source": str(p["source"]),
        "note": ("滑点 10bp/边是**无官方费率下的保守基线**，已用全成本翻倍做敏感性兜底；"
                 "费率档位按卖出日选，不按运行日今天倒灌历史"),
        "notCovered": list(NOT_COVERED),
        # ── 旧别名（附加，不参与判定）──
        "commissionRate": commission["buy"],
        "commissionMin": commission["minPerOrder"],
        "stampRateBuy": 0.0,
        "stampRateSell": stamp["after"],
        "transferRateBuy": transfer["after"],
        "transferRateSell": transfer["after"],
        "slippageBps": float(p["slippageBps"]) * m,
        "oneWayBps": round_trip_bps,
        "rateTable": rate_table(profile, m),
        "doubledRoundTripBps": one_way_bps(profile, 2.0),
    }


def commission_of(notional: float, profile: str = "a_share",
                  multiplier: float = 1.0) -> float:
    """佣金金额 = max(名义额 × 万三, 5元)，两倍成本时起点也翻倍。

    名义额 1 万：3元 < 5元 → 收 5元（5bp）；
    名义额 2 万：6元 > 5元 → 收 6元（3bp）。最低佣金在这里翻转。
    """
    r = rate_at(profile, "2099-01-01", multiplier)
    n = float(notional)
    if not np.isfinite(n) or n < 0:
        raise ValueError("notional 必须是非负有限数，收到 %r" % (notional,))
    return float(max(n * r["commission"], r["commissionMin"]))


def min_commission_shortfall(notional: float, profile: str = "a_share",
                             multiplier: float = 1.0) -> float:
    """不足最低佣金时多收的金额（已含起点，故不会为负）。"""
    r = rate_at(profile, "2099-01-01", multiplier)
    return float(max(0.0, r["commissionMin"] - float(notional) * r["commission"]))


# ────────────────────────────────────────────────────────────
# 成本因子与净收益
# ────────────────────────────────────────────────────────────
def adjust_factor(dates: Sequence[str], horizon: int,
                  profile: str = "a_share",
                  multiplier: float = 1.0) -> pd.Series:
    """逐日往返成本因子 `k`。

        k[i] = (1 - slip_s) * (1 - sell_fee(dates[i+h]))
             / ((1 + slip_b) * (1 + buy_fee(dates[i])))

    **档位按卖出日 `dates[i+h]` 选**（唯一入口 `rate_at`），绝不按运行日今天选。
    最后 `horizon` 天没有卖出日 → NaN，**不 fillna、不 backfill**。

    Args:
        dates: 交易日轴（升序，YYYY-MM-DD）。
        horizon: 持有期（交易日）。
        profile: 成本档位。
        multiplier: 全成本项倍数。

    Returns:
        `pd.Series(index=dates, name="costFactor")`。
    """
    h = int(horizon)
    if h < 1:
        raise ValueError("horizon 必须 >= 1，收到 %r" % (horizon,))
    _profile(profile)
    _valid_multiplier(multiplier)

    days = [str(d)[:10] for d in dates]
    n = len(days)
    vals = np.full(n, np.nan, dtype="float64")
    for i in range(max(0, n - h)):
        # 档位按卖出日选（不是评分日/买入日/运行日）
        r = rate_at(profile, days[i + h], multiplier)
        buy_fee = r["commission"] + r["transferBuy"]
        sell_fee = r["commission"] + r["stampSell"] + r["transferSell"]
        vals[i] = ((1.0 - r["slippage"]) * (1.0 - sell_fee)
                   / ((1.0 + r["slippage"]) * (1.0 + buy_fee)))
    return pd.Series(vals, index=pd.Index(days), name="costFactor", dtype="float64")


def net_forward(fwd: pd.DataFrame, factor: pd.Series) -> pd.DataFrame:
    """净未来收益：`(1 + fwd) * k[d] - 1`（按日期对齐，不使用未来 k）。"""
    if not isinstance(factor, pd.Series):
        raise ValueError("net_forward 的 factor 必须是 pd.Series（按日期的 k）")
    k = factor.reindex(index=fwd.index).astype("float64")
    return (1.0 + fwd).mul(k, axis=0) - 1.0


# ────────────────────────────────────────────────────────────
# 结算探针
# ────────────────────────────────────────────────────────────
def settle_check(close_wide: pd.DataFrame, open_wide: pd.DataFrame,
                 fwd: pd.DataFrame, horizon: int,
                 entry: str = "s1_open", volume_wide: Optional[pd.DataFrame] = None,
                 tol: float = 1e-12) -> Dict[str, Any]:
    """逐格核对「能不能按 fwd 的假设成交」，把静默的假成交变成可见的计数。

    拦截三类：
      1. 日期轴不齐 / `open[d+1]` 缺失（不能用 close 顶替）；
      2. fwd 复算差 > `tol`；
      3. `open[d+1] <= 0` 或 `volume[d+1] == 0` → 不可买，剔除。

    一字涨停无法从日线判定，列入 `notCovered`，不假装判过。

    Returns:
        架构契约 §7.2 的 `{ok, axisMatch, missingOpen, missingClose, nUnbuyable,
        nUnsettled, nSettled, fwdRecomputeMaxAbsDiff, samples, notCovered}`；
        旧名（`nMissingOpen/maxFwdDiff/examples/…`）作为附加别名一并保留。
        `ok=False` 只表示数据/复算有问题；`nUnbuyable>0` 是正常剔除，不判失败。
        日期轴不一致时**返回 ok=False**而不是抛异常 —— 抛异常会绕过
        `evidenceGate` 的聚合优先级，用户只看到「打分异常」却不知道是结算塌了。
    """
    h = int(horizon)
    if h < 1:
        raise ValueError("horizon 必须 >= 1，收到 %r" % (horizon,))
    if entry != "s1_open":
        raise ValueError("settle_check 只支持 entry='s1_open'，收到 %r" % (entry,))
    if close_wide.empty or fwd.empty:
        raise ValueError("价格/fwd 宽表为空，无法结算")

    axis_match = bool(close_wide.index.equals(fwd.index))

    def result(ok: bool, n_settled: int, n_unsettled: int, n_unbuyable: int,
               missing_open: int, missing_close: int, n_mismatch: int,
               max_diff: Any, samples: List[Dict[str, Any]]) -> Dict[str, Any]:
        return {
            "ok": bool(ok),
            "axisMatch": axis_match,
            "missingOpen": int(missing_open),
            "missingClose": int(missing_close),
            "nUnbuyable": int(n_unbuyable),
            "nUnsettled": int(n_unsettled),
            "nSettled": int(n_settled),
            "fwdRecomputeMaxAbsDiff": max_diff,
            "samples": samples,
            "notCovered": list(NOT_COVERED),
            "entry": entry,
            "horizon": h,
            # ── 旧别名（附加，不参与判定）──
            "nMissingOpen": int(missing_open),
            "nMissingClose": int(missing_close),
            "nFwdMismatch": int(n_mismatch),
            "maxFwdDiff": max_diff,
            "examples": samples,
        }

    if not axis_match:
        return result(False, 0, 0, 0, 0, 0, 0, None,
                      [{"reason": "axisMismatch",
                        "note": "close 与 fwd 日期轴不一致，不能按位置对齐"}])

    idx = fwd.index
    cols = fwd.columns
    op = open_wide.reindex(index=idx, columns=cols)
    cl = close_wide.reindex(index=idx, columns=cols)
    entry_px = op.shift(-1)
    exit_px = cl.shift(-h)
    with np.errstate(divide="ignore", invalid="ignore"):
        recomputed = exit_px / entry_px - 1.0
    diff = (fwd - recomputed).abs()

    mask = fwd.notna()
    has_open = mask & entry_px.notna() & np.isfinite(entry_px)
    has_close = mask & exit_px.notna() & np.isfinite(exit_px)
    missing_open = int((mask & ~has_open).sum().sum())
    missing_close = int((mask & ~has_close).sum().sum())
    mism = has_open & has_close & (diff > float(tol))
    n_mismatch = int(mism.sum().sum())
    # DataFrame.max() 先按列聚合，再取一次 max 才是全表标量
    max_diff = float(diff[mism | (has_open & has_close) & np.isfinite(diff)]
                     .max().max()) if int((has_open & has_close).sum().sum()) else 0.0
    if not np.isfinite(max_diff):
        max_diff = 0.0

    unbuyable = has_open & ((entry_px <= 0) | ~np.isfinite(entry_px))
    if volume_wide is not None:
        vol = volume_wide.reindex(index=idx, columns=cols).shift(-1)
        unbuyable = unbuyable | (mask & vol.notna() & (vol <= 0))
    n_unbuyable = int(unbuyable.sum().sum())

    good = has_open & has_close & ~mism & ~unbuyable
    n_settled = int(good.sum().sum())
    n_unsettled = int(mask.sum().sum()) - n_settled

    samples: List[Dict[str, Any]] = []
    for flag, tag in ((mask & ~has_open, "missingOpen"),
                      (mask & ~has_close, "missingClose"),
                      (mism, "fwdMismatch"), (unbuyable, "unbuyable")):
        where = np.argwhere(flag.to_numpy())
        for r, c in where[:3]:
            samples.append({"reason": tag, "date": str(idx[int(r)]),
                            "symbol": str(cols[int(c)])})

    ok = bool(axis_match and missing_open == 0 and missing_close == 0
              and n_mismatch == 0)
    return result(ok, n_settled, n_unsettled, n_unbuyable, missing_open,
                  missing_close, n_mismatch, max_diff, samples)


# ────────────────────────────────────────────────────────────
# 纸面交易（净收益 → 日序列 / 门槛）
# ────────────────────────────────────────────────────────────
def paper_trade(scores: pd.DataFrame, valid: pd.DataFrame,
                fwd_gross: pd.DataFrame, cost_factor: pd.Series,
                top_n: int = 10, horizon: int = 5,
                min_valid_factors: int = 2,
                min_trades: int = 50,
                capital: float = 1_000_000.0,
                profile: str = "a_share",
                multiplier: float = 1.0,
                volume_wide: Optional[pd.DataFrame] = None) -> Dict[str, Any]:
    """逐日 Top N 等权，按净收益记一笔纸面交易。

    为什么重叠持仓要用 NW t：相邻交易日的 h 日标签有 h-1 天重合，日收益序列
    强自相关，朴素 `mean/std*sqrt(n)` 会把有效样本虚高约 sqrt(h) 倍。
    这里**只**用 `newey_west_t(daily, lag=h-1)`，不自己写修正。

    最低佣金按每笔名义额 `capital/top_n` 补收差额（1 万 / 2 万会翻转），
    差额摊回该股票的净收益；绝不按收盘价补成交。

    Args:
        scores: 综合分宽表（index=date, columns=symbol）。
        valid: 每格参与合成的因子数（< min_valid_factors 的票剔除）。
        fwd_gross: 毛收益宽表（同一份，不重新读价格）。
        cost_factor: `adjust_factor()` 的逐日 k。
        capital: 每个买入日投入的总名义额（仅用于最低佣金判定）。

    Returns:
        `{daily, nTrades, nBuyDates, nDays, tNw, meanNet, annualNet, gate,
          symbolsByDate}`
    """
    n_top = int(top_n)
    h = int(horizon)
    if n_top < 1 or h < 1:
        raise ValueError("top_n / horizon 必须 >= 1，收到 %d / %d" % (n_top, h))
    k = float(capital) / float(n_top)
    if k <= 0:
        raise ValueError("capital/top_n 必须为正")

    fwd_net = net_forward(fwd_gross, cost_factor)
    shortfall = min_commission_shortfall(k, profile, multiplier) * 2.0  # 买+卖各一次
    shortfall_ret = shortfall / k

    dates = [d for d in scores.index if d in fwd_net.index]
    # 结算探针只「数」不可买的票；真正下单向纸面交易也必须剔除同一批票，
    # 否则会出现「settlement 报 nUnbuyable>0，交易却照买」的自相矛盾。
    vol_entry: Optional[pd.DataFrame] = None
    if volume_wide is not None and not volume_wide.empty:
        vol_entry = volume_wide.reindex(index=scores.index,
                                        columns=scores.columns).shift(-1)
    daily: Dict[str, float] = {}
    symbols_by_date: Dict[str, List[str]] = {}
    n_trades = 0
    n_unbuyable_excluded = 0
    for d in dates:
        row = scores.loc[d]
        vrow = valid.reindex(index=scores.index).loc[d]
        row = row[vrow.reindex(row.index).fillna(0) >= int(min_valid_factors)]
        row = row.dropna()
        if row.empty:
            continue
        if vol_entry is not None:
            vol_row = vol_entry.loc[d].reindex(row.index)
            bad = vol_row.notna() & (vol_row <= 0)
            n_unbuyable_excluded += int(bad.sum())
            row = row[~bad]
        if row.empty:
            continue
        chosen = list(row.sort_values(ascending=False).index[:n_top])
        rets = [fwd_net.loc[d, s] for s in chosen]
        rets = [float(r) for r in rets if np.isfinite(r)]
        if not rets:
            continue
        # 最低佣金差额：每只票的净收益各扣一次，等权平均 = 总差额 / capital
        value = float(np.mean(rets)) - shortfall_ret
        daily[str(d)] = value
        symbols_by_date[str(d)] = chosen
        n_trades += len(rets)

    series = pd.Series(daily, dtype="float64").sort_index()
    n_days = int(series.size)
    mean_net = float(series.mean()) if n_days else 0.0
    t_nw = float(newey_west_t(series.to_numpy(), max(0, h - 1))) if n_days >= 3 else float("nan")
    if not np.isfinite(t_nw):
        t_nw = None
    n_buy_dates = int(sum(1 for v in symbols_by_date.values() if v))

    # 门槛：三条件全满足才过；作用域是**整个验证窗口**，逐期只记数不判定
    thresholds = {"nTrades": int(min_trades), "nBuyDates": 10, "nDays": 50}
    gate_ok = (n_trades >= thresholds["nTrades"] and n_buy_dates >= thresholds["nBuyDates"]
               and n_days >= thresholds["nDays"])
    reasons = []
    if n_trades < thresholds["nTrades"]:
        reasons.append("成交笔数 %d < %d" % (n_trades, thresholds["nTrades"]))
    if n_buy_dates < thresholds["nBuyDates"]:
        reasons.append("买入日 %d < %d" % (n_buy_dates, thresholds["nBuyDates"]))
    if n_days < thresholds["nDays"]:
        reasons.append("有效日 %d < %d" % (n_days, thresholds["nDays"]))

    return {
        "daily": series,
        "nTrades": n_trades,
        "nBuyDates": n_buy_dates,
        "nDays": n_days,
        "tNw": t_nw,
        "meanNet": mean_net,
        "annualNet": _annualize(mean_net, h),
        "gate": {"passed": bool(gate_ok), "thresholds": thresholds,
                 "reasons": reasons, "scope": "entireValidationWindow"},
        "symbolsByDate": symbols_by_date,
        "nUnbuyableExcluded": int(n_unbuyable_excluded),
        "capitalPerName": k,
        "minCommissionShortfall": shortfall,
        "notCovered": list(NOT_COVERED),
    }
