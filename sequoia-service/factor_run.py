#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factor_run.py — 因子任务的运行状态文件（`data/factor_run.json`）与运行锁

为什么需要这个文件
------------------
「页面上显示的上次计算时间」必须只有一个来源。改造前是各脚本各自
`time.strftime()` 生成字符串，于是出现了「页面显示 11-15 22:13、内容其实是
11-16 00:22」这种自相矛盾。本模块是**唯一时间源**：业务时间只从这里读
（共享知识第 1 条）。

双层结构（关键设计）
--------------------
文件里同时存在两份状态：

    {
      "runId": "...", "startedAt": "...", "finishedAt": "...",   ← 顶层 = 上一次「已结算」的运行
      "targetDates": [...], "ok": true, "cost": 719.4, "error": null,
      "trigger": "scheduler", "verify": {...},
      "running": {                                              ← 进行中的运行（未结算）
         "runId": "...", "startedAt": "...", "phase": "compute",
         "targetDates": [...], "trigger": "scheduler"
      }
    }

规则（不可违反）：
  * `begin()` **只写 running，绝不动顶层**。所以任务跑到一半崩掉，页面读到的
    仍然是「上一次成功的运行」——它确实是最后一次**已结算**的事实，没有说谎。
  * `finish()` 把 running 的内容升到顶层并置 `running=null`。
  * 两者都是 `.tmp → os.replace`，任何时刻磁盘上的文件都是完整 JSON。

由此派生（共享知识第 8 条「失败不可静默」）：
    lastCalcAt    = finishedAt（未结算过则 startedAt）
    lastCalcOk    = ok
    lastCalcError = error
失败时必须同时满足：error 有值（≤120 字）、进程非零退出码、不覆盖任何产物。

运行锁
------
`acquire_lock(ttl=5400)` 用 `data/factor_run.lock` 做进程级互斥，防止
「定时任务 + 手动触发」同时跑两份重算（两份同时写 factor_panel 会互相等锁，
且 staging 目录会被踩坏）。锁带 TTL，持有者进程消失或超时即视为陈旧可抢占。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.abspath(__file__))

# 退出码（共享知识第 6 条，全链路统一）
EXIT_OK = 0
EXIT_INVALID = 2       # 校验失败（含缓存因子不齐、数据层探针未过）
EXIT_LOCKED = 3        # 运行锁被占用
EXIT_NO_PERIOD = 4     # 没有任何可用结果（连打分都做不出来）
#   ⚠ 语义变更：以前把「滚动验证本次无有效期」也归到 4，会造成
#   「名单和导出都已经写成功，页面却报更新失败」——用户以为整批数据不能用，
#   实际只缺滚动那一块。现在「无有效期」走 `ok=true, partial=true`，
#   产物照写、退出码 0；4 只留给「连打分都做不出来」（没有任何可用因子 / 面板为空）。
EXIT_EXPORT = 5        # 导出/写盘异常
EXIT_TIMEOUT = 124     # 超时

# 错误信息上限（共享知识第 8 条：error ≤120 字，避免把整段 traceback 塞进 JSON）
ERROR_MAX_LEN = 120
# 锁默认 TTL：90 分钟。全量重建实测 39 分钟，留一倍余量。
LOCK_TTL = 5400

# `running` 的有效期（秒）。`running` 是**持久化**的，进程被强杀、机器断电时
# 它会永久停在 true，页面就会永久卡在「今日重算进行中」。
# 子进程硬超时 60 分钟（scheduler.FACTOR_TIMEOUT），这里留到 90 分钟余量。
RUNNING_STALE_SEC = 90 * 60

# ISO 本地时间（无时区），共享知识第 2 条
ISO_FMT = "%Y-%m-%dT%H:%M:%S"


def iso_now() -> str:
    """当前本地时间，格式 `YYYY-MM-DDTHH:MM:SS`（无时区）。"""
    return time.strftime(ISO_FMT)


def _run_path() -> str:
    """运行状态文件路径（可用环境变量 FACTOR_RUN_PATH 覆盖，测试用）。"""
    env = os.environ.get("FACTOR_RUN_PATH")
    if env:
        return env
    return os.path.join(HERE, "data", "factor_run.json")


def _lock_path() -> str:
    env = os.environ.get("FACTOR_RUN_LOCK_PATH")
    if env:
        return env
    return os.path.join(HERE, "data", "factor_run.lock")


