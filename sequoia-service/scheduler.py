#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
scheduler.py — 定时任务调度（增量更新 + 策略扫描 + 因子重算）

做三件重复劳动的事：
  1) 每个交易日收盘后，把当日行情增量补进本地库（腾讯源，全市场约 1 分钟）
  2) 增量完成后自动跑一遍全市场策略扫描，结果落盘供页面直接读
  3) 16:10 跑一遍因子重算（`run_factor_pipeline.py`：增量面板 → 缓存 →
     因子时间序列 → 滚动验证 → 打分 → 导出），约 11 分钟

状态写 data/scheduler_state.json，服务重启后不会重复执行同一天的任务。
设计上刻意用子进程跑长任务（bulk_backfill.py / run_factor_pipeline.py），
避免长任务卡住 HTTP 服务。

因子任务的两级去重（为什么需要两级）
------------------------------------
    * 尝试级 `last_factor_attempt_date`：一次 tick 的间隔是 30 秒，
      没有它的话「K 线还没到」这个状态会让我们每 30 秒起一次子进程，
      日志瞬间被刷满，而且用户从页面上分不清「今天没跑」和「在反复重试」。
    * 成功级 `last_factor_ok`：sync 迟到成功之后，同一天应该允许补跑
      一次因子（否则「15:40 增量失败 → 16:10 因子锚在旧数据上」会一直错到明天）。
      所以尝试级只挡「同一个 sync 状态下的重复尝试」，sync 状态一变就放行。
