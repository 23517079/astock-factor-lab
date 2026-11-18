#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_contract_sync.py — 结算之后回写产物契约字段（T10）

缺陷本体
--------
pipeline 顺序是 step7 打分 → step8 导出 → step9 结算。两个产物在 step7/step8
就落盘了，那时本轮还没结算 → `freshness.compute()` 拿到的 `lastCalcAt` 是
**上一轮**的（第一次跑甚至是 `None`）→ 页面显示「上次计算 16:22」，
内容却是 20:55 那一轮的。

用例（每条都要有牙齿）
----------------------
  [1] 缺陷复现 + 修复：`generate()` 写出的产物 lastCalcAt 是 None/旧值；
      结算后回写 → lastCalcAt == factor_run.json 的 finishedAt
  [2] 回写后 `freshness.status == fresh` 且 `dataThrough == 面板末端`
      （不许再出现「已过期 2 个交易日」这种与数据截止日矛盾的说法）
  [3] 失败运行也要回写：status=failed / lastCalcOk=false / reason 带错误
      （否则页面顶着上一轮成功的绿徽标，而流水线其实已经断了）
  [4] 只碰契约字段：名单、数值、顶层 partial/warnings 一律不动
  [5] 产物缺失/损坏时不炸（回填失败不能反过来把结算结果搞崩）

所有测试只用合成数据 + 临时目录；`prod_guard` 断言生产 data/ 一个字节未写。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, Optional

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for path in (HERE, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import contract_sync as CS                       # noqa: E402
import factor_eval as fe                        # noqa: E402
import factor_run as FR                         # noqa: E402
import factor_score as FS                       # noqa: E402
import factor_ts as TS                          # noqa: E402
from synth_env import PARAMS, SynthEnv          # noqa: E402
import prod_guard                               # noqa: E402

PASS = 0
FAIL = 0
FAILS = []

_ORIG_PRICE: Dict[str, Any] = {}


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  — %s" % detail) if detail else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  ✗ %s%s" % (name, ("  — %s" % detail) if detail else ""))


def _patch_loaders(env: SynthEnv) -> None:
    """把取价格/面板的函数换成合成数据（与 T06 用例同一套做法）。"""
    if not _ORIG_PRICE:
        _ORIG_PRICE["panel"] = fe.load_panel_wide
        _ORIG_PRICE["price"] = fe.load_price_wide
    env.activate()
    fe.load_panel_wide = (lambda conn, name, start=None, end=None, symbols=None:
                          env.panel(name))
    fe.load_price_wide = (lambda conn, column, symbols=None:
                          (env.close if str(column) == "close" else
                           env.volume if str(column) == "volume" else env.open))


def _restore_loaders() -> None:
    if not _ORIG_PRICE:
        return
    fe.load_panel_wide = _ORIG_PRICE["panel"]
    fe.load_price_wide = _ORIG_PRICE["price"]


def _read(path: str) -> Dict[str, Any]:
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ────────────────────────────────────────────────────────────
# [1][2] 缺陷复现 + 结算后回写
# ────────────────────────────────────────────────────────────
def case_backfill_after_finish(tmp: str) -> None:
    print("\n[1][2] 结算后回写：lastCalcAt == finishedAt、status=fresh、dataThrough=面板末端")
    env = SynthEnv(tmp, "cs1", n_dates=140)
    run_path = os.path.join(tmp, "factor_run.json")
    old_run = os.environ.get("FACTOR_RUN_PATH")
    os.environ["FACTOR_RUN_PATH"] = run_path
    score_out = os.path.join(tmp, "factor_score.json")
    try:
        _patch_loaders(env)
        TS.ensure(env.conn, env.fwd(axis=list(env.dates)), PARAMS,
                  panel_dates_axis=list(env.dates), strict=False, verbose=False)
        env.write_eval()

        # ── 缺陷复现：本轮还没结算，产物里的 lastCalcAt 必然拿不到本轮时间 ──
        FS.generate(env.conn, eval_path=env.eval_path, out=score_out, verbose=False,
                    run_robustness=False)
        pre = _read(score_out)
        ok("缺陷复现：结算前产物 lastCalcAt 拿不到本轮时间（None 或上一轮的值）",
           pre.get("lastCalcAt") in (None, ""), repr(pre.get("lastCalcAt")))
        ok("缺陷复现：结算前 freshness 段也是空的/不完整",
           not (pre.get("freshness") or {}).get("lastCalcAt"),
           repr((pre.get("freshness") or {}).get("lastCalcAt")))

        # ── 本轮结算 ──
        FR.begin("20261116T000000", trigger="manual")
        FR.finish(True, cost=1.0)
        finished = (FR.read() or {}).get("finishedAt")
        ok("factor_run.json 已结算（finishedAt 有值）", bool(finished), str(finished))

        res = CS.backfill(env.conn, [score_out], verbose=False)
        ok("回填成功", bool(res.get("ok")), str(res.get("reason"))[:80])
        post = _read(score_out)
        ok("产物 lastCalcAt == factor_run.json 的 finishedAt（不再滞后一轮）",
           post.get("lastCalcAt") == finished,
           "%s vs %s" % (post.get("lastCalcAt"), finished))
        ok("产物 lastCalcOk == true", post.get("lastCalcOk") is True,
           repr(post.get("lastCalcOk")))
        fresh = post.get("freshness") or {}
        ok("freshness.lastCalcAt 同步为 finishedAt",
           fresh.get("lastCalcAt") == finished, repr(fresh.get("lastCalcAt")))
        ok("freshness.status == fresh（不再是「尚未运行过」/「已过期 N 个交易日」）",
           fresh.get("status") == "fresh",
           "%s / %s" % (fresh.get("status"), str(fresh.get("reason"))[:40]))
        panel_end = str(env.dates_all[-1])
        ok("freshness.dataThrough == 面板末端",
           fresh.get("dataThrough") == panel_end
           and post.get("dataThrough") == panel_end,
           "%s vs %s" % (fresh.get("dataThrough"), panel_end))
        ok("lagTradingDays == 0（数据已追平，不该说「已过期」）",
           int(fresh.get("lagTradingDays") or 0) == 0,
           str(fresh.get("lagTradingDays")))
    finally:
        _restore_loaders()
        env.cleanup()
        if old_run is None:
            os.environ.pop("FACTOR_RUN_PATH", None)
        else:
            os.environ["FACTOR_RUN_PATH"] = old_run