def _read_json(path: str) -> Dict[str, Any]:
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def truncate_error(text: Optional[str]) -> Optional[str]:
    """把错误信息压到 ≤120 字（JSON 里只放摘要，完整 traceback 走日志）。"""
    if text is None:
        return None
    s = str(text).replace("\n", " ").replace("\r", " ").strip()
    return s[:ERROR_MAX_LEN] if len(s) > ERROR_MAX_LEN else s


# ────────────────────────────────────────────────────────────
# 读
# ────────────────────────────────────────────────────────────
def read() -> Dict[str, Any]:
    """读运行状态。文件不存在/损坏返回空 dict（调用方按「从未跑过」处理）。"""
    return _read_json(_run_path())


def last_calc() -> Dict[str, Any]:
    """派生「上次计算」三元组（页面文案唯一来源）。

    Returns:
        {"at": ISO 字符串或 None, "ok": bool 或 None, "error": str 或 None,
         "running": bool}
        `at` = finishedAt（未结算过则 startedAt）。只统计**顶层**（已结算）运行。
    """
    data = read()
    if not data:
        return {"at": None, "ok": None, "error": None, "running": False,
                "partial": False}
    at = data.get("finishedAt") or data.get("startedAt")
    ok = data.get("ok")
    return {"at": at,
            "ok": bool(ok) if isinstance(ok, bool) else None,
            "error": data.get("error"),
            "running": bool(data.get("running")),
            # `partial` = 「主体产物成功、个别环节缺」
            # （目前只有一种：滚动验证本次无有效期）。它**不改变** ok，
            # 页面因此不会亮红灯，但可以在滚动卡片上写一行灰字交代。
            "partial": bool(data.get("partial"))}


def running_age_sec(running: Any, now: Optional[str] = None) -> Optional[float]:
    """`running.startedAt` 距今秒数；无 running / 解析失败返回 None。"""
    if not isinstance(running, dict) or not running:
        return None
    started = running.get("startedAt")
    if not started:
        return None
    try:
        a = time.mktime(time.strptime(str(started), ISO_FMT))
        b = time.mktime(time.strptime(now or iso_now(), ISO_FMT))
    except (ValueError, OverflowError):
        return None
    return b - a


def running_is_stale(running: Any, now: Optional[str] = None) -> bool:
    """`running` 是否已失效（超 `RUNNING_STALE_SEC`，或时间戳根本解析不出来）。

    **拿不准就算失效**：一个「不知道跑了多久」的 running 和「跑了 3 小时」的
    running 对页面是一样的灾难（永久卡在「重算中」），宁可当作没有进程在跑。
    `startedAt` 落在未来（系统时间被往回调过）同样算失效——那个时间戳不可信。
    """
    if not isinstance(running, dict) or not running:
        return False
    age = running_age_sec(running, now)
    return age is None or age < 0 or age > RUNNING_STALE_SEC


def mark_skipped(reason: str, at: Optional[str] = None) -> Dict[str, Any]:
    """记录「本次**根本没开始算**」（调度器门禁拦住，例如 K 线还没更新）。

    只写独立字段 `skipped`，**不动** `ok` / `finishedAt` / `lastCalcAt`：
    那三个描述的是「上一次真实计算」，把它们改成调度器的时刻就是在撒谎 ——
    页面会显示「上次计算时间 = 16:10」，可实际上 16:10 什么都没算。

    与 `ok=false` 的区别是本文件的核心语义：
        * `ok=false`   = 算了，但出错（`error` 有值）
        * `skipped`    = 没算（数据旧就是旧，`state` 仍按 `lag` 判 stale）
    """
    data = read()
    data["skipped"] = {"at": at or iso_now(), "reason": truncate_error(str(reason))}
    _write_json_atomic(_run_path(), data)
    return data


