# -*- coding: utf-8 -*-
"""
因子大白话解释表（给不看统计术语的人用）

为什么要单独一个文件：
    `factors/registry.py` 里的 `formula_desc` 是给写代码的人看的（"close[t-20]/close[t-40]-1"），
    不懂公式的人读完仍然不知道"这个指标到底在看什么"。
    这里为每个因子写三句话：
        1. `what`  这是什么——用日常生活的话说，不出现公式
        2. `high`  数值高说明什么——只描述现象，不判断好坏（好坏由 direction 决定）
        3. `note`  为什么这么设计 / 容易误解的地方

    注意：这里只写"这个指标在量什么"，**不写"它有效"**。
    有效性必须看 IC 和 t 值，由 factor_plain.reading() 按实测结果生成。

维护方式：
    新增因子时在 PLAIN 里补一条；缺条目不会报错，页面会退化成显示 label。
"""

from typing import Any, Dict, Optional

# 每个因子：what（这是什么）/ high（数值高说明什么）/ note（设计意图或易误解点）
PLAIN: Dict[str, Dict[str, str]] = {
    # ---------- 动量 ----------
    "mom20": {
        "what": "一个月前那段时间，这只股票涨了多少（故意跳开最近一个月不算）",
        "high": "说明它一个月前涨得猛、趋势刚起来不久",
        "note": "为什么跳过最近 20 天：短期涨跌多半是情绪和消息，混进来会把『真趋势』和『一日游』搞混",
    },
    "mom60": {
        "what": "两个月前那三个月里，这只股票累计涨了多少（同样跳开最近一个月）",
        "high": "说明中期趋势向上，且不是最近才启动",
        "note": "窗口更长，反应更慢，适合看『这只票是不是处在中期上升通道』",
    },
    "mom120": {
        "what": "半年前那半年里累计涨了多少（跳开最近一个月）",
        "high": "说明长期趋势向上",
        "note": "需要至少 141 根 K 线才有值，上市不满半年的新股算不出来",
    },
    # ---------- 反转 ----------
    "ret1": {
        "what": "昨天涨了还是跌了，涨跌幅多少",
        "high": "说明昨天是上涨的",
        "note": "短期反转逻辑：昨天涨多了今天容易回吐，所以按『越小越好』登记",
    },
    "rev5": {
        "what": "最近 5 个交易日累计涨跌幅（一周）",
        "high": "说明这一周是涨的",
        "note": "同上，一周内涨太多的票，下周容易歇一歇",
    },
    "rev10": {
        "what": "最近 10 个交易日累计涨跌幅（两周）",
        "high": "说明这两周是涨的",
        "note": "窗口拉长到两周，仍然按『涨多了要回调』来用",
    },
    # ---------- 波动 ----------
    "vol20": {
        "what": "最近 20 天这只股票每天波动有多大（换算成年化百分比）",
        "high": "说明它最近很『晃』，涨跌幅度大",
        "note": "低波动异象：历史上波动小的股票，长期收益反而更稳，所以按『越小越好』登记",
    },
    "vol_ratio_20_60": {
        "what": "最近 20 天的波动，比最近 60 天的波动，是放大还是收敛了",
        "high": "说明最近一个月比过去三个月更剧烈（波动在放大）",
        "note": "比值 >1 表示短期比长期更闹腾，通常出现在行情转折或消息刺激时",
    },
    "atr_pct14": {
        "what": "最近 14 天平均每天真实波动幅度，占股价的百分之几",
        "high": "说明每天上下震荡的幅度相对股价更大",
        "note": "ATR 会把跳空算进去（用昨日收盘和今日最高最低比较），比单纯收盘价波动更贴近真实体验",
    },
    "max_ret20": {
        "what": "最近 20 天里，单日涨幅最大的那一天涨了多少",
        "high": "说明最近有过一次暴涨（比如涨停）",
        "note": "用来识别『刚被爆炒过』的票。爆炒之后往往回落，所以按『越小越好』登记",
    },
    # ---------- 流动性 ----------
    "turn20": {
        "what": "最近 20 天平均每天成交多少钱（单位：亿元）",
        "high": "说明这只票交易活跃、买卖容易成交",
        "note": "原名『换手率』，但数据源不给流通股本，这里实际是成交额。名字保留 turn20 以免破坏已有引用",
    },
    "amihud20": {
        "what": "平均而言，成交一亿元能把股价推动多少（越大说明越推得动）",
        "high": "说明这只票『盘口薄』，稍微有点买卖就能把价格砸出坑",
        "note": "学术名叫 Amihud 非流动性。数值大 = 流动性差 = 想买买不到、想卖卖不掉",
    },
    "turn_cv20": {
        "what": "最近 20 天成交额的『忽大忽小』程度（变异系数）",
        "high": "说明成交额很不稳定，时而爆量时而冷清",
        "note": "成交忽大忽小往往跟着消息或资金进出，比『一直活跃』包含更多信息",
    },
    "vol_ratio5v20": {
        "what": "最近 5 天的平均成交量，是最近 20 天平均的几倍",
        "high": "说明最近一周明显放量（有人在动手）",
        "note": "大于 1 是放量，小于 1 是缩量。经典用法是『放量突破』",
    },
    # ---------- 趋势 ----------
    "ma_gap20": {
        "what": "现在的股价，比最近 20 天的平均成本高（或低）百分之几",
        "high": "说明股价已经涨到 20 日均线上方较远（短期偏离均线过多）",
        "note": "乖离过大通常意味着短期涨太快，有回踩均线的倾向，所以按『越小越好』登记",
    },
    "ma_gap60": {
        "what": "现在的股价，比最近 60 天的平均成本高（或低）百分之几",
        "high": "说明股价远离三个月来的平均成本",
        "note": "比 ma_gap20 更稳，反映中期是否『过热』",
    },
    "slope20": {
        "what": "最近 20 天股价走势的整体倾斜程度（除以股价做标准化）",
        "high": "说明这一个月整体是往上走的，且斜率陡",
        "note": "和『涨了多少』不同，斜率看的是走势的角度，横盘很久后突然拉升会很明显",
    },
    "r2_20": {
        "what": "最近 20 天的走势，有多像一条直线（0 到 1，越大越像）",
        "high": "说明走势很规整、很少乱震（趋势清晰）",
        "note": "R² 只衡量『像不像直线』，不区分向上还是向下。要和 slope20 一起看",
    },
    # ---------- 位置 ----------
    "dist_high250": {
        "what": "现在的股价，距离过去一年最高价还有多远（百分比）",
        "high": "说明离一年新高还很远（还在下面趴着）",
        "note": "接近 0 表示正在创新高。数据不足 250 天时退化为『上市以来最高价』，口径会变化",
    },
    "dist_low250": {
        "what": "现在的股价，比过去一年最低价高了多少（百分比）",
        "high": "说明已经离一年最低点涨上去不少",
        "note": "同样有口径退化问题：上市不满一年的票，用的是『上市以来最低价』",
    },
    # ---------- 量价 ----------
    "corr_pv20": {
        "what": "最近 20 天，『价格上涨』和『成交量放大』是不是同步发生（相关系数 -1 到 +1）",
        "high": "说明涨的时候放量、跌的时候缩量（量价配合健康）",
        "note": "负值表示量价背离：涨得越高成交反而越少，通常被认为是上攻乏力",
    },
    "obv_slope20": {
        "what": "把每天的成交量按涨跌方向加总成一条线（OBV），这条线最近 20 天是往上还是往下",
        "high": "说明资金在持续净流入（涨的日子成交更多）",
        "note": "OBV 把『价』和『量』压成一个数，用来看资金方向而不只是价格方向",
    },
    # ---------- 收益结构 ----------
    "overnight_ret20": {
        "what": "最近 20 天，平均每天『隔夜』（昨天收盘到今天开盘）涨跌多少",
        "high": "说明经常一开盘就跳空高开",
        "note": "隔夜收益常被归因于消息和外围市场，和盘中资金行为是两回事",
    },
    "intraday_ret20": {
        "what": "最近 20 天，平均每天『盘中』（今天开盘到今日收盘）涨跌多少",
        "high": "说明白天交易时段整体是往上走的",
        "note": "把一天拆成『隔夜』和『盘中』两段，能区分是消息驱动还是资金驱动",
    },
}