"""
import json
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

# 「下一次几点跑」的唯一实现：`freshness.nextRunAt` 与本模块的
# `/api/scheduler/status.next_*` 必须调同一个函数，否则两处时间会对不上。
import sched_time                                   # noqa: E402

DATA_DIR = os.path.join(HERE, "data")
STATE_PATH = os.path.join(DATA_DIR, "scheduler_state.json")
LOG_PATH = os.path.join(DATA_DIR, "scheduler.log")

# 因子重算的硬超时。实测全链路 ~11 分钟（含滚动验证），冷启动首次 ~4 分钟，
# 给到 60 分钟是「宁可报超时，也不要挂一个僵尸子进程占着锁」。
FACTOR_TIMEOUT = 60 * 60
# 尝试级去重的兜底间隔：同一个 tick 周期内不重复起子进程
ATTEMPT_GAP_SEC = 30
# 失败退避：同一个 sync 状态下今天已经失败过，这么久之内不再重试。
# 一次失败的重试代价是「又一遍 11 分钟 + 冷启动读 54.7 秒价格宽表」，
# 30 秒一retry 会在半小时内把磁盘和 CPU 烧干净，日志也没法看。
FAIL_RETRY_GAP_SEC = 600

DEFAULT_CFG = {
    "enabled": True,
    "sync_time": "15:40",      # 收盘后补当日 K 线
    "scan_time": "15:50",      # 补完数据后跑策略
    "factor_time": "16:10",    # 因子重算（要等 sync 落库，故排在 scan 之后）
    "catch_up": True,          # 启动时若已过时间点且今天未跑，补跑一次
    "source": "tencent",
    "workers": 10,
}


def _now():
    return datetime.now()


def _today():
    return _now().strftime("%Y-%m-%d")


def _hm(str_val):
    """`"16:10"` → `(16, 10)`；非法值回落 15:40。

    实现已抽到 `sched_time._hm()`：那里是「下一次几点跑」的唯一实现，
    `freshness.nextRunAt` 与 `/api/scheduler/status.next_*` 必须同源，
    否则页面与状态接口迟早会给出两个不同的时间。
    """
    return sched_time._hm(str_val)


def _next_run(now, hhmm, done_today):
    """下一次执行时间 `"YYYY-MM-DD HH:MM"`（实现见 `sched_time._next_run`）。"""
    return sched_time._next_run(now, hhmm, done_today)


def _seconds_since(iso_text, now):
    """`"2026-11-16T16:10:00"` → 距今秒数；解析失败或时间在未来返回 None。

    返回 None（而不是负数）是刻意的：系统时间被往回调时，负数会让
    「%.0f 秒前刚尝试过」变成「-3600 秒前」，去重逻辑会莫名其妙地一直挡住。
    """
    if not iso_text:
        return None
    try:
        delta = (now - datetime.strptime(str(iso_text), "%Y-%m-%dT%H:%M:%S")).total_seconds()
    except (ValueError, TypeError):
        return None
    return delta if delta >= 0 else None


def _factor_run_info():
    """读 `factor_run.json`（编排层写的运行状态）。读不到就返回空 dict。"""
    try:
        import factor_run as FR
        return FR.read() or {}
    except Exception:                                # noqa: BLE001
        return {}


def _factor_error(proc, today):
    """失败原因：优先用 `factor_run.json` 里的 `error`（编排层给的病因，含中文），
    其次子进程 stderr 最后一行。

    **只认今天的 runId**（`YYYYMMDD` 前缀）：否则一次启动即崩的失败会把**昨天**
    的错误信息当今天报出来，排障方向直接带偏。
    """
    info = _factor_run_info()
    run_id = str(info.get("runId") or "")
    if info.get("error") and run_id[:8] == str(today).replace("-", ""):
        return str(info["error"])[:120]
    lines = [ln.strip() for ln in (proc.stderr or "").strip().splitlines() if ln.strip()]
    if lines:
        return lines[-1][:120]
    return "子进程返回码 %s" % getattr(proc, "returncode", "?")


def _log(msg):
    line = "[%s] %s" % (_now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(LOG_PATH, "a", encoding="utf-8") as f:
            f.write(line + "\n")
        # 控制日志体积
        if os.path.getsize(LOG_PATH) > 512 * 1024:
            with open(LOG_PATH, "r", encoding="utf-8") as f:
                tail = f.readlines()[-500:]
            with open(LOG_PATH, "w", encoding="utf-8") as f:
                f.writelines(tail)
    except Exception:
        pass
    try:
        print(line, flush=True)
    except Exception:
        pass


def _load_state():
    try:
        with open(STATE_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save_state(st):
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(STATE_PATH, "w", encoding="utf-8") as f:
            json.dump(st, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


class Scheduler:
    def __init__(self, cfg_path=None):
        self.cfg_path = cfg_path or os.path.join(DATA_DIR, "scheduler_config.json")
        self.cfg = dict(DEFAULT_CFG)
        self._load_cfg()
        self.state = _load_state()
        self.lock = threading.Lock()
        self.busy = {"sync": False, "scan": False, "factor": False}
        self.last_error = None
        self.thread = None
        self.stopping = False

    # ── 配置 ──
    def _load_cfg(self):
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                d = json.load(f)
            for k in DEFAULT_CFG:
                if k in d:
                    self.cfg[k] = d[k]
        except Exception:
            pass

    def save_cfg(self, patch):
        with self.lock:
            for k, v in (patch or {}).items():
                if k in DEFAULT_CFG:
                    self.cfg[k] = v
            try:
                os.makedirs(os.path.dirname(self.cfg_path), exist_ok=True)
                with open(self.cfg_path, "w", encoding="utf-8") as f:
                    json.dump(self.cfg, f, ensure_ascii=False, indent=2)
            except Exception:
                pass
        return self.cfg

    # ── 启动 ──
    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stopping = False
        self.thread = threading.Thread(target=self._loop, daemon=True, name="scheduler")
        self.thread.start()
        _log("调度器已启动：增量 %s / 扫描 %s / 因子 %s（enabled=%s）"
             % (self.cfg["sync_time"], self.cfg["scan_time"],
                self.cfg["factor_time"], self.cfg["enabled"]))

    def stop(self):
        self.stopping = True

    # ── 主循环：每分钟检查一次，用「今天是否已执行」去重 ──
    def _loop(self):
        time.sleep(5)          # 等主服务起来
        while not self.stopping:
            try:
                self._tick()
            except Exception as e:
                self.last_error = str(e)
                _log("调度循环异常：%s" % e)
            time.sleep(30)

    def _tick(self):
        if not self.cfg.get("enabled"):
            return
        now = _now()
        if now.weekday() >= 5:          # 周末不跑
            return
        today = _today()
        st = self.state

        sync_h, sync_m = _hm(self.cfg["sync_time"])
        scan_h, scan_m = _hm(self.cfg["scan_time"])

        sync_due = (now.hour, now.minute) >= (sync_h, sync_m)
        scan_due = (now.hour, now.minute) >= (scan_h, scan_m)

        # 首次启动补跑：若已过点且今天没跑过
        if self.cfg.get("catch_up") and st.get("catch_up_done") != today:
            st["catch_up_done"] = today
            _save_state(st)
            if sync_due and st.get("last_sync_date") != today:
                _log("启动补跑：今日增量尚未执行，立即开始")
                self.run_sync()
            if scan_due and st.get("last_scan_date") != today:
                _log("启动补跑：今日扫描尚未执行，立即开始")
                self.run_scan()

        if sync_due and st.get("last_sync_date") != today and not self.busy["sync"]:
            self.run_sync()
        if scan_due and st.get("last_scan_date") != today and not self.busy["scan"]:
            self.run_scan()

        # 因子重算：门禁（两级去重 + sync 就绪）通过才起子进程。
        # 放在最后是因为它依赖 sync 落库，且不该拖慢前面两件事的判定。
        gate = self._factor_gate(now, today)
        if gate.get("run"):
            self.run_factor(trigger="scheduler")
        elif gate.get("block"):
            self._record_factor_blocked(today, gate)
        elif gate.get("backoff"):
            self._log_factor_backoff(gate)

    # ── 任务：增量同步 ──
    def run_sync(self):
        if self.busy["sync"]:
            return {"ok": False, "message": "增量同步已在执行"}
        self.busy["sync"] = True
        t0 = time.time()
        try:
            src = self.cfg.get("source", "tencent")
            # baostock 有额度与黑名单限制：冻结期内直接跳过，不去撞枪口；
            # 也不自动切到腾讯源——两种复权口径不能混在一张表里。
            if src == "baostock":
                try:
                    import bs_guard
                    g = bs_guard.Guard()
                    if g.blacklist_until():
                        raise RuntimeError(
                            "baostock 冻结至 %s（还剩 %.1f 小时），本次增量跳过；"
                            "如需继续更新请把 source 改成 tencent（注意前复权/后复权口径不同）"
                            % (g.blacklist_until().strftime("%Y-%m-%d %H:%M"),
                               g.blacklist_remaining_hours()))
                except ImportError:
                    pass
            cmd = [sys.executable, os.path.join(HERE, "bulk_backfill.py"),
                   "--incremental", "--source", src,
                   "--workers", str(self.cfg.get("workers", 10))]
            _log("开始增量同步：%s" % " ".join(cmd[1:]))
            p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                               timeout=60 * 90, encoding="utf-8", errors="ignore")
            tail = (p.stdout or "")[-800:]
            ok = p.returncode == 0
            if not ok:
                self.last_error = (p.stderr or "")[-400:]
                _log("增量同步失败 rc=%s：%s" % (p.returncode, (p.stderr or "")[-300:]))
            else:
                _log("增量同步完成，耗时 %.0fs" % (time.time() - t0))
            self.state["last_sync_date"] = _today()
            self.state["last_sync_at"] = _now().isoformat(timespec="seconds")
            self.state["last_sync_ok"] = ok
            self.state["last_sync_cost"] = round(time.time() - t0)
            self.state["last_sync_tail"] = tail
            _save_state(self.state)
            return {"ok": ok, "output": tail}
        except subprocess.TimeoutExpired:
            self.last_error = "增量同步超时（90 分钟）"
            _log(self.last_error)
            return {"ok": False, "message": self.last_error}
        except Exception as e:
            self.last_error = str(e)
            _log("增量同步异常：%s" % e)
            return {"ok": False, "message": str(e)}
        finally:
            self.busy["sync"] = False

    # ── 任务：策略扫描（由 server 注入回调，避免循环依赖）──
    def set_scan_runner(self, fn):
        self._scan_runner = fn

    def run_scan(self):
        if self.busy["scan"]:
            return {"ok": False, "message": "扫描已在执行"}
        fn = getattr(self, "_scan_runner", None)
        if not fn:
            return {"ok": False, "message": "扫描执行器未注册"}
        self.busy["scan"] = True
        t0 = time.time()
        try:
            _log("开始定时扫描（全市场 7 策略）")
            res = fn("all")
            self.state["last_scan_date"] = _today()
            self.state["last_scan_at"] = _now().isoformat(timespec="seconds")
            self.state["last_scan_ok"] = True
            self.state["last_scan_total"] = (res or {}).get("total", 0)
            self.state["last_scan_cost"] = round(time.time() - t0)
            _save_state(self.state)
            _log("定时扫描完成：命中 %s 只，耗时 %.0fs"
                 % ((res or {}).get("total", 0), time.time() - t0))
            return {"ok": True, "result": res}
        except Exception as e:
            self.last_error = str(e)
            self.state["last_scan_date"] = _today()
            self.state["last_scan_ok"] = False
            _save_state(self.state)
            _log("定时扫描失败：%s" % e)
            return {"ok": False, "message": str(e)}
        finally:
            self.busy["scan"] = False

    # ── 任务：因子重算（子进程，约 11 分钟）──
    def _factor_gate(self, now, today):
        """因子重算的两级去重 + K 线门禁。

        Returns:
            `{"run": True}` —— 该跑了；
            `{"run": False, "block": True, "why", "sig", "reason"}` —— 这次要记一笔
            「没跑成」（写进 `scheduler_state.json`，页面才看得到）；
            `{"run": False, "reason"}` —— **静默**跳过（已跑过 / 没到点 / 正在跑 /
            同一个 sync 状态下已经在等）。

        为什么「同一 sync 状态下在等」必须静默：tick 每 30 秒一次，若每次都记一笔
        failed，`scheduler.log` 半小时就是 60 条一模一样的错误，而且用户从页面上
        分不清「今天没跑」和「在反复重试」。所以尝试级去重记的是**状态快照**
        （`sync_ok|sync_date`），而不是「今天试过了」——快照一变（sync 迟到成功）
        就放行，这才是「允许同日补跑」的正确实现。
        """
        st = self.state
        hhmm = self.cfg.get("factor_time") or DEFAULT_CFG["factor_time"]

        # 成功级去重：今天已经成功跑过
        if st.get("last_factor_ok") is True and st.get("last_factor_date") == today:
            return {"run": False, "reason": "今日已完成"}

        if not sched_time.due(now, hhmm):
            return {"run": False, "reason": "未到 %s" % hhmm}

        if self.busy.get("factor"):
            return {"run": False, "reason": "已在执行"}

        # 尝试级去重（兜底）：子进程可能瞬间崩掉，别在 30 秒内连起两个
        gap = _seconds_since(st.get("last_factor_attempt_at"), now)
        if gap is not None and gap < ATTEMPT_GAP_SEC:
            return {"run": False, "reason": "%.0f 秒前刚尝试过" % gap}

        # K 线门禁：sync 没成功、或不是今天的，因子算出来也是锚在旧数据上。
        # 「宁可不跑，也不要用昨天的行情算出今天的名单」。
        sig = "%s|%s" % (st.get("last_sync_ok"), st.get("last_sync_date"))
        ready = (st.get("last_sync_ok") is True and st.get("last_sync_date") == today)
        if not ready:
            if st.get("last_factor_sig") == sig:
                return {"run": False, "reason": "K 线仍未更新（已记过一次）"}
            state_text = ("false" if st.get("last_sync_ok") is False
                          else "未执行（%s）" % (st.get("last_sync_date") or "从未"))
            return {"run": False, "block": True, "why": "sync_not_ready", "sig": sig,
                    "reason": "K 线未更新（sync_ok=%s）" % state_text}

        # 失败退避：今天在**同一个 sync 状态**下已经失败过，10 分钟内不再重试。
        # 没有这一条，一次 exit 2 会演变成「每 30 秒起一个子进程」——
        # 每个子进程都要先把价格宽表读进内存（冷启动 54.7 秒），
        # 半小时能把磁盘和 CPU 全烧掉，日志也彻底没法看。
        # sync 状态一变（迟到成功 / 换了一天）sig 就不等了，立刻放行。
        if (st.get("last_factor_ok") is False and st.get("last_factor_date") == today
                and st.get("last_factor_sig") == sig):
            # `gap is None`（时间戳缺失/系统时间被回调）时**放行**：
            # 拿不到时长就退避等于永久锁死，宁可多试一次也不要卡到明天。
            # 「可能浪费资源」和「可能永久不出数」之间，永远选前者。
            if gap is not None and gap < FAIL_RETRY_GAP_SEC:
                until = (now + timedelta(seconds=max(0.0, FAIL_RETRY_GAP_SEC - gap)))
                return {"run": False,
                        "backoff": True,
                        # 给日志用的人话时刻（运维要知道「等到几点」）
                        "until": until.strftime("%H:%M"),
                        # 日志去重键 = `sync 快照|上次尝试时刻`：任何一次真正重试
                        # 都会换一个新键，于是「新的一次失败」重新记一行，
                        # 而「同一状态下每 30 秒 tick」不会刷屏。
                        "key": "%s|%s" % (sig, st.get("last_factor_attempt_at") or ""),
                        "reason": "上次失败，%d 分钟内不重试" % (FAIL_RETRY_GAP_SEC // 60)}
        return {"run": True, "reason": "门禁通过", "sig": sig}

    def _log_factor_backoff(self, gate):
        """退避状态在日志里留一行（**每次失败状态只记一次**）。

        为什么非记不可：退避是静默等待，不写日志的话运维看到的是「因子重算失败」
        然后什么都没发生，会以为进程死了。**静默的等待和静默的卡死，在日志上长得
        一样**——而这两件事的处置完全不同（前者等着就行，后者要立刻去查）。
        """
        key = str(gate.get("key") or "")
        st = self.state
        if st.get("last_factor_backoff_key") == key:
            return                                    # 同一状态已记过，不刷屏
        st["last_factor_backoff_key"] = key
        _save_state(st)
        _log("因子重算已失败，退避至 %s 前不重试（%s）"
             % (gate.get("until") or "?", gate.get("reason") or ""))

    def _record_factor_blocked(self, today, gate):
        """门禁没过：记一次「没跑成」。

        **`last_factor_attempt_date` 必须写**（team-lead 明确要求）：不写的话
        16:10 之后每个 tick（30 秒）都会重试一次，日志被刷满，而且用户从页面上
        看不出是「没跑」还是「在反复重试」。
        """
        st = self.state
        reason = str(gate.get("reason") or "门禁未通过")[:120]
        st["last_factor_date"] = today
        st["last_factor_at"] = _now().isoformat(timespec="seconds")
        st["last_factor_ok"] = False
        st["last_factor_error"] = reason
        st["last_factor_attempt_date"] = today
        st["last_factor_attempt_at"] = _now().isoformat(timespec="seconds")
        st["last_factor_sig"] = gate.get("sig")
        _save_state(st)
        # 同时告诉**页面**：往 `factor_run.json` 写一个独立字段 `skipped`。
        # 刻意不动 `ok` / `finishedAt` / `lastCalcAt` —— 那三个描述的是
        # 「上一次真实计算」，改成调度器时刻就是在撒谎（页面会显示
        # 「上次计算 16:10」，可 16:10 什么都没算）。
        # `freshness` 会把这条渲染成「已过期 1 个交易日 · 本次未开始：K 线未更新」，
        # 既不是笼统的红字「更新失败」，也不是干巴巴的「已过期」却不给原因。
        try:
            import factor_run as FR
            # `at` 用调度器自己的 `_now()`（而不是 factor_run 的系统时钟），
            # 保证「门禁判断的时刻」和「写进文件的时刻」是同一个，测试里也能打桩。
            FR.mark_skipped(reason, at=_now().strftime("%Y-%m-%dT%H:%M:%S"))
        except Exception as exc:                    # noqa: BLE001
            _log("写 skipped 失败（不影响调度）：%s" % exc)
        _log("因子重算未执行：%s" % reason)
        return {"ok": False, "message": reason}

    def run_factor(self, trigger="scheduler"):
        """起子进程跑一次因子重算（`run_factor_pipeline.py`）。"""
        if self.busy.get("factor"):
            return {"ok": False, "message": "因子重算已在执行"}
        self.busy["factor"] = True
        t0 = time.time()
        today = _today()
        st = self.state
        # 先记「尝试」，再起进程：进程被强杀时也要留下痕迹，否则重启后
        # 看不出「今天其实试过」，会立刻再起一次。
        st["last_factor_attempt_date"] = today
        st["last_factor_attempt_at"] = _now().isoformat(timespec="seconds")
        st["last_factor_sig"] = "%s|%s" % (st.get("last_sync_ok"),
                                           st.get("last_sync_date"))
        # 新一轮尝试：上次的「退避已记过」标记作废，下一次失败要重新留一行日志
        st["last_factor_backoff_key"] = None
        _save_state(st)
        try:
            cmd = [sys.executable, os.path.join(HERE, "run_factor_pipeline.py"),
                   "--trigger", trigger]
            _log("开始因子重算：%s" % " ".join(cmd[1:]))
            p = subprocess.run(cmd, cwd=HERE, capture_output=True, text=True,
                               timeout=FACTOR_TIMEOUT, encoding="utf-8", errors="ignore")
            ok = p.returncode == 0
            tail = (p.stdout or "")[-800:]
            st["last_factor_date"] = today
            st["last_factor_at"] = _now().isoformat(timespec="seconds")
            st["last_factor_ok"] = ok
            st["last_factor_cost"] = round(time.time() - t0)
            st["last_factor_rc"] = p.returncode
            st["last_factor_tail"] = tail
            st["last_factor_error"] = None if ok else _factor_error(p, today)
            partial = _factor_run_info()
            st["last_factor_partial"] = bool(partial.get("partial"))
            st["last_factor_warnings"] = list(partial.get("warnings") or [])
            _save_state(st)
            if ok:
                _log("因子重算完成，耗时 %.0fs%s"
                     % (time.time() - t0,
                        "（partial：%s）" % "；".join(st["last_factor_warnings"])
                        if st["last_factor_partial"] else ""))
            else:
                self.last_error = st["last_factor_error"]
                _log("因子重算失败 rc=%s：%s" % (p.returncode, st["last_factor_error"]))
            return {"ok": ok, "output": tail, "rc": p.returncode}
        except subprocess.TimeoutExpired:
            msg = "因子重算超时（%d 分钟）" % (FACTOR_TIMEOUT // 60)
            self.last_error = msg
            st["last_factor_date"] = today
            st["last_factor_at"] = _now().isoformat(timespec="seconds")
            st["last_factor_ok"] = False
            st["last_factor_error"] = msg
            st["last_factor_cost"] = round(time.time() - t0)
            _save_state(st)
            _log(msg)
            return {"ok": False, "message": msg}
        except Exception as e:                      # noqa: BLE001
            self.last_error = str(e)
            st["last_factor_date"] = today
            st["last_factor_ok"] = False
            st["last_factor_error"] = str(e)[:120]
            st["last_factor_cost"] = round(time.time() - t0)
            _save_state(st)
            _log("因子重算异常：%s" % e)
            return {"ok": False, "message": str(e)}
        finally:
            self.busy["factor"] = False

    # ── 状态 ──
    def status(self):
        now = _now()
        st = self.state
        today = now.strftime("%Y-%m-%d")
        factor_done_today = (st.get("last_factor_ok") is True
                             and st.get("last_factor_date") == today)
        return {
            "ok": True,
            "enabled": bool(self.cfg.get("enabled")),
            "sync_time": self.cfg["sync_time"],
            "scan_time": self.cfg["scan_time"],
            "factor_time": self.cfg.get("factor_time"),
            "source": self.cfg.get("source"),
            "workers": self.cfg.get("workers"),
            "running": self.thread.is_alive() if self.thread else False,
            "busy": dict(self.busy),
            "now": now.isoformat(timespec="seconds"),
            "weekday": now.weekday(),
            "is_trading_day": now.weekday() < 5,
            "last_sync_date": st.get("last_sync_date"),
            "last_sync_at": st.get("last_sync_at"),
            "last_sync_ok": st.get("last_sync_ok"),
            "last_sync_cost": st.get("last_sync_cost"),
            "last_scan_date": st.get("last_scan_date"),
            "last_scan_at": st.get("last_scan_at"),
            "last_scan_total": st.get("last_scan_total"),
            "last_scan_cost": st.get("last_scan_cost"),
            # 因子重算（键名与 sync/scan 一致，前缀统一 last_factor_）
            "last_factor_date": st.get("last_factor_date"),
            "last_factor_at": st.get("last_factor_at"),
            "last_factor_ok": st.get("last_factor_ok"),
            "last_factor_cost": st.get("last_factor_cost"),
            "last_factor_error": st.get("last_factor_error"),
            "last_factor_attempt_date": st.get("last_factor_attempt_date"),
            # 主体成功但滚动验证没跑成时，页面显示「数据已更新」+ 一行灰字
            "last_factor_partial": bool(st.get("last_factor_partial")),
            "last_factor_warnings": list(st.get("last_factor_warnings") or []),
            "last_error": self.last_error,
            "next_sync": _next_run(now, self.cfg["sync_time"],
                                   st.get("last_sync_date") == today),
            "next_scan": _next_run(now, self.cfg["scan_time"],
                                   st.get("last_scan_date") == today),
            "next_factor": _next_run(now, self.cfg.get("factor_time") or
                                     DEFAULT_CFG["factor_time"], factor_done_today),
        }
