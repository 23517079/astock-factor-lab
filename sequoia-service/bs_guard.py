#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
bs_guard.py — baostock 访问额度闸门（防黑名单）

为什么需要这个文件：
    baostock 的规则写得很清楚（官网「访问限制」）：
      1. 每日 API 请求不能超过 5 万次，超过进黑名单；
      2. **不能并发连接访问**；
      3. 本自然年首次被限制冻结 6 小时，多次则「限制时长 = 本年累计被限次数 × 6 小时」。

    2026-11-13 我用 8 进程并发登录，直接把账号打进了黑名单（10001011）。
    这个模块的作用就是：任何走 baostock 的调用都必须先过闸门——
      · 单进程串行（MAX_CONCURRENCY = 1）
      · 每次调用之间强制间隔 MIN_INTERVAL 秒
      · 每日累计请求数落盘，逼近 5 万前就停（SAFE_LIMIT 留 16% 余量）
      · 一旦拿到 10001011，立刻记账并写下解封时间，后续直接拒绝运行

用法（代码内）：
    from bs_guard import Guard
    g = Guard()
    g.require_can_run(n_planned)     # 不够额度就抛 QuotaError，别硬跑
    g.spend(1)                       # 每发起一次请求记一次
    g.note_blacklist()               # 命中 10001011 时调用

命令行：
    python bs_guard.py status        # 看今日已用/剩余额度、黑名单解封时间
    python bs_guard.py reset         # 手动清零今日计数（确认是自己搞错时才用）