# ────────────────────────────────────────────────────────────
# [3] 失败运行也要回写
# ────────────────────────────────────────────────────────────
def case_failure_is_visible(tmp: str) -> None:
    print("\n[3] 失败运行：status=failed / lastCalcOk=false / reason 带错误")
    env = SynthEnv(tmp, "cs3", n_dates=60)
    run_path = os.path.join(tmp, "factor_run.json")
    old_run = os.environ.get("FACTOR_RUN_PATH")
    os.environ["FACTOR_RUN_PATH"] = run_path
    score_out = os.path.join(tmp, "factor_score.json")
    try:
        _patch_loaders(env)
        TS.ensure(env.conn, env.fwd(axis=list(env.dates)), PARAMS,
                  panel_dates_axis=list(env.dates), strict=False, verbose=False)
        env.write_eval()
        FS.generate(env.conn, eval_path=env.eval_path, out=score_out, verbose=False,
                    run_robustness=False)

        FR.begin("20261116T000001", trigger="manual")
        FR.finish(False, error="模拟失败：面板读取炸了", cost=1.0)
        finished = (FR.read() or {}).get("finishedAt")

        res = CS.backfill(env.conn, [score_out], verbose=False)
        ok("回填成功", bool(res.get("ok")), str(res.get("reason"))[:80])
        post = _read(score_out)
        fresh = post.get("freshness") or {}
        ok("lastCalcOk == false（页面据此亮红字，而不是继续绿着）",
           post.get("lastCalcOk") is False, repr(post.get("lastCalcOk")))
        ok("lastCalcError 带失败原因",
           "模拟失败" in str(post.get("lastCalcError") or ""),
           str(post.get("lastCalcError"))[:60])
        ok("freshness.status == failed", fresh.get("status") == "failed",
           str(fresh.get("status")))
        ok("freshness.reason 带失败原因（用户能看懂为什么红）",
           "模拟失败" in str(fresh.get("reason") or ""),
           str(fresh.get("reason"))[:60])
        ok("lastCalcAt 仍然是本轮结算时刻",
           post.get("lastCalcAt") == finished,
           "%s vs %s" % (post.get("lastCalcAt"), finished))
    finally:
        _restore_loaders()
        env.cleanup()
        if old_run is None:
            os.environ.pop("FACTOR_RUN_PATH", None)
        else:
            os.environ["FACTOR_RUN_PATH"] = old_run


