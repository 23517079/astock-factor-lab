#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
test_bs_guard.py — baostock 额度闸门与「防封禁」逻辑的确定性测试

为什么不用真机组测：
    当前账号仍在 6 小时封禁期内，任何真实登录都是在拿你的账号继续冒险。
    所以用一个假的 baostock 模块（sys.modules 注入）跑通全部分支：
      · 正常登录 → 只登录一次，不重复登；
      · 登录返回 10001011 → 立刻短路、记账、写解封时间；
      · 查询返回 10001011 → 同样中止整轮；
      · 额度用尽 / 黑名单期内 → 拒绝运行，不发任何请求。

运行：
    python test_bs_guard.py
"""
import json
import os
import subprocess
import sys
import tempfile
import types

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

PASS = 0
FAIL = 0
FAILS = []


def ok(name, cond, extra=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("PASS", name, extra)
    else:
        FAIL += 1
        FAILS.append(name)
        print("FAIL", name, extra)


# ── 假 baostock ──
class FakeRS(object):
    def __init__(self, rows, error_code="0", error_msg=""):
        self._rows = list(rows)
        self._i = -1
        self.error_code = error_code
        self.error_msg = error_msg

    def next(self):
        self._i += 1
        return self._i < len(self._rows)

    def get_row_data(self):
        return self._rows[self._i]


def make_fake_baostock(login_behavior, query_behavior):
    """login_behavior: list of ("ok"|"blacklist"|"error", msg)
       query_behavior: list of rows 或 ("blacklist",)"""
    state = {"logins": 0, "queries": 0}

    mod = types.ModuleType("baostock")

    class _LG(object):
        def __init__(self, code, msg):
            self.error_code = code
            self.error_msg = msg

    def login():
        state["logins"] += 1
        idx = min(state["logins"] - 1, len(login_behavior) - 1)
        kind = login_behavior[idx]
        if kind[0] == "ok":
            return _LG("0", "success")
        if kind[0] == "blacklist":
            return _LG("10001011", "黑名单用户，请与管理员联系")
        return _LG(kind[0], kind[1] if len(kind) > 1 else "err")

    def logout():
        return _LG("0", "success")

    def query_stock_basic(code_name="", code=""):
        return FakeRS([["sh.600000", "浦发银行", "", "", "1", "1"]])

    def query_history_k_data_plus(*a, **kw):
        state["queries"] += 1
        if isinstance(query_behavior, tuple) and query_behavior[0] == "blacklist":
            return FakeRS([], error_code="10001011", error_msg="黑名单用户")
        return FakeRS(query_behavior)

    mod.login = login
    mod.logout = logout
    mod.query_stock_basic = query_stock_basic
    mod.query_history_k_data_plus = query_history_k_data_plus
    mod._state = state
    return mod


def fresh_quota(**kw):
    fd, path = tempfile.mkstemp(suffix=".json")
    os.close(fd)
    os.remove(path)
    os.environ["BS_QUOTA_PATH"] = path
    if kw:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(kw, f)
    import bs_guard
    bs_guard.QUOTA_PATH = path
    return path


# ────────────────────────────────────────────────────────────
print("=== 1. 额度记账 ===")
fresh_quota()
import bs_guard
g = bs_guard.Guard()
ok("初始剩余=安全上限", g.remaining() == bs_guard.SAFE_LIMIT, "剩余 %d" % g.remaining())
ok("安全上限低于官方上限", bs_guard.SAFE_LIMIT < bs_guard.DAILY_LIMIT,
   "%d < %d" % (bs_guard.SAFE_LIMIT, bs_guard.DAILY_LIMIT))
g.spend(120)
ok("记账后剩余减少", g.remaining() == bs_guard.SAFE_LIMIT - 120, "剩余 %d" % g.remaining())
g.require_can_run(100)
ok("额度内放行", True)
try:
    g.require_can_run(bs_guard.SAFE_LIMIT)
    ok("超额拒绝", False, "未抛异常")
except bs_guard.QuotaError:
    ok("超额拒绝", True)
ok("跨境日期不计入", g.data["date"] == bs_guard._today())

print("\n=== 2. 黑名单记账（时长=次数×6h）===")
fresh_quota()
import importlib
importlib.reload(bs_guard)
bs_guard.QUOTA_PATH = os.environ["BS_QUOTA_PATH"]
g = bs_guard.Guard()
until1, h1 = g.note_blacklist("10001011 黑名单用户")
ok("首次封禁 6 小时", h1 == 6, "h=%d until=%s" % (h1, until1.isoformat(timespec="seconds")))
try:
    g.require_can_run(0)
    ok("黑名单期拒绝运行", False, "未抛异常")
except bs_guard.QuotaError as e:
    ok("黑名单期拒绝运行", "黑名单" in str(e))
until2, h2 = g.note_blacklist("again")
ok("同一次封禁重复探测不叠加次数(仍是6h)", h2 <= 6.01 and int(g.data.get("blacklist_count_year")) == 1,
   "h=%.2f count=%s" % (h2, g.data.get("blacklist_count_year")))
ok("探活成功后清冻结记录", g.clear_blacklist() is True and g.blacklist_until() is None)
ok("清冻结不清年度次数", int(g.data.get("blacklist_count_year")) == 1,
   "count=%s" % g.data.get("blacklist_count_year"))
until3, h3 = g.note_blacklist("解封后又被封")
ok("解禁后再被封才累加为12小时", h3 == 12, "h=%d" % h3)

print("\n=== 3. 登录只做一次 ===")
fresh_quota()
importlib.reload(bs_guard)
bs_guard.QUOTA_PATH = os.environ["BS_QUOTA_PATH"]
sys.modules["baostock"] = make_fake_baostock([("ok",)], [["2026-11-13", "1", "2", "0.5", "1.5", "1000", "1500", "0.1"]])
if "bulk_backfill" in sys.modules:
    del sys.modules["bulk_backfill"]
import bulk_backfill as B
B._BS_STATE.clear()
B._BS_STATE.update({"ready": False, "blacklist": False, "blacklist_msg": ""})
ok("首次登录成功", B._bs_login_once() is True)
ok("二次调用不再登录", B._bs_login_once() is True and sys.modules["baostock"]._state["logins"] == 1,
   "logins=%d" % sys.modules["baostock"]._state["logins"])

print("\n=== 4. 登录即黑名单 → 短路 ===")
fresh_quota()
importlib.reload(bs_guard)
bs_guard.QUOTA_PATH = os.environ["BS_QUOTA_PATH"]
sys.modules["baostock"] = make_fake_baostock([("blacklist",)], [])
del sys.modules["bulk_backfill"]
import bulk_backfill as B2
B2._BS_STATE.clear()
B2._BS_STATE.update({"ready": False, "blacklist": False, "blacklist_msg": ""})
ok("登录返回黑名单→False", B2._bs_login_once() is False)
ok("黑名单标志置位", B2._BS_STATE["blacklist"] is True)
n_login = sys.modules["baostock"]._state["logins"]
ok("不刷登录（最多 2 次）", n_login <= 2, "logins=%d" % n_login)

print("\n=== 5. 查询期间被拉黑 → 整轮中止并记账 ===")
fresh_quota()
importlib.reload(bs_guard)
bs_guard.QUOTA_PATH = os.environ["BS_QUOTA_PATH"]
sys.modules["baostock"] = make_fake_baostock([("ok",)], ("blacklist",))
del sys.modules["bulk_backfill"]
import bulk_backfill as B3
B3._BS_STATE.clear()
B3._BS_STATE.update({"ready": False, "blacklist": False, "blacklist_msg": ""})
tasks = [("600000", "sh.600000", "2026-11-01", "2026-11-13"),
         ("600004", "sh.600004", "2026-11-01", "2026-11-13"),
         ("600006", "sh.600006", "2026-11-01", "2026-11-13")]
out = list(B3.iter_baostock(tasks))
ok("生成器在首个结果即停止", len(out) == 1, "yielded=%d" % len(out))
ok("标记为 __BLACKLIST__", out and out[0][2] == "__BLACKLIST__", str(out[0][2] if out else None))
gg = bs_guard.Guard()
ok("已写解封时间", gg.blacklist_until() is not None,
   gg.data.get("blacklist_until"))
ok("只发了 1 次请求就停", sys.modules["baostock"]._state["queries"] == 1,
   "queries=%d" % sys.modules["baostock"]._state["queries"])

print("\n=== 6. 额度耗尽 → 主动停（不发请求）===")
fresh_quota(calls=bs_guard.SAFE_LIMIT, date=bs_guard._today())
importlib.reload(bs_guard)
bs_guard.QUOTA_PATH = os.environ["BS_QUOTA_PATH"]
sys.modules["baostock"] = make_fake_baostock([("ok",)], [["2026-11-13", "1", "2", "0.5", "1.5", "1000", "1500", "0.1"]])
del sys.modules["bulk_backfill"]
import bulk_backfill as B4
B4._BS_STATE.clear()
B4._BS_STATE.update({"ready": False, "blacklist": False, "blacklist_msg": ""})
out = list(B4.iter_baostock([("600000", "sh.600000", "2026-11-01", "2026-11-13")]))
ok("额度为 0 时不发请求", len(out) == 0 and sys.modules["baostock"]._state["queries"] == 0,
   "yielded=%d queries=%d" % (len(out), sys.modules["baostock"]._state["queries"]))

print("\n=== 7. 命令行闸门（真实进程，不联网）===")
fresh_quota()
importlib.reload(bs_guard)
bs_guard.QUOTA_PATH = os.environ["BS_QUOTA_PATH"]
# 写入一个未来 6 小时的黑名单
import datetime
until = (datetime.datetime.now() + datetime.timedelta(hours=6)).isoformat(timespec="seconds")
with open(os.environ["BS_QUOTA_PATH"], "w", encoding="utf-8") as f:
    json.dump({"date": bs_guard._today(), "calls": 0, "logins": 0, "failures": 0,
               "blacklist_until": until, "blacklist_count_year": 1}, f)
env = dict(os.environ)
env["BS_QUOTA_PATH"] = os.environ["BS_QUOTA_PATH"]
p = subprocess.run([sys.executable, os.path.join(HERE, "bulk_backfill.py"),
                    "--source", "baostock", "--workers", "8", "--limit", "5"],
                   capture_output=True, cwd=HERE, env=env, timeout=120)
txt = (p.stdout or b"").decode("utf-8", "replace")
err = (p.stderr or b"").decode("utf-8", "replace")
alltxt = txt + err
ok("强制串行并拒绝运行", p.returncode != 0 and "黑名单" in alltxt,
   "rc=%d" % p.returncode)
ok("workers 被降为 1", "强制降为 1" in alltxt)
ok("未触碰网络（无拉取日志）", "并发拉取" not in alltxt and "串行拉取" not in alltxt)

print("\n========================================")
print("RESULT: pass=%d fail=%d" % (PASS, FAIL))
if FAILS:
    print("FAILED:", ", ".join(FAILS))
print("========================================")
sys.exit(0 if FAIL == 0 else 1)
