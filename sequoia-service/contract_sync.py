#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""contract_sync.py — 结算之后把「本轮契约字段」回写进已经写好的产物

缺陷背景（2026-11-16 真机端到端验证抓出来的）
-----------------------------------------------
pipeline 的步骤顺序是 **step7 打分 → step8 导出 → step9 结算**（`FR.finish`）。
两个产物（`data/factor_score.json` / `data/factor_api.json`）在 step7/step8
就落盘了，而那一刻本轮**还没结算**：`factor_run.json` 的顶层仍是上一轮的状态，
于是 `freshness.compute()` → `FR.last_calc()` 拿到的 `at` 是**上一轮**的
`finishedAt`（第一次跑时甚至是 `None`）。

结果：产物里的 `lastCalcAt` **永远滞后一轮** ——
页面显示「上次计算 11-16 16:22」，而内容其实是 11-16 20:55 那一轮的；
第一次跑完甚至整段 `freshness` 是 `None`，页面只能说「尚未运行过」。
这与说明书「`lastCalcAt = finishedAt`」的定义直接矛盾，
也正是说明书 §「lastCalcAt=null 却声称 fresh」那条教训指向的坑。

修法（最小改动）
----------------
结算之后**只回写契约字段**：

    lastCalcAt / lastCalcOk / lastCalcError / dataThrough / nextRunAt / freshness

不重跑打分、不重跑导出（导出会重算因子评估，白跑几秒还可能引入新的不一致），
也**不碰任何数值字段**（名单、因子、IC、权重一律不动）。
写盘仍然是 `.tmp → os.replace`，页面任何时刻都读不到半份 JSON。

为什么失败也要回写
------------------
失败时**数据产物没变**（不覆盖任何产物），但「上一轮跑失败了」这件事必须让
页面看得见 —— 否则页面顶着上一轮成功的绿徽标，而流水线其实已经断了
（共享知识第 3/8 条：失败不可静默）。回写后 `status=failed`、`reason=error`，
页面自动亮红字，这是**唯一**能改变徽标的几处字段，所以回写只碰它们。

两个必须知道的坑
----------------
1. **必须调 `freshness.compute()`，不能调 `freshness.cached()`**：
   `cached()` 有 60 秒 TTL，而 step7/step8 早就把「结算前」的判定缓存过一次，
   拿到的是旧值 —— 正是这个 bug 的本体。
2. **顶层 `partial` / `warnings` 不许动**：
   `factor_score.json` 的顶层 `partial` 是**打分级**的（证据门槛没过 → partial），
   `freshness.partial` 才是**运行级**的（滚动无有效期 → partial）。
   两者含义不同，覆盖掉会丢掉「证据不足」这个提示。
"""

from __future__ import annotations

import json
import os
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence

import freshness

# 契约字段：这些字段的**唯一来源**是 `freshness.compute()`，别处不许另算。
CONTRACT_KEYS = ("dataThrough", "lastCalcAt", "lastCalcOk", "lastCalcError",
                 "nextRunAt")


def _read_json(path: str) -> Optional[Dict[str, Any]]:
    """读 JSON 对象；文件不存在/损坏返回 None（**不抛**，回填不该阻断流水线）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def _write_json_atomic(path: str, payload: Dict[str, Any]) -> None:
    """`.tmp → os.replace`（共享知识第 7 条：页面读不到半份 JSON）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def _same_contract(a: Any, b: Any) -> bool:
    """比较两段契约内容，忽略 `evaluatedAt`（它是「判定时刻」，每秒都在变）。

    忽略它是为了不为了一个时间戳就把 800KB 的产物重写一遍；
    真正重要的字段（lastCalcAt / status / reason …）一个都不放过。
    """
    if not isinstance(a, dict) or not isinstance(b, dict):
        return a == b
    ka = {k: v for k, v in a.items() if k != "evaluatedAt"}
    kb = {k: v for k, v in b.items() if k != "evaluatedAt"}
    return ka == kb


def backfill(conn: Any, paths: Sequence[str],
             factor_time: str = freshness.DEFAULT_FACTOR_TIME,
             verbose: bool = False) -> Dict[str, Any]:
    """把**本轮结算结果**回写进产物的契约字段。

    Args:
        conn: 数据库连接（用于 `freshness.compute()`）；None 时直接放弃。
        paths: 要回写的产物路径（如 `FS.OUT` / `EFA.OUT`）。
            路径要在**调用时**取 —— 测试会把模块常量改到临时目录。
        factor_time: 因子任务时点（决定 `nextRunAt`），与 pipeline 配置一致。

    Returns:
        `{"ok", "reason", "freshness", "patched", "unchanged", "missing"}`
        `ok=False` 只表示回填没做成（读不到库/文件损坏），**不影响本轮结算结果**。
    """
    if conn is None:
        return {"ok": False, "reason": "没有数据库连接，无法重算新鲜度",
                "freshness": {}, "patched": [], "unchanged": [], "missing": []}
    try:
        # ⚠ 必须是 compute()，不能是 cached()（见模块 docstring 的坑 1）。
        fresh = freshness.compute(conn, factor_time=factor_time)
    except Exception as exc:                       # noqa: BLE001 — 回填不该阻断结算
        return {"ok": False,
                "reason": "新鲜度重算失败：%s: %s" % (type(exc).__name__, str(exc)[:100]),
                "freshness": {}, "patched": [], "unchanged": [], "missing": []}

    patched: List[str] = []
    unchanged: List[str] = []
    missing: List[str] = []
    for path in paths:
        if not path:
            continue
        payload = _read_json(path)
        if payload is None:
            missing.append(path)
            continue
        changed = False
        for key in CONTRACT_KEYS:
            if key in fresh and payload.get(key) != fresh.get(key):
                payload[key] = fresh[key]
                changed = True
        if not _same_contract(payload.get("freshness"), fresh):
            payload["freshness"] = fresh
            changed = True
        if changed:
            try:
                _write_json_atomic(path, payload)
                patched.append(path)
            except OSError as exc:
                return {"ok": False,
                        "reason": "回写失败（%s）：%s" % (path, str(exc)[:80]),
                        "freshness": fresh, "patched": patched,
                        "unchanged": unchanged, "missing": missing}
        else:
            unchanged.append(path)
    if verbose:
        print("[contract] 契约字段回写：%d 个产物更新、%d 个无变化%s"
              % (len(patched), len(unchanged),
                 ("、%d 个不存在" % len(missing)) if missing else ""), flush=True)
    return {"ok": True, "reason": None, "freshness": fresh,
            "patched": patched, "unchanged": unchanged, "missing": missing}


def main(argv: Optional[Any] = None) -> int:
    """手动回填一次（排障用）：`python contract_sync.py`"""
    import argparse
    import sqlite3

    import factor_score as FS
    import export_factor_api as EFA

    ap = argparse.ArgumentParser(description="把最近一次结算结果回写进产物契约字段")
    ap.add_argument("--db", default=os.path.join(os.path.dirname(
        os.path.abspath(__file__)), "data", "sequoia_v2.db"))
    ap.add_argument("--factor-time", default=freshness.DEFAULT_FACTOR_TIME)
    args = ap.parse_args(argv)

    conn = sqlite3.connect(args.db, timeout=120)
    try:
        res = backfill(conn, [FS.OUT, EFA.OUT], factor_time=args.factor_time,
                       verbose=True)
    finally:
        conn.close()
    print(json.dumps({k: res.get(k) for k in ("ok", "reason", "patched", "unchanged",
                                              "missing")},
                     ensure_ascii=False, indent=2))
    return 0 if res.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