# ────────────────────────────────────────────────────────────
# 写：begin / set_phase / finish
# ────────────────────────────────────────────────────────────
def begin(run_id: str, target_dates: Optional[list] = None,
          trigger: str = "manual",
          now: Optional[str] = None) -> Dict[str, Any]:
    """标记一次运行开始：**只写 running**，顶层（上次已结算的运行）原样保留。

    Args:
        run_id: 本次运行标识，建议 `YYYYMMDDTHHMMSS`。
        target_dates: 本次要算的日期；None 表示待 plan 后再补。
        trigger: `scheduler` / `manual` / `retry`。
        now: 覆盖「现在」（ISO，测试用）；None 取系统时间。

    Returns:
        写入后的完整文件内容。
    """
    data = read()
    stale = data.get("running")
    if running_is_stale(stale, now):
        # B：状态文件本身要干净。不清的话，这个脏 running 会一直躺在磁盘上，
        # 每次读都判一次过期（无害但一直脏），而且下次 `begin` 的 running
        # 会把它覆盖掉却没人记得「上一次其实没正常结束」。
        data["warnings"] = list(data.get("warnings") or []) + [
            truncate_error("清掉上次残留的 running（startedAt=%s，已超过 %d 分钟）"
                           % (stale.get("startedAt"), RUNNING_STALE_SEC // 60))]
        data["staleRunning"] = dict(stale)
    data["running"] = {
        "runId": run_id,
        "startedAt": iso_now(),
        "phase": "begin",
        "targetDates": list(target_dates or []),
        "trigger": trigger,
        "pid": os.getpid(),
    }
    _write_json_atomic(_run_path(), data)
    return data


def set_phase(phase: str, **extra: Any) -> Dict[str, Any]:
    """更新进行中运行的阶段（`plan/compute/cache/rolling/score/finish`）。

    额外字段用关键字传入（例如 `dates=3`、`rows=120720`），会并进 running。
    running 不存在时（说明没走 begin，属于异常路径）自动补一个最小 running。
    """
    data = read()
    running = data.get("running")
    if not isinstance(running, dict) or not running:
        running = {"runId": time.strftime("%Y%m%dT%H%M%S"), "startedAt": iso_now(),
                   "phase": "begin", "targetDates": [], "trigger": "manual",
                   "pid": os.getpid()}
    running["phase"] = str(phase)
    for key, value in extra.items():
        running[key] = value
    data["running"] = running
    _write_json_atomic(_run_path(), data)
    return data


def finish(ok: bool, error: Optional[str] = None, cost: Optional[float] = None,
           verify: Optional[Dict[str, Any]] = None,
           trigger: Optional[str] = None,
           target_dates: Optional[list] = None,
           partial: bool = False,
           warnings: Optional[list] = None) -> Dict[str, Any]:
    """结算本次运行：running 升到顶层，并清空 running。

    Args:
        ok: 主体是否成功。False 时 `error` 必须有值（共享知识第 8 条）。
        error: 失败原因，会被截到 ≤120 字。
        cost: 本次耗时（秒）。
        verify: 校验结果（原样写进 JSON，供页面展示）。
        trigger: 覆盖触发来源；None 表示沿用 running 里的值。
        target_dates: 覆盖目标日期；None 表示沿用 running 里的值。
        partial: `ok=True` 但有环节缺失（目前只有「滚动验证本次无有效期」）。
            **`ok=True, partial=True` 是合法组合**：产物已经写出去了，
            页面必须显示「已更新」而不是「更新失败」，只在缺的那块写一行说明。
        warnings: 缺失环节的人话说明（每条 ≤120 字，逐条截断）。

    Returns:
        写入后的完整文件内容。
    """
    data = read()
    running = data.get("running") if isinstance(data.get("running"), dict) else {}
    run_id = running.get("runId") or time.strftime("%Y%m%dT%H%M%S")
    started = running.get("startedAt") or iso_now()
    finished = iso_now()

    if cost is None:
        cost = _diff_seconds(started, finished)

    data.update({
        "runId": run_id,
        "startedAt": started,
        "finishedAt": finished,
        "targetDates": list(target_dates if target_dates is not None
                            else (running.get("targetDates") or [])),
        "ok": bool(ok),
        # 失败时 partial 无意义（红字已经说明一切），强制归零，避免出现
        # 「ok=false 但 partial=true」这种自相矛盾的状态卡在页面上
        "partial": bool(partial) and bool(ok),
        "warnings": [truncate_error(str(w)) for w in (warnings or []) if str(w).strip()],
        "cost": round(float(cost), 1),
        "error": truncate_error(error),
        "trigger": trigger or running.get("trigger") or "manual",
        "verify": verify if verify is not None else None,
        # 真跑过一次，之前那条「本次未开始」就过期了，必须清掉，
        # 否则页面会在成功之后仍附着一条旧的「本次未开始：K 线未更新」。
        "skipped": None,
        "running": None,
    })
    _write_json_atomic(_run_path(), data)
    return data


def _diff_seconds(started: str, finished: str) -> float:
    """两个 ISO 本地时间字符串之差（解析失败返回 0.0）。"""
    try:
        a = time.mktime(time.strptime(started, ISO_FMT))
        b = time.mktime(time.strptime(finished, ISO_FMT))
        return max(0.0, b - a)
    except (ValueError, OverflowError):
        return 0.0


def reset() -> None:
    """删除运行状态文件（测试用；生产不要调）。"""
    try:
        os.remove(_run_path())
    except OSError:
        pass


# ────────────────────────────────────────────────────────────
# 运行锁
# ────────────────────────────────────────────────────────────
def _pid_alive(pid: int) -> bool:
    """进程是否还活着。

    Windows 上**绝对不能**用 `os.kill(pid, 0)` —— Python 在 Windows 下把
    除 CTRL_C_EVENT/CTRL_BREAK_EVENT 以外的信号直接交给 TerminateProcess，
    那会真的把那个进程杀掉。这里用 OpenProcess + WaitForSingleObject 探测。
    """
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            synchronize = 0x00100000
            wait_timeout = 0x00000102
            kernel32 = ctypes.windll.kernel32                    # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(synchronize, False, pid)
            if not handle:
                return False
            try:
                rc = kernel32.WaitForSingleObject(wintypes.HANDLE(handle), 0)
                return rc == wait_timeout                        # 未 signal = 还在跑
            finally:
                kernel32.CloseHandle(wintypes.HANDLE(handle))
        except Exception:                                        # noqa: BLE001
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def acquire_lock(ttl: int = LOCK_TTL, run_id: Optional[str] = None) -> Dict[str, Any]:
    """抢占运行锁。

    Args:
        ttl: 锁的最长有效期（秒）。超过即视为陈旧可抢占。
        run_id: 本次运行标识，写进锁文件便于排查。

    Returns:
        {"ok": bool, "token": ..., "holder": {...} 或 None, "reason": str 或 None}
        `ok=False` 时调用方应 `exit 3`（共享知识第 6 条）。
    """
    path = _lock_path()
    now = time.time()
    holder = _read_json(path)
    if holder:
        age = now - float(holder.get("acquiredEpoch") or 0.0)
        holder_pid = holder.get("pid")
        if age <= float(ttl) and holder_pid and _pid_alive(int(holder_pid)):
            if int(holder_pid) != os.getpid():
                return {"ok": False, "token": None, "holder": holder,
                        "reason": "另一份因子重算正在运行（pid=%s，已持续 %.0f 秒）"
                                  % (holder_pid, age)}
            # 同进程重入：刷新时间戳后放行（便于测试与脚本内连续调用）
        elif age <= float(ttl) and not holder_pid:
            return {"ok": False, "token": None, "holder": holder,
                    "reason": "运行锁被占用（锁文件无 pid，%.0f 秒前写入）" % age}

    token = "%s-%d" % (run_id or time.strftime("%Y%m%dT%H%M%S"), os.getpid())
    _write_json_atomic(path, {"token": token, "pid": os.getpid(),
                              "acquiredAt": iso_now(), "acquiredEpoch": now,
                              "ttl": int(ttl), "runId": run_id})
    return {"ok": True, "token": token, "holder": None, "reason": None,
            "stolen": bool(holder)}


def release_lock(token: Optional[str] = None) -> bool:
    """释放运行锁。token 不匹配则不删（防止误删别人刚抢到的锁）。"""
    path = _lock_path()
    holder = _read_json(path)
    if not holder:
        return False
    if token is not None and holder.get("token") != token:
        return False
    try:
        os.remove(path)
        return True
    except OSError:
        return False


def lock_status() -> Dict[str, Any]:
    """只读查看锁状态（供状态接口使用）。"""
    holder = _read_json(_lock_path())
    if not holder:
        return {"locked": False, "holder": None}
    age = time.time() - float(holder.get("acquiredEpoch") or 0.0)
    alive = _pid_alive(int(holder.get("pid") or 0))
    return {"locked": bool(age <= float(holder.get("ttl") or LOCK_TTL) and alive),
            "holder": holder, "ageSec": round(age, 1), "pidAlive": alive}


__all__ = [
    "EXIT_EXPORT", "EXIT_INVALID", "EXIT_LOCKED", "EXIT_NO_PERIOD", "EXIT_OK",
    "EXIT_TIMEOUT", "LOCK_TTL", "acquire_lock", "begin", "finish", "iso_now",
    "last_calc", "lock_status", "read", "release_lock", "reset", "set_phase",
    "truncate_error",
]