# 指标名 → 一句话解释（页面「怎么看这张表」用）
METRIC_HELP: Dict[str, str] = {
    "ic": "IC 均值：把全市场股票按这个指标从高到低排队，看排在前面的股票是不是真的涨得更好。"
          "取值 -1 到 +1，0 表示『排前面排后面没差别』，绝对值越大说明这个指标越能区分好坏。",
    "t": "t 值：这个 IC 是真本事还是碰运气。|t| 超过 2 通常认为不像运气（约等于 95% 把握）。"
         "我们用的是修正过的算法（Newey-West），因为连续几天的收益会互相重叠，不修正会把 t 值吹大。",
    "q": "q 值：同时测 24 个指标时，总有几个『看着显著』其实纯属巧合。"
         "q 值做了多重比较校正，q < 0.05 才算真的不是巧合。",
    "mono": "单调性：把股票按指标分成 10 组（第 1 组最靠先验方向），看这 10 组的收益是不是依次递增或递减。"
            "+1 表示完全递增、-1 表示完全反向，0 表示乱七八糟没有规律。",
    "ls": "多空年化：买第 1 组、卖第 10 组，一年下来能赚多少（未扣交易成本）。"
          "这是理论值，实际会因为手续费、滑点、买不到而打折。",
    "turnover": "换手：这个指标选出的股票池，每次调仓要换掉多少（0.3 表示换掉三成）。"
                "越高越难落地，因为每次换股都要付手续费。",
}