"""
import json
import os
import sys
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
# 允许用环境变量指向别处，便于测试时不污染真实额度文件
QUOTA_PATH = os.environ.get("BS_QUOTA_PATH") or os.path.join(HERE, "data", "bs_quota.json")

DAILY_LIMIT = 50000          # 官方硬限制
SAFE_LIMIT = 42000           # 自留 16% 余量：官方统计口径可能比我们宽（重试/登录也算）
MIN_INTERVAL = 0.2           # 每次请求之间至少 0.2s → 峰值 5 次/秒，全市场 5030 只约 17 分钟
MAX_CONCURRENCY = 1          # 官方明确禁止并发；这里写死 1，不接受任何覆盖
BLACKLIST_HOURS_PER_HIT = 6  # 官方：限制时长 = 本年累计次数 × 6 小时


class QuotaError(RuntimeError):
    """额度不足 / 处于黑名单，不允许继续发起请求。"""


def _today():
    return datetime.now().strftime("%Y-%m-%d")


def _blank():
    return {
        "date": _today(),
        "calls": 0,
        "logins": 0,
        "failures": 0,
        "blacklist_until": None,
        "blacklist_count_year": 0,
        "updated_at": None,
    }


def load():
    data = _blank()
    try:
        with open(QUOTA_PATH, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if isinstance(raw, dict):
            data.update(raw)
    except Exception:
        pass
    # 跨日自动清零（黑名单与年度次数不清）
    if data.get("date") != _today():
        data["date"] = _today()
        data["calls"] = 0
        data["logins"] = 0
        data["failures"] = 0
    return data


def save(data):
    data["updated_at"] = datetime.now().isoformat(timespec="seconds")
    try:
        os.makedirs(os.path.dirname(QUOTA_PATH), exist_ok=True)
        tmp = QUOTA_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, QUOTA_PATH)
    except Exception as e:
        print("  [警告] 额度文件写入失败：%s" % e, flush=True)


class Guard(object):
    def __init__(self, path=None):
        global QUOTA_PATH
        if path:
            QUOTA_PATH = path
        self.data = load()

    # ── 额度 ──
    def used(self):
        return int(self.data.get("calls") or 0)

    def remaining(self):
        return max(0, SAFE_LIMIT - self.used())

    def spend(self, n=1):
        self.data["calls"] = self.used() + int(n)
        save(self.data)

    def note_login(self):
        self.data["logins"] = int(self.data.get("logins") or 0) + 1
        save(self.data)

    def note_failure(self):
        self.data["failures"] = int(self.data.get("failures") or 0) + 1
        save(self.data)

    # ── 黑名单 ──
    def blacklist_until(self):
        raw = self.data.get("blacklist_until")
        if not raw:
            return None
        try:
            dt = datetime.fromisoformat(raw)
        except Exception:
            return None
        return dt if dt > datetime.now() else None

    def blacklist_remaining_hours(self):
        dt = self.blacklist_until()
        if not dt:
            return 0.0
        return round((dt - datetime.now()).total_seconds() / 3600.0, 2)

    def note_blacklist(self, error_msg=""):
        """记录一次封禁。

        关键：同一次封禁里反复探测（解封前多试几次）**不能**累加年度次数，
        否则「限制时长 = 本年累计次数 × 6 小时」会把自己越推越远
        （实测踩过：第二次探测把 6 小时推成 12 小时）。
        只有当前没有生效中的冻结时，才算新的一次。
        """
        cur = self.blacklist_until()
        if cur:
            # 仍在冻结期内：不叠加次数，只保证解封时间不早于 now+6h
            until = max(cur, datetime.now() + timedelta(hours=BLACKLIST_HOURS_PER_HIT))
            self.data["blacklist_until"] = until.isoformat(timespec="seconds")
            if error_msg:
                self.data["last_blacklist_msg"] = error_msg[:200]
            save(self.data)
            hours = round((until - datetime.now()).total_seconds() / 3600.0, 2)
            return until, hours

        n = int(self.data.get("blacklist_count_year") or 0) + 1
        hours = n * BLACKLIST_HOURS_PER_HIT
        until = datetime.now() + timedelta(hours=hours)
        self.data["blacklist_count_year"] = n
        self.data["blacklist_until"] = until.isoformat(timespec="seconds")
        self.data["last_blacklist_msg"] = (error_msg or "")[:200]
        save(self.data)
        return until, hours

    def clear_blacklist(self):
        """探测成功（真的能登录了）就清掉本地冻结记录，年度累计次数保留。"""
        if self.data.get("blacklist_until"):
            self.data["blacklist_until"] = None
            save(self.data)
            return True
        return False

    # ── 统一准入 ──
    def require_can_run(self, planned=0):
        dt = self.blacklist_until()
        if dt:
            raise QuotaError(
                "baostock 仍在黑名单期内，预计 %s 解封（还剩 %.1f 小时）。\n"
                "        期间请用腾讯源：python bulk_backfill.py --incremental --source tencent"
                % (dt.strftime("%Y-%m-%d %H:%M"), self.blacklist_remaining_hours())
            )
        rem = self.remaining()
        if rem <= 0:
            raise QuotaError(
                "今日 baostock 额度已用尽（%s 次，安全上限 %s），明天再跑或改用腾讯源。"
                % (f"{self.used():,}", f"{SAFE_LIMIT:,}")
            )
        if planned and planned > rem:
            raise QuotaError(
                "今日额度不足：计划 %d 次请求，剩余 %d 次（安全上限 %d，官方上限 %d）。\n"
                "        请改用腾讯源，或用 --limit %d 分批跑。"
                % (planned, rem, SAFE_LIMIT, DAILY_LIMIT, max(1, rem))
            )

    def status(self):
        dt = self.blacklist_until()
        return {
            "date": self.data.get("date"),
            "used": self.used(),
            "daily_limit": DAILY_LIMIT,
            "safe_limit": SAFE_LIMIT,
            "remaining": self.remaining(),
            "logins": int(self.data.get("logins") or 0),
            "failures": int(self.data.get("failures") or 0),
            "blacklist_until": dt.isoformat(timespec="seconds") if dt else None,
            "blacklist_remaining_hours": self.blacklist_remaining_hours(),
            "blacklist_count_year": int(self.data.get("blacklist_count_year") or 0),
            "min_interval_sec": MIN_INTERVAL,
            "max_concurrency": MAX_CONCURRENCY,
            "updated_at": self.data.get("updated_at"),
        }


def main(argv):
    g = Guard()
    if len(argv) > 1 and argv[1] == "reset":
        d = load()
        d["calls"] = 0
        d["logins"] = 0
        d["failures"] = 0
        d["date"] = _today()
        save(d)
        print("已清零今日 baostock 请求计数。")
        return 0
    st = g.status()
    print("baostock 访问额度 —— %s" % st["date"])
    print("  今日已用      : %s 次（安全上限 %s / 官方上限 %s）"
          % (f'{st["used"]:,}', f'{st["safe_limit"]:,}', f'{st["daily_limit"]:,}'))
    print("  今日剩余      : %s 次" % f'{st["remaining"]:,}')
    print("  登录次数      : %d" % st["logins"])
    print("  请求节流      : 最短间隔 %.2fs（峰值 %.0f 次/秒）" % (MIN_INTERVAL, 1.0 / MIN_INTERVAL))
    print("  并发上限      : %d（官方禁止并发）" % MAX_CONCURRENCY)
    if st["blacklist_until"]:
        print("  ⚠ 黑名单至    : %s（还剩 %.1f 小时，本年累计 %d 次）"
              % (st["blacklist_until"], st["blacklist_remaining_hours"], st["blacklist_count_year"]))
    else:
        print("  黑名单        : 无（本年累计 %d 次）" % st["blacklist_count_year"])
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