# ────────────────────────────────────────────────────────────
# [4] 只碰契约字段
# ────────────────────────────────────────────────────────────
def case_only_contract_fields(tmp: str) -> None:
    print("\n[4] 回填只改契约字段：名单/数值/顶层 partial/warnings 一律不动")
    run_path = os.path.join(tmp, "factor_run.json")
    old_run = os.environ.get("FACTOR_RUN_PATH")
    os.environ["FACTOR_RUN_PATH"] = run_path
    api_out = os.path.join(tmp, "factor_api.json")
    try:
        payload = {
            "ok": True,
            "generatedAt": "2026-11-16 20:51",
            "dataThrough": "2026-11-11",
            "lastCalcAt": None,
            "lastCalcOk": None,
            "lastCalcError": None,
            "nextRunAt": "2026-11-17 16:10",
            "partial": True,
            "warnings": ["证据门槛 fail：扣费后 weak（名单照常产出）"],
            "freshness": {"status": "fresh", "lastCalcAt": None},
            "rolling": {"n_periods": 7},
            "factors": [{"name": "mom20", "ic": 0.031}],
            "picks": [{"symbol": "S0001", "score": 1.23}],
        }
        with open(api_out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False)

        FR.begin("20261116T000002", trigger="manual")
        FR.finish(True, cost=1.0)

        res = CS.backfill(None, [api_out], verbose=False)
        ok("conn=None 时不炸（返回 ok=False 而不是抛异常）",
           res.get("ok") is False, str(res.get("reason"))[:60])

        # 用一个可用的连接重跑：借生产库只读不写 —— 这里改用合成库
        env = SynthEnv(tmp, "cs4", n_dates=60)
        try:
            _patch_loaders(env)
            TS.ensure(env.conn, env.fwd(axis=list(env.dates)), PARAMS,
                      panel_dates_axis=list(env.dates), strict=False, verbose=False)
            res2 = CS.backfill(env.conn, [api_out], verbose=False)
            ok("回填成功", bool(res2.get("ok")), str(res2.get("reason"))[:80])
            post = _read(api_out)
            ok("generatedAt 未被改（不是本轮重新生成的产物）",
               post.get("generatedAt") == "2026-11-16 20:51",
               str(post.get("generatedAt")))
            ok("picks / factors 未被改",
               post.get("picks") == payload.get("picks")
               and post.get("factors") == payload.get("factors"))
            ok("rolling 未被改", post.get("rolling") == payload.get("rolling"))
            ok("顶层 partial 未被改（打分级 partial ≠ 运行级 partial）",
               post.get("partial") is True, repr(post.get("partial")))
            ok("顶层 warnings 未被改（证据门槛的提示不能被覆盖掉）",
               post.get("warnings") == payload.get("warnings"),
               str(post.get("warnings"))[:60])
            ok("契约字段确实被更新了（lastCalcAt 从 None 变成结算时刻）",
               bool(post.get("lastCalcAt")), repr(post.get("lastCalcAt")))
            ok("freshness 段被整体刷新（status/lastCalcAt 同步）",
               (post.get("freshness") or {}).get("lastCalcAt") == post.get("lastCalcAt")
               and (post.get("freshness") or {}).get("status") in
               ("fresh", "pending", "stale", "failed"),
               str((post.get("freshness") or {}).get("status")))
        finally:
            env.cleanup()
    finally:
        if old_run is None:
            os.environ.pop("FACTOR_RUN_PATH", None)
        else:
            os.environ["FACTOR_RUN_PATH"] = old_run


# ────────────────────────────────────────────────────────────
# [5] 产物缺失/损坏时不炸
# ────────────────────────────────────────────────────────────
def case_missing_and_broken(tmp: str) -> None:
    print("\n[5] 产物缺失/损坏：回填不抛异常，且如实报告")
    env = SynthEnv(tmp, "cs5", n_dates=60)
    run_path = os.path.join(tmp, "factor_run.json")
    old_run = os.environ.get("FACTOR_RUN_PATH")
    os.environ["FACTOR_RUN_PATH"] = run_path
    broken = os.path.join(tmp, "broken.json")
    with open(broken, "w", encoding="utf-8") as fh:
        fh.write("{ this is not json")
    try:
        _patch_loaders(env)
        TS.ensure(env.conn, env.fwd(axis=list(env.dates)), PARAMS,
                  panel_dates_axis=list(env.dates), strict=False, verbose=False)
        FR.begin("20261116T000003", trigger="manual")
        FR.finish(True, cost=1.0)
        res = CS.backfill(env.conn, [os.path.join(tmp, "nope.json"), broken],
                          verbose=False)
        ok("缺文件/坏文件都不抛异常", bool(res.get("ok")), str(res.get("reason"))[:60])
        ok("如实列出缺失的产物", len(res.get("missing") or []) == 2,
           str(res.get("missing")))
        ok("坏文件没被改写（不把垃圾变成另一种垃圾）",
           open(broken, encoding="utf-8").read() == "{ this is not json")
    finally:
        _restore_loaders()
        env.cleanup()
        if old_run is None:
            os.environ.pop("FACTOR_RUN_PATH", None)
        else:
            os.environ["FACTOR_RUN_PATH"] = old_run


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="contract_sync_test_")
    t0 = time.time()
    print("=" * 72)
    print("T10 / 结算后回写产物契约字段（修「时间源滞后一轮」）")
    print("=" * 72)
    before = prod_guard.snapshot()
    try:
        case_backfill_after_finish(tmp)
        case_failure_is_visible(tmp)
        case_only_contract_fields(tmp)
        case_missing_and_broken(tmp)
    finally:
        _restore_loaders()
        shutil.rmtree(tmp, ignore_errors=True)

    ok("生产 data/ 的产物一个字节都没被写（隔离护栏）",
       not prod_guard.diff(before), str(prod_guard.diff(before)[:6]))
    print("\n========================================")
    print("RESULT: pass=%d fail=%d  （%.1fs）" % (PASS, FAIL, time.time() - t0))
    if FAILS:
        print("FAILED:", ", ".join(FAILS))
    print("========================================")
    return 0 if FAIL == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