def plain_of(name: str, label: str = "") -> Dict[str, str]:
    """取某个因子的大白话解释；没有登记就退回 label，不抛错。"""
    hit = PLAIN.get(name)
    if hit:
        return hit
    return {"what": label or name, "high": "（暂无解释）", "note": ""}


def reading(factor: Dict[str, Any], horizon: int = 5) -> Dict[str, Any]:
    """
    根据实测结果生成「现在说明什么」和「能不能用」两段人话。

    参数 factor 需要含：ic / tNw / qNw / mono / direction / directionMismatch
    返回：{plain: 一句话结论, detail: 补充说明, tone: good/warn/bad}
    """
    ic = factor.get("ic")
    t = factor.get("tNw")
    q = factor.get("qNw")
    mono = factor.get("mono")
    direction = factor.get("direction") or 1
    mismatch = bool(factor.get("directionMismatch"))

    if ic is None or t is None:
        return {"plain": "样本不足，算不出可靠结论",
                "detail": "有效交易日太少，这个数字现在没有意义。",
                "tone": "bad"}

    word = "越大" if direction > 0 else "越小"
    same_way = (ic > 0) == (direction > 0)

    if same_way:
        head = f"实测：这个指标{word}的股票，未来 {horizon} 天确实涨得更好"
    elif mismatch:
        head = f"实测：这个指标{word}的股票，未来 {horizon} 天反而涨得更差"
    else:
        head = f"实测：这个指标{word}的股票，未来 {horizon} 天略差一些（但不明显）"

    # 显著性
    if q is not None and q < 0.05:
        sig = "统计上站得住（多重校正后仍显著）"
    elif abs(t) >= 2:
        sig = "单看 t 值够强，但多重校正后不算稳"
    else:
        sig = "统计上跟『没规律』区分不开，可能只是运气"

    # 单调性
    if mono is not None and abs(mono) >= 0.5:
        mono_txt = ("十组收益基本按顺序排列，说明不是某一小撮股票带出来的结果"
                    if mono > 0 else
                    "十组收益是倒序排列，说明规律存在但方向和登记的先验相反")
    else:
        mono_txt = "十组收益没有明显顺序，可能只在极端值上有效，也可能只是噪声"

    detail = f"{sig}；{mono_txt}。"

    if mismatch:
        tone = "warn"
        detail += (" 注意：这个指标的真实方向与系统登记的相反，"
                   "如果直接照登记方向用，会做出反向操作。要用必须先翻转方向。")
    elif q is not None and q < 0.05 and mono is not None and abs(mono) >= 0.5:
        tone = "good"
    elif abs(t) >= 2:
        tone = "warn"
    else:
        tone = "bad"

    return {"plain": head, "detail": detail, "tone": tone}


def factor_notes(factors: Dict[str, Dict[str, Any]], horizon: int = 5) -> Dict[str, Dict[str, str]]:
    """给一批因子补上 what / high / note / reading，返回 name → 说明。"""
    out: Dict[str, Dict[str, str]] = {}
    for name, f in factors.items():
        base = plain_of(name, f.get("label", ""))
        r = reading(f, horizon)
        out[name] = {
            "what": base["what"],
            "high": base["high"],
            "note": base["note"],
            "plain": r["plain"],
            "detail": r["detail"],
            "tone": r["tone"],
        }
    return out


if __name__ == "__main__":
    # 自检：确保 PLAIN 覆盖 registry 里的全部因子，多写或漏写都会报出来
    import sys

    from factors.registry import all_names

    have = set(PLAIN)
    need = set(all_names())
    missing = sorted(need - have)
    extra = sorted(have - need)
    print("registry 因子数 %d，PLAIN 条目 %d" % (len(need), len(have)))
    if missing:
        print("！！ 缺解释：", ", ".join(missing))
    if extra:
        print("！！ 多余条目（registry 里没有）：", ", ".join(extra))
    print("OK" if not missing and not extra else "有问题")
    sys.exit(1 if (missing or extra) else 0)
