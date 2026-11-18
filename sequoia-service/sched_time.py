#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""sched_time.py — 「下一次几点跑」的唯一实现（调度器与新鲜度共用）

为什么必须抽出来
----------------
改造前「下一次何时跑」这件事有两处独立实现：
  * `scheduler._next_run()` —— 给 `/api/scheduler/status` 的 `next_sync` / `next_scan`；
  * 页面自己按 `spawn` 时间 + 间隔推算 —— 于是出现了「页面说 16:10、实际 15:50」
    这类互相矛盾的时间。

现在只有这一个文件能回答「下一次几点跑」。`scheduler.py` 与 `freshness.py`
都从 `_next_run()` 取，页面（T05）只展示，不再自己算。

与调度器语义的对应关系（改这里必须同步 `scheduler._tick()`）
------------------------------------------------------------
调度器每 30 秒扫一次，判据是：
    * 周末不跑（`weekday() >= 5`）；
    * 已过 `hhmm` 且「今天还没成功过」→ 跑；
    * `catch_up` 开启时，服务在过点后启动会补跑一次。

`_next_run(now, hhmm, done_today)` 是它的逆函数：
    * `done_today=False` 且今天工作日、当前时间未过点 → 返回今天 `hhmm`；
    * 其余情况（今天已跑过 / 已过点 / 周末）→ 返回下一个工作日的 `hhmm`。

时间口径（共享知识第 2 条）
--------------------------
    `_next_run()` 返回 `YYYY-MM-DD HH:MM`（注意：是空格分隔，不是 ISO 的 T）；
    ISO 时间戳（`factor_run.json` 里的 `startedAt` 等）用 `factor_run.iso_now()`。

用法
----
    from sched_time import _next_run, _hm
    hh, mm = _hm("16:10")                     # → (16, 10)，非法值回落 15:40
    _next_run(datetime.now(), "16:10", False) # → "2026-11-16 16:10"
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Optional, Tuple

# 非法 `hhmm` 的回落值：与 scheduler.DEFAULT_CFG["sync_time"] 一致。
# 刻意**不**回落成"12:00"这种看起来更中性的值 —— 回落值必须是一个「真实可能
# 被配置出来」的时点，否则错误配置会被一个奇怪的时间点悄悄掩盖。
DEFAULT_HHMM = "15:40"

# `_next_run` 的输出格式（共享知识第 2 条：分到分钟，空格分隔）
RUN_FMT = "%Y-%m-%d %H:%M"
# 日期键格式
DATE_FMT = "%Y-%m-%d"

# 中文星期，供页面写「数据截止 2026-11-16（周一）」
WEEKDAY_CN = ("周一", "周二", "周三", "周四", "周五", "周六", "周日")


def now() -> datetime:
    """当前本地时间。抽成函数是为了让测试能替换它。"""
    return datetime.now()


def _hm(hhmm: str, default: str = DEFAULT_HHMM) -> Tuple[int, int]:
    """`"16:10"` → `(16, 10)`；解析失败返回 `default` 的时分（不抛异常）。

    刻意不抛异常：这是被页面/状态接口高频调用的展示路径，一个写错的配置文件
    不应该让整个状态接口 500 —— 而应该退回一个可解释的默认时点。
    需要严格校验的地方（保存配置时）另做校验，不靠这里。
    """
    for text in (hhmm, default):
        try:
            hour_s, minute_s = str(text).split(":")
            hour, minute = int(hour_s), int(minute_s)
        except (AttributeError, TypeError, ValueError):
            continue
        if 0 <= hour <= 23 and 0 <= minute <= 59:
            return hour, minute
    return 15, 40


def is_workday(moment: datetime) -> bool:
    """是否工作日。**只按星期几判**，不含法定节假日日历。

    这是本项目的既有边界（`scheduler._tick()` 同样只看 `weekday() < 5`）：
    节假日当天行情库不更新 → `lagTradingDays == 0`，新鲜度判定仍会是 fresh，
    不需要单独的节假日表。若哪天接了交易日历，改这里一处即可。
    """
    return moment.weekday() < 5


def due(moment: datetime, hhmm: str) -> bool:
    """当前时间是否已到（或已过）`hhmm` 这个时点（精确到分钟）。"""
    hour, minute = _hm(hhmm)
    return (moment.hour, moment.minute) >= (hour, minute)


def next_at(moment: datetime, hhmm: str,
            done_today: bool = False) -> Optional[datetime]:
    """下一次执行的**真实时间对象**；一周内找不到工作日时返回 None。

    Args:
        moment: 以哪个时刻为基准（通常是 `datetime.now()`）。
        hhmm: 目标时点，`"HH:MM"`。
        done_today: 今天这一档是否已经跑过。

    Returns:
        datetime 或 None（`_next_run` 用 None 表示"未知"）。
    """
    hour, minute = _hm(hhmm)
    if is_workday(moment) and not done_today and (moment.hour, moment.minute) < (hour, minute):
        return moment.replace(hour=hour, minute=minute, second=0, microsecond=0)
    probe = moment
    for _ in range(1, 8):          # 最多往前看 7 天，必然跨过一个工作日
        probe = probe + timedelta(days=1)
        if is_workday(probe):
            return probe.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return None


def _next_run(moment: datetime, hhmm: str, done_today: bool = False) -> str:
    """下一次执行时间，`"YYYY-MM-DD HH:MM"`；无法确定时返回 `"未知"`。

    参数语义与 `scheduler._tick()` 的判据严格对应，见模块 docstring。
    """
    target = next_at(moment, hhmm, done_today)
    if target is None:
        return "未知"
    return target.strftime(RUN_FMT)


def fmt(moment: Optional[datetime]) -> Optional[str]:
    """datetime → `"YYYY-MM-DD HH:MM"`；None 原样返回 None。"""
    return None if moment is None else moment.strftime(RUN_FMT)


def date_str(moment: Optional[datetime] = None) -> str:
    """`"YYYY-MM-DD"`；不传参数取今天。"""
    return (moment or now()).strftime(DATE_FMT)


def weekday_cn(moment: Optional[datetime] = None) -> str:
    """`"周一"`…`"周日"`；不传参数取今天。"""
    return WEEKDAY_CN[(moment or now()).weekday()]


def is_week_start(moment: Optional[datetime] = None) -> bool:
    """是否周一（= 每周全轴校验的那一天，见 Q6 决策）。

    为什么周一做全轴校验：出过的那次严重事故是**轴中间错位**，`tail` 模式只比对
    尾部，发现不了中间被打乱。每周一次全轴核对约 25 秒，成本可接受。
    """
    return (moment or now()).weekday() == 0


__all__ = [
    "DATE_FMT", "DEFAULT_HHMM", "RUN_FMT", "WEEKDAY_CN", "_hm", "_next_run",
    "date_str", "due", "fmt", "is_week_start", "is_workday", "next_at", "now",
    "weekday_cn",
]
