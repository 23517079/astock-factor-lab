#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""run_factor_pipeline.py — 每日因子重算的编排入口

每日动作序列（R1 修订版，2026-11-16）
────────────────────────────────────
    plan → 增量算面板 → 写库 → 缓存 staging+verify+提交
         → **加载一次价格宽表** → ts 追加 → 滚动验证 → 打分 → 导出 → finish

打印成 9 个编号步骤：

    step 1  plan      探测缺口、剔除残日、裁到 MAX_DAYS（**前置断言 24 因子缓存齐备**）
    step 2  compute   增量计算 + INSERT OR REPLACE 写库（panel_incr）
    step 3  cache     缓存 staging → verify → 原子提交（周一追加一次全轴校验）
    step 4  price     价格宽表 close+open 与 fwd —— **全流程只加载一次**（惰性）
    step 5  ts        逐日 ic + 逐日 10 组收益追加（factor_ts）
    step 6  rolling   滚动验证（rolling_eval，纯切片不自算 IC）
    step 7  score     打分（factor_score）
    step 8  export    导出数据契约（export_factor_api，含 evalAsOf）
    step 9  finish    结算 factor_run.json

**为什么 step 4 单独拎出来**：实测 `close`+`open` 宽表合计 **54.7 秒**。
`step_ts`（算 `ic` 与 `groups`）需要 `fwd`；`step_rolling` 纯切片、**不需要**
价格。价格宽表由 pipeline 以 `PriceBundle` 注入，`step_ts` 第一次 `get()` 时
才真正读库（之前计划里两个空间都是空的、白付 55 秒的教训）。
`verify.price.loaded` 就是这根探针：T03 落地后它必须是 `true`、`cost ≈ 55s`；
若仍为 `false` 且目标日期非空，说明有人在别处偷偷又加载了一份价格宽表。

**周一全轴校验（Q6）**：`today.weekday() == 0` 时 `step_cache` 在提交后多跑一次
`panel_cache.verify(mode="full")`（约 +25s）。上次的严重事故是**轴中间错位**，
`tail` 只比对尾部，发现不了。

**明确不重跑 `factor_eval.py`**（小时级全量评估）。这是审计边界，不是遗漏：
页面上的单因子 IC 仍然来自上一次全量评估，因此必须同时展示 `evalAsOf`。

退出码（共享知识第 6 条）
────────────────────────
    0    成功 / 幂等空跑
    2    校验失败（缓存因子不齐、计划非法、缓存追加失败）
    3    运行锁被占用
    4    滚动验证没有有效期（T03 起生效）
    5    打分/导出异常（T03 起生效）
    124  超时

用法
----
    python run_factor_pipeline.py --trigger manual
    python run_factor_pipeline.py --trigger scheduler --workers 8
    python run_factor_pipeline.py --dry-run          # 只打印计划，零副作用
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
import time
from typing import Any, Dict, List, Optional, Sequence

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import factor_run as FR                        # noqa: E402
import factor_ts as TS                         # noqa: E402
import panel_cache as PC                       # noqa: E402
import panel_incr as PI                        # noqa: E402
import rolling_eval as RE                      # noqa: E402
import sched_time                              # noqa: E402
from factors import registry                   # noqa: E402

DB = os.path.join(HERE, "data", "sequoia_v2.db")

# 口径常量（共享知识第 5 条：全部走配置，并写进 JSON 供页面展示）
# R1：min_stocks 统一取 100，不复用存档的 30 口径（§3 决策 9）
DEFAULTS: Dict[str, Any] = {
    "horizon": 5,               # IC 的前瞻期（交易日）
    "entry": "s1_open",         # 建仓价口径：T+1 开盘
    "minStocks": 100,           # 逐日横截面最小有效样本
    "nwLag": 4,                 # Newey-West 滞后阶数
    "nGroups": 10,              # 分组收益的组数
    "rollStep": 20,             # 滚动验证的期步长（交易日）
    "rollSelectWindow": 120,    # 每期回看多少日 IC 来选因子
    "warmupBars": PI.WARMUP_BARS,
    "minSymbolsPerDay": PI.MIN_SYMBOLS_PER_DAY,
    "maxDays": PI.MAX_DAYS,
    "commitEvery": PI.COMMIT_EVERY,
    "factorTime": "16:10",      # 因子任务时点（T04 会与 scheduler.DEFAULT_CFG 对齐）
    "top": 10,                  # 推荐名单只数
    "weight": "equal",          # 合成分加权方式（等权最不容易过拟合）
}

# 步骤总数（含 finish），只用于日志的 [i/N] 计数
N_STEPS = 9


# ────────────────────────────────────────────────────────────
# step 4：价格宽表（惰性，全流程只加载一次）
# ────────────────────────────────────────────────────────────
class PriceBundle:
    """价格宽表 `close` / `open` 与未来收益 `fwd` 的**惰性单例**。

    `step_ts` 与 `step_rolling` 必须共用同一份 `fwd`：
      * `step_ts` 拿它算逐日 `ic` 与 `groups`；
      * `step_rolling` 拿它做逐期切片（`ic_cutoff` 上游）。
    两者各自加载会付两遍 54.7 秒，所以由 pipeline 注入本对象，步骤内部
    调 `bundle.get()`，第一次访问才读库，之后返回同一个 dict。

    Attributes:
        loaded: 是否已经真正读过库。
        cost: 加载耗时（秒）；未加载时为 0.0。
    """

    def __init__(self, conn: sqlite3.Connection, config: Dict[str, Any]) -> None:
        self._conn = conn
        self._config = config
        self._data: Optional[Dict[str, Any]] = None
        self.cost: float = 0.0

    @property
    def loaded(self) -> bool:
        """是否已加载（用于日志与 verify 记录）。"""
        return self._data is not None

    def get(self) -> Dict[str, Any]:
        """返回 `{close, open, fwd, horizon, entry, dates, symbols}`。

        同一个 bundle 实例上反复调用是免费的（返回同一个 dict 对象）。
        """
        if self._data is not None:
            return self._data
        t0 = time.time()
        import factor_eval as FE               # 延迟导入：这个模块很大且只在需要时用

        close = FE.load_close_wide(self._conn)
        open_wide = FE.load_open_wide(self._conn)
        horizon = int(self._config["horizon"])
        entry = str(self._config["entry"])
        fwd = FE.load_forward_returns(close, horizon, entry=entry,
                                      open_wide=open_wide)
        self._data = {
            "close": close,
            "open": open_wide,
            "fwd": fwd,
            "horizon": horizon,
            "entry": entry,
            "dates": [str(d) for d in close.index],
            "symbols": [str(s) for s in close.columns],
        }
        self.cost = round(time.time() - t0, 1)
        return self._data

    def summary(self) -> Dict[str, Any]:
        """写进 factor_run.json 的摘要（不把 DataFrame 写进去）。"""
        if self._data is None:
            return {"loaded": False, "cost": 0.0,
                    "reason": "本次没有任何步骤请求价格宽表"
                              "（幂等空跑，或 step_ts 提前失败）"}
        return {"loaded": True, "cost": self.cost,
                "horizon": self._data["horizon"], "entry": self._data["entry"],
                "dates": len(self._data["dates"]),
                "symbols": len(self._data["symbols"])}


def _is_week_start(today: Optional[str] = None) -> bool:
    """今天是否周一 —— 决定 step_cache 是否追加一次全轴校验（Q6 决策）。

    `today` 显式传入时按它判（排障/回放用），否则用系统时间。
    """
    if not today:
        return sched_time.is_week_start()
    try:
        from datetime import datetime
        return datetime.strptime(str(today)[:10], sched_time.DATE_FMT).weekday() == 0
    except (ValueError, TypeError):
        return False



# ────────────────────────────────────────────────────────────
# 各步骤
# ────────────────────────────────────────────────────────────
def step_plan(conn: sqlite3.Connection, today: Optional[str] = None,
              verbose: bool = True) -> Dict[str, Any]:
    """step 1：缓存齐备性断言 + 缺口探测。

    第一步就是 `coverage()` 断言 24 因子齐备（§3 决策 3）：冷启动只补了 7 个
    缓存时，宁可 `exit 2` 并给出明确提示，也不能让页面拿「部分因子的滚动验证
    结果」当「24 个因子的」用。

    Returns:
        {"ok", "reason", "plan": {...}, "coverage": {...}}
    """
    required = registry.all_names()
    cov = PC.coverage(conn, required)
    if verbose:
        print("[1/%d plan] 缓存齐备性：%d/%d%s" % (N_STEPS,
              len(cov["present"]), len(cov["required"]),
              "" if cov["ok"] else "；缺 %s" % (cov["missing"][:8])), flush=True)
    if not cov["ok"]:
        reasons: List[str] = []
        if cov["missing"]:
            reasons.append("缺 %d 个因子缓存（%s）"
                           % (len(cov["missing"]), ",".join(cov["missing"][:6])))
        if cov["badShape"]:
            reasons.append("形状不符 %d 个（%s）"
                           % (len(cov["badShape"]), ",".join(cov["badShape"][:6])))
        if not cov["axis"]["dates"]:
            reasons.append("缓存轴为空，从未建过缓存")
        return {"ok": False,
                "reason": "缓存因子不齐，必须先在非定时窗口手动补齐：%s；"
                          "请先执行 panel_cache.py --build-all（约 60–90 分钟）"
                          % "；".join(reasons),
                "plan": None, "coverage": cov}

    pl = PI.plan(conn, today=today, max_days=DEFAULTS["maxDays"],
                 min_symbols=DEFAULTS["minSymbolsPerDay"])
    if verbose:
        print("      面板已算到 %s，行情库到 %s，缺口 %d 日 → 目标 %s%s"
              % (pl.get("dataThrough"), pl.get("expectedThrough"), pl.get("lagDays"),
                 pl.get("targetDates") or "无",
                 "（残日剔除 %s）" % pl["residualDates"] if pl.get("residualDates") else ""),
              flush=True)
        if pl.get("skippedByMaxDays"):
            print("      缺口超 %d 日上限，本次只补最近 %d 日；剩余 %s 留给下次"
                  % (DEFAULTS["maxDays"], len(pl.get("targetDates") or []),
                     pl["skippedByMaxDays"][:5]), flush=True)
    if not pl.get("ok"):
        return {"ok": False, "reason": pl.get("reason"), "plan": pl, "coverage": cov}
    return {"ok": True, "reason": pl.get("reason"), "plan": pl, "coverage": cov}


def step_compute(conn: sqlite3.Connection, target_dates: Sequence[str],
                 workers: int = 8, verbose: bool = True) -> Dict[str, Any]:
    """step 2：增量计算 + 写库（INSERT OR REPLACE，天然幂等）。"""
    if not target_dates:
        if verbose:
            print("[2/%d compute] 无缺口，跳过（幂等空跑）" % N_STEPS, flush=True)
        return {"ok": True, "skipped": True, "plainRows": 0, "targetDates": [],
                "residualDates": [], "elapsed": 0.0}
    t0 = time.time()
    res = PI.run_incremental(conn=conn, target_dates=list(target_dates),
                             workers=workers, progress=verbose)
    if verbose:
        print("[2/%d compute] %d 只 × %d 因子 × %d 日 → 写入 %s 行，%.1fs%s"
              % (N_STEPS, res.get("symbolsDone", 0), len(res.get("factors") or []),
                 len(res.get("targetDates") or []), f"{res.get('plainRows', 0):,}",
                 time.time() - t0,
                 "；残日 %s" % res["residualDates"] if res.get("residualDates") else ""),
              flush=True)
    return res


def step_cache(conn: sqlite3.Connection, verbose: bool = True,
               full_verify: bool = False) -> Dict[str, Any]:
    """step 3：把面板尾巴追加进缓存（staging → verify → 原子提交）。

    与 step 2 解耦：本步补的是「面板里已有、缓存里还没有」的日期。
    所以即使 step 2 因为缺口为 0 什么都没算，只要缓存落后（比如上次崩在
    commit 之前），本步也会把缓存追上。

    staging 未通过 verify 时**只 abort，不提交**，正式缓存保持旧轴可用
    （§5.2：崩在提交前，旧缓存与旧 meta 必须完好可用）。

    Args:
        conn: 数据库连接。
        verbose: 打印进度。
        full_verify: 是否在本步之后追加一次**全轴**校验（`mode="full"`，约 25s）。
            周一跑（Q6 决策）：上次的严重事故是**轴中间错位**，`tail` 只比对尾部
            发现不了；每周一次全轴核对，25 秒/周，值得。

    Returns:
        {"ok", "skipped", "tail", "verify", "fullVerify", "commit", "elapsed"}
    """
    tail = PC.tail_delta(conn)
    if not tail.get("ok"):
        return {"ok": False, "reason": tail.get("reason"), "tail": tail,
                "fullVerify": None}
    result: Dict[str, Any] = {"ok": True, "skipped": True, "tail": tail,
                              "verify": None, "commit": None, "fullVerify": None}
    if not tail["newDates"] and not tail["newSymbols"]:
        if verbose:
            print("[3/%d cache] 缓存轴已是最新（%d 天 × %d 只），跳过"
                  % (N_STEPS, tail["axisDates"], tail["axisSymbols"]), flush=True)
    else:
        t0 = time.time()
        if verbose:
            print("[3/%d cache] 尾部增量：+%d 日 / +%d 只 → staging…"
                  % (N_STEPS, len(tail["newDates"]), len(tail["newSymbols"])), flush=True)
        try:
            st = PC.stage_append(conn, tail["newDates"])
        except Exception as exc:                         # noqa: BLE001 — 上游要拿到原因
            PC.abort_stage()
            return {"ok": False, "reason": "staging 失败：%s" % exc, "tail": tail,
                    "fullVerify": None}

        vf = PC.verify_stage(conn, mode="tail")
        if verbose:
            print("       verify_stage：抽 %d 格，最大相对误差 %.2e，不一致 %d → %s（%.1fs）"
                  % (vf.get("checked", 0), vf.get("maxRelErr", 0.0), vf.get("mismatch", 0),
                     "通过" if vf.get("ok") else "不通过", time.time() - t0), flush=True)
        if not vf.get("ok"):
            PC.abort_stage()
            return {"ok": False, "reason": "staging 校验未通过，已丢弃（正式缓存未改动）",
                    "tail": tail, "verify": vf, "stage": st, "fullVerify": None}

        cr = PC.commit_stage()
        if verbose:
            print("       提交 %d 个因子（meta.json 最后替换），%.1fs"
                  % (len(cr.get("committed") or []), cr.get("elapsed", 0.0)), flush=True)
        if not cr.get("ok"):
            return {"ok": False, "reason": "提交失败：%s" % cr.get("reason"),
                    "tail": tail, "verify": vf, "commit": cr, "fullVerify": None}
        result.update(skipped=False, verify=vf, commit=cr, stage=st,
                      elapsed=round(time.time() - t0, 2))

    if full_verify:
        # 全轴校验放在提交之后：核对的是「刚提交完的正式缓存」，
        # 而不是 staging —— 否则校验通过但提交出错的极端情况会被漏掉。
        t1 = time.time()
        fv = PC.verify(conn, mode="full")
        result["fullVerify"] = {
            "ok": bool(fv.get("ok")), "checked": fv.get("checked"),
            "maxRelErr": fv.get("maxRelErr"), "mismatch": fv.get("mismatch"),
            "axis": fv.get("axis"), "elapsed": round(time.time() - t1, 2),
        }
        if verbose:
            print("       全轴校验（每周一）：抽 %s 格，轴 %s，不一致 %d → %s（%.1fs）"
                  % (fv.get("checked"), "通过" if (fv.get("axis") or {}).get("ok") else "不通过",
                     int(fv.get("mismatch") or 0),
                     "通过" if fv.get("ok") else "不通过", time.time() - t1), flush=True)
        if not fv.get("ok"):
            return {"ok": False, "reason": "全轴校验未通过（轴中间可能错位）",
                    "tail": tail, "verify": result.get("verify"),
                    "fullVerify": result["fullVerify"]}
    return result


def step_ts(conn: sqlite3.Connection, bundle: "PriceBundle", config: Dict[str, Any],
            run_id: Optional[str] = None, verbose: bool = True) -> Dict[str, Any]:
    """step 5：因子时间序列追加（`factor_ts`：逐日 `ic` + 逐日 10 组收益）。

    职责（说明书 §5.5 / §5.6）：
      * 每因子**独立轴**（预热期不同，共轴必然引入 NaN 或被短序列稀释）；
      * 追加区间 `last < d <= panelMaxDate - horizon`（天然排除最后 horizon 天）；
      * 逐日同时落 `ic[d]`（未乘 direction 的原始 Spearman IC，`min_stocks=100`）
        与 `groups[d][1..10]`（按 direction 归正，组 1 = 预期最好）。
        **`groups` 缺失 = `verdict_of` 拿不到 `monotonicity` = 所有因子落到
        「仅统计显著」= 滚动验证选出 0 个因子且不报错**，这是最容易做错的一处；
      * 失效矩阵 6 条，第 6 条走 `.bak` 保护式重算，禁止直接截断。

    Args:
        conn: 数据库连接。
        bundle: 价格宽表（`bundle.get()` 取 `{close, open, fwd, ...}`）。
                **必须用它，不要在本步里自己 `load_close_wide()`** —— 会和
                step_rolling 各付一遍 54.7 秒。它是惰性的：本步第一次 `get()`
                才真正读库。
        config: 口径常量（`DEFAULTS` 的副本）。
        run_id: 本次运行标识（写进 `factor_ts/meta.json` 便于排障对照）。
        verbose: 打印进度。

    Returns:
        {"ok", "appended", "factors", "skipped", "failed", "suspects",
         "willPersist", "ensure", "elapsed"}
        `skipped` 只表示「因 `> panelMax-horizon` 算不出 IC 的日期」。
    """
    t0 = time.time()
    price = bundle.get()                        # 第一次访问：付那 54.7 秒
    params = {k: config[k] for k in ("horizon", "entry", "minStocks", "nGroups")}
    axis = TS.panel_dates_fast(conn)
    if verbose:
        print("[5/%d ts] 面板轴 %d 天（%s ~ %s），价格宽表 %.1fs…"
              % (N_STEPS, len(axis), axis[0] if axis else "-",
                 axis[-1] if axis else "-", bundle.cost), flush=True)
    try:
        ens = TS.ensure(conn, price["fwd"], params, panel_dates_axis=axis,
                        verbose=verbose)
        app = TS.append(conn, price["fwd"], panel_dates_axis=axis,
                        run_id=run_id, verbose=verbose)
    except TS.CacheIncomplete as exc:
        return {"ok": False, "reason": str(exc), "exit": FR.EXIT_INVALID,
                "appended": 0, "factors": 0, "skipped": [], "failed": [],
                "willPersist": list(TS.WILL_PERSIST),
                "elapsed": round(time.time() - t0, 2)}

    out = {"ok": bool(app.get("ok")),
           "reason": app.get("reason"),
           "appended": int(app.get("appended") or 0),
           "factors": int(app.get("factors") or 0),
           "skipped": list(app.get("skipped") or []),
           "failed": list(app.get("failed") or []),
           "suspects": list(ens.get("suspects") or []),
           "reconcile": ens.get("reconcile") or {},
           "ensure": {k: ens.get(k) for k in
                      ("action", "reasons", "rebuilt", "repaired", "protective",
                       "axis", "elapsed")},
           # 断言用：本步必须同时落 ic 与 groups（缺 groups = 滚动验证静默失效）
           "willPersist": list(TS.WILL_PERSIST),
           "params": params,
           "elapsed": round(time.time() - t0, 2)}
    if verbose:
        print("[5/%d ts] ensure=%s，追加 %d 个日期点（%d 因子），参数 %s，%.1fs"
              % (N_STEPS, ens.get("action"), out["appended"], out["factors"],
                 params, out["elapsed"]), flush=True)
    return out


def step_rolling(conn: sqlite3.Connection, bundle: "PriceBundle",
                 config: Dict[str, Any], verbose: bool = True) -> Dict[str, Any]:
    """step 6：滚动验证（`rolling_eval`：逐期重选因子）。

    职责（说明书 §6 / 约束 A）：
      * `ic_cutoff(dates, t_idx, horizon)` 是唯一入口，`h=5` 时
        `icCutoff == dates[idx(T) - 5]`；运行时断言 `ic_win.index.max() <= cut`；
      * 逐期重选（`step=20` / `select_window=120`）；
      * `verdict_of` / `monotonicity_of` 复用，不得新增第二处定义；
      * 数据一律从 `factor_ts` 的逐期切片取 —— **纯切片，不自算 IC 序列**，
        所以本步**不需要价格宽表**（那 54.7 秒由 step_ts 付过一次就够）。
        `bundle` 参数保留是为了显式表达「价格由上游统一供给」，
        `needsPrice` 为 False 时它不会被访问；
      * `ts.last` 滞后 `>= horizon` 都是正常的（`min_stocks=100` 会让个别日子无 IC），
        只有 `< horizon` 才是 bug —— 不要写 `== horizon` 的断言。

    返回 `no_period=True` 时调用方应以退出码 4 结束。
    """
    t0 = time.time()
    params = {k: config[k] for k in
              ("horizon", "entry", "minStocks", "nwLag", "nGroups",
               "rollStep", "rollSelectWindow")}
    axis = TS.panel_dates_fast(conn)
    try:
        res = RE.run(conn, params, panel_dates_axis=axis, out=RE.OUT_DEFAULT,
                     verbose=verbose)
    except RuntimeError as exc:                    # 数据层探针未通过
        return {"ok": False, "no_period": False, "exit": FR.EXIT_INVALID,
                "reason": str(exc), "n_periods": 0, "skippedFactors": [],
                "willUsePrice": False, "elapsed": round(time.time() - t0, 2)}

    no_period = int(res.get("code") or 0) == FR.EXIT_NO_PERIOD or not res.get("ok")
    out = {"ok": bool(res.get("ok")),
           "no_period": bool(no_period),
           "exit": FR.EXIT_NO_PERIOD if no_period else FR.EXIT_OK,
           "reason": res.get("reason"),
           "n_periods": int(res.get("n_periods") or 0),
           "skippedFactors": list(res.get("skippedFactors") or []),
           "latest": res.get("latest"),
           "summary": res.get("summary"),
           "lag": res.get("lag"),
           "willUsePrice": False,
           "params": params,
           "elapsed": round(time.time() - t0, 2)}
    if verbose and res.get("ok"):
        print("[6/%d rolling] %d 期，最新 %s（cut %s）入选 %d 个，跳过 %d 个 suspect，%.1fs"
              % (N_STEPS, out["n_periods"],
                 (out["latest"] or {}).get("asOf"), (out["latest"] or {}).get("icCutoff"),
                 (out["latest"] or {}).get("n_selected"),
                 len(out["skippedFactors"]), out["elapsed"]), flush=True)
    return out


def step_score(conn: sqlite3.Connection, rolling: Dict[str, Any],
               config: Dict[str, Any], partial: bool = False,
               warnings: Optional[List[str]] = None,
               verbose: bool = True) -> Dict[str, Any]:
    """step 7：打分（`factor_score.generate`，写 `data/factor_score.json`）。

    写盘是 `.tmp → os.replace`：宁可页面数据「旧」，也不能写出一份半成品让页面
    显示错的东西（共享知识第 8 条「失败不可静默」的另一面）。
    滚动验证无有效期（`no_period`）时**仍然打分**：打分用的是全历史评估结果，
    与滚动验证互不阻塞。此时整轮以 `ok=true, partial=true` 结算、退出码 0 ——
    滚动那一路的缺口由 `warnings` 记录，不用退出码 4（4 只留给「连打分都做不出来」，
    否则会出现「名单已经更新、页面却报更新失败」的自相矛盾）。
    """
    t0 = time.time()
    import factor_score as FS

    try:
        res = FS.generate(conn, eval_path=FS.EVAL, out=FS.OUT,
                          horizon=int(config["horizon"]),
                          entry=str(config["entry"]),
                          top=int(config.get("top", 10)),
                          weight=str(config.get("weight", "equal")),
                          recent_filter="inherit", partial=partial,
                          warnings=warnings, verbose=verbose,
                          # 逐期打分的期网格必须与 step_rolling 同一套口径，
                          # 否则 signAsOf 与「滚动验证」卡片是两套期、不可互证
                          mode=FS.MODE_WF,
                          roll_step=int(config["rollStep"]),
                          roll_window=int(config["rollSelectWindow"]),
                          # 成本/证据口径从配置取；DEFAULTS 未列这些键时走这里的保守默认
                          cost_profile=str(config.get("costProfile", "a_share")),
                          cost_multiplier=float(config.get("costMultiplier", 1.0)),
                          min_trades=int(config.get("minTrades", 50)),
                          gate_mode=str(config.get("gateMode", "warn")),
                          enforce_gate=bool(config.get("enforceGate", False)),
                          run_robustness=bool(config.get("runRobustness", True)))
    except SystemExit as exc:                      # 唯一一类 exit 4：连名单都做不出来
        return {"ok": False, "exit": FR.EXIT_NO_PERIOD,
                "reason": "打分失败（无任何可用因子，本轮无产物）：%s"
                          % (str(exc) or "无可用因子"),
                "elapsed": round(time.time() - t0, 2)}
    except (AssertionError, ValueError, FS.CostProfileError,
            FS.EvidenceGateError) as exc:
        # 这是一套反自欺检查能否被机器拦住的关键映射：
        #   AssertionError    = 前视断言/切片越界被撞穿
        #   ValueError        = 结算口径不成立等校验失败
        #   CostProfileError  = 费率档位/倍数配置非法（ValueError 子类，显式列出便于审计）
        #   EvidenceGateError = 证据不足且显式 enforce_gate
        # 四者都必须 exit 2（校验失败）。若报 exit 4，调度器会当成「没数据、等下次」，
        # 于是真正的前视 bug 被当成空跑，白做。
        return {"ok": False, "exit": FR.EXIT_INVALID,
                "reason": "打分校验失败（%s）：%s"
                          % (type(exc).__name__, str(exc)[:180]),
                "elapsed": round(time.time() - t0, 2)}
    except Exception as exc:                       # noqa: BLE001
        # 真正的打分/导出异常用既有的 5；不再是「4 = 一切失败」的垃圾桶
        return {"ok": False, "exit": FR.EXIT_EXPORT,
                "reason": "打分异常（本轮无产物）：%s: %s"
                          % (type(exc).__name__, str(exc)[:100]),
                "elapsed": round(time.time() - t0, 2)}
    out = {"ok": True, "out": res.get("out"), "factors": res.get("factors"),
           "picks": res.get("picks"), "validation": res.get("validation"),
           "recentOnlyDeprecated": res.get("recentOnlyDeprecated"),
           "evidenceGate": res.get("evidenceGate"),
           "settlement": res.get("settlement"),
           "costSensitivity": res.get("costSensitivity"),
           # 滚动验证的期数（打分不依赖它，只作为 verify 段的对照探针）
           "rollingPeriods": int((rolling or {}).get("n_periods") or 0),
           "elapsed": round(time.time() - t0, 2)}
    if verbose:
        print("[7/%d score] %d 个因子参与合成，推荐 %s 只，%s"
              % (N_STEPS, out["factors"], out["picks"],
                 (out["validation"] or {}).get("ic_mean")), flush=True)
    return out


def step_export(conn: sqlite3.Connection, rolling: Dict[str, Any],
                config: Dict[str, Any], partial: bool = False,
                warnings: Optional[List[str]] = None,
                verbose: bool = True) -> Dict[str, Any]:
    """step 8：导出数据契约（`export_factor_api.export`，写 `data/factor_api.json`）。

    除因子数值外还注入：`evalAsOf`（取 `factor_eval.json` 的 mtime）、
    `dataThrough / lastCalcAt / lastCalcOk / lastCalcError / nextRunAt`、
    `freshness`（4 态徽标）、`rolling`（滚动验证卡片）。页面只做展示，
    **不得自己按 `generatedAt` 推算数据日期**（T05 的 grep 断言）。
    """
    t0 = time.time()
    import export_factor_api as EFA

    try:
        res = EFA.export(conn=conn, out=EFA.OUT, eval_path=EFA.EVAL,
                         seg_path=EFA.SEG,
                         factor_time=str(config.get("factorTime", "16:10")),
                         partial=partial, warnings=warnings, verbose=verbose)
    except Exception as exc:                       # noqa: BLE001
        return {"ok": False, "exit": FR.EXIT_EXPORT,
                "reason": "导出异常：%s: %s" % (type(exc).__name__, str(exc)[:100]),
                "elapsed": round(time.time() - t0, 2)}
    if not res.get("ok"):
        return {"ok": False, "exit": FR.EXIT_EXPORT,
                "reason": res.get("reason") or "导出失败",
                "elapsed": round(time.time() - t0, 2)}
    out = {"ok": True, "out": res.get("out"), "factors": res.get("factors"),
           "pairs": res.get("pairs"), "evalAsOf": res.get("evalAsOf"),
           "dataThrough": res.get("dataThrough"),
           "rollingPeriods": res.get("rollingPeriods"),
           "elapsed": round(time.time() - t0, 2)}
    if verbose:
        print("[8/%d export] %d 个因子 / %d 对高相关 → %s（evalAsOf=%s）"
              % (N_STEPS, out["factors"], out["pairs"], out["out"], out["evalAsOf"]),
              flush=True)
    return out


# ────────────────────────────────────────────────────────────
# 编排
# ────────────────────────────────────────────────────────────
def run_pipeline(db: str = DB, trigger: str = "manual", workers: int = 8,
                 today: Optional[str] = None, dry_run: bool = False,
                 verbose: bool = True) -> Dict[str, Any]:
    """跑一次完整的因子 pipeline。

    Returns:
        {"exit": 退出码, "runId", "plan", "compute", "cache", "rolling",
         "score", "cost", "error"}
    """
    run_id = time.strftime("%Y%m%dT%H%M%S")
    t0 = time.time()
    config = dict(DEFAULTS)

    # --dry-run 必须零副作用：不抢锁、不写 factor_run.json、不动缓存。
    # 否则一次「只想看看缺口」的操作会把生产的时间源改成「上次运行失败」。
    if dry_run:
        conn0: Optional[sqlite3.Connection] = None
        try:
            conn0 = sqlite3.connect(db, timeout=120)
            planned = step_plan(conn0, today=today, verbose=verbose)
            if not planned["ok"]:
                if verbose:
                    print("！！ %s" % (planned.get("reason") or "计划阶段失败"), flush=True)
                return {"exit": FR.EXIT_INVALID, "runId": None, "plan": None,
                        "reason": planned.get("reason"), "dryRun": True,
                        "error": planned.get("reason"),
                        "cost": round(time.time() - t0, 1)}
            return {"exit": FR.EXIT_OK, "runId": None, "plan": planned.get("plan"),
                    "coverage": planned.get("coverage"), "dryRun": True,
                    "error": None, "cost": round(time.time() - t0, 1)}
        finally:
            if conn0 is not None:
                conn0.close()

    lock = FR.acquire_lock(run_id=run_id)
    if not lock["ok"]:
        if verbose:
            print("[lock] %s" % lock["reason"], flush=True)
        return {"exit": FR.EXIT_LOCKED, "runId": None, "reason": lock["reason"],
                "cost": round(time.time() - t0, 1), "error": lock["reason"]}

    result: Dict[str, Any] = {"exit": FR.EXIT_OK, "runId": run_id, "plan": None,
                              "compute": None, "cache": None, "price": None,
                              "ts": None, "rolling": None, "score": None,
                              "export": None, "cost": 0.0, "error": None,
                              "partial": False, "warnings": []}
    # 本次运行的「缺失环节」清单。目前只有一种来源：滚动验证无有效期。
    # 它必须一路传到 `FR.finish(..., warnings=warnings)`，否则页面只会看到
    # 一个「更新成功」，而滚动卡片空着没人解释。
    warnings: List[str] = []
    conn: Optional[sqlite3.Connection] = None
    phase = "plan"
    try:
        conn = sqlite3.connect(db, timeout=120)
        conn.execute("PRAGMA journal_mode=WAL")   # 保证「边写边读」不被锁住
        FR.begin(run_id, None, trigger)
        FR.set_phase(phase, db=db, config=config)

        # ---- step 1 plan ----
        planned = step_plan(conn, today=today, verbose=verbose)
        result["plan"] = planned.get("plan")
        if not planned["ok"]:
            error = planned.get("reason") or "计划阶段失败"
            FR.finish(False, error=error, cost=time.time() - t0, warnings=warnings,
                      verify={"coverage": planned.get("coverage"), "axis": None})
            result.update(exit=FR.EXIT_INVALID, error=error,
                          cost=round(time.time() - t0, 1))
            if verbose:
                print("！！ %s" % error, flush=True)
            return result

        plan_info = planned["plan"] or {}
        target_dates = list(plan_info.get("targetDates") or [])
        FR.set_phase("plan", targetDates=target_dates, lagDays=plan_info.get("lagDays"))

        # ---- step 2 compute ----
        phase = "compute"
        FR.set_phase(phase, targetDates=target_dates)
        comp = step_compute(conn, target_dates, workers=workers, verbose=verbose)
        result["compute"] = {k: comp.get(k) for k in
                             ("ok", "skipped", "plainRows", "targetDates",
                              "residualDates", "symbolsDone", "elapsed")}
        if not comp.get("ok"):
            error = comp.get("reason") or "增量计算失败"
            FR.finish(False, error=error, cost=time.time() - t0, warnings=warnings,
                      verify={"coverage": planned.get("coverage"),
                              "axis": None, "compute": result["compute"]})
            result.update(exit=FR.EXIT_INVALID, error=error,
                          cost=round(time.time() - t0, 1))
            if verbose:
                print("！！ %s" % error, flush=True)
            return result

        # ---- step 3 cache（周一追加全轴校验，Q6 决策）----
        phase = "cache"
        FR.set_phase(phase)
        full_verify = _is_week_start(today)
        if verbose and full_verify:
            print("[3/%d cache] 今天是周一：提交后追加一次全轴校验（+25s）"
                  % N_STEPS, flush=True)
        cache = step_cache(conn, verbose=verbose, full_verify=full_verify)
        result["cache"] = cache
        if not cache.get("ok"):
            error = cache.get("reason") or "缓存追加失败"
            FR.finish(False, error=error, cost=time.time() - t0, warnings=warnings,
                      verify={"coverage": planned.get("coverage"),
                              "axis": (cache.get("tail") or {}),
                              "cache": cache})
            result.update(exit=FR.EXIT_INVALID, error=error,
                          cost=round(time.time() - t0, 1))
            if verbose:
                print("！！ %s" % error, flush=True)
            return result

        # ---- step 4 price：价格宽表（惰性，全流程只加载一次）----
        # 这里**只是创建 bundle**，不读库。step_ts 第一次访问时付那 54.7 秒，
        # step_rolling（纯切片）不再访问，所以整条链路只付一次。
        phase = "price"
        FR.set_phase(phase)
        bundle = PriceBundle(conn, config)
        result["price"] = bundle.summary()

        # ---- step 5 ts ----
        phase = "ts"
        FR.set_phase(phase)
        ts = step_ts(conn, bundle, config, run_id=run_id, verbose=verbose)
        result["ts"] = ts
        result["price"] = bundle.summary()          # 此时才知道真实加载耗时
        if not ts.get("ok"):
            error = ts.get("reason") or "因子时间序列步骤失败"
            FR.finish(False, error=error, cost=time.time() - t0, warnings=warnings,
                      verify={"coverage": planned.get("coverage"),
                              "axis": (cache.get("verify") or {}).get("axis"),
                              "price": bundle.summary(), "ts": ts})
            result.update(exit=int(ts.get("exit") or FR.EXIT_INVALID), error=error,
                          cost=round(time.time() - t0, 1))
            if verbose:
                print("！！ %s" % error, flush=True)
            return result

        # ---- step 6 rolling ----
        phase = "rolling"
        FR.set_phase(phase)
        roll = step_rolling(conn, bundle, config, verbose=verbose)
        result["rolling"] = roll
        if roll.get("no_period"):
            # 「本次无有效期」**不是失败**：名单与导出跟它无关，产物照写。
            # 若判成失败（exit 4），页面会亮「更新失败」红字，而名单其实是新的——
            # 用户要么全信要么全不信，而真相是「名单可信、验证缺一轮」。
            warnings.append("rolling: 本次无有效期（%s）"
                            % (roll.get("reason") or "面板期数不足"))
            if verbose:
                print("！！ rolling 本次无有效期，继续打分与导出（partial）：%s"
                      % (roll.get("reason") or "-"), flush=True)
        elif not roll.get("ok"):
            error = roll.get("reason") or "滚动验证失败"
            FR.finish(False, error=error, cost=time.time() - t0, warnings=warnings,
                      verify={"coverage": planned.get("coverage"),
                              "axis": (cache.get("verify") or {}).get("axis"),
                              "price": bundle.summary(), "ts": ts, "rolling": roll})
            result.update(exit=int(roll.get("exit") or FR.EXIT_INVALID),
                          error=error, cost=round(time.time() - t0, 1),
                          warnings=list(warnings))
            if verbose:
                print("！！ %s" % error, flush=True)
            return result

        # ---- step 7 score ----
        phase = "score"
        FR.set_phase(phase)
        score = step_score(conn, roll, config, partial=bool(warnings),
                           warnings=warnings, verbose=verbose)
        result["score"] = score
        if not score.get("ok"):
            error = score.get("reason") or "打分失败"
            FR.finish(False, error=error, cost=time.time() - t0, warnings=warnings,
                      verify={"coverage": planned.get("coverage"),
                              "axis": (cache.get("verify") or {}).get("axis"),
                              "price": bundle.summary(), "ts": ts,
                              "rolling": roll, "score": score})
            result.update(exit=int(score.get("exit") or FR.EXIT_EXPORT),
                          error=error, cost=round(time.time() - t0, 1))
            if verbose:
                print("！！ %s" % error, flush=True)
            return result

        # ---- step 8 export ----
        phase = "export"
        FR.set_phase(phase)
        export = step_export(conn, roll, config, partial=bool(warnings),
                            warnings=warnings, verbose=verbose)
        result["export"] = export
        if not export.get("ok"):
            error = export.get("reason") or "导出失败"
            FR.finish(False, error=error, cost=time.time() - t0, warnings=warnings,
                      verify={"coverage": planned.get("coverage"),
                              "axis": (cache.get("verify") or {}).get("axis"),
                              "price": bundle.summary(), "ts": ts,
                              "rolling": roll, "score": score, "export": export})
            result.update(exit=int(export.get("exit") or FR.EXIT_EXPORT),
                          error=error, cost=round(time.time() - t0, 1))
            if verbose:
                print("！！ %s" % error, flush=True)
            return result

        # ---- step 9 finish ----
        phase = "finish"
        axis = dict((cache.get("verify") or {}).get("axis") or {})
        axis["mode"] = "tail"
        if cache.get("fullVerify"):
            axis["full"] = cache["fullVerify"]
            axis["mode"] = "tail+full"
        verify_payload = {
            "ok": bool((cache.get("verify") or {}).get("ok", cache.get("skipped"))),
            "checked": int((cache.get("verify") or {}).get("checked") or 0),
            "maxRelErr": float((cache.get("verify") or {}).get("maxRelErr") or 0.0),
            "mismatch": int((cache.get("verify") or {}).get("mismatch") or 0),
            "axis": axis,
            "cacheSkipped": bool(cache.get("skipped")),
            "residualDates": comp.get("residualDates") or [],
            # 价格宽表是否被真正加载过（T03 已落地 → 应为 true 且 cost≈55s）
            "price": bundle.summary(),
            "ts": {"appended": ts.get("appended"),
                   "factors": ts.get("factors"),
                   "skipped": ts.get("skipped") or [],
                   "reconcile": (ts.get("reconcile") or {}).get("state"),
                   "elapsed": ts.get("elapsed")},
            "rolling": {"n_periods": roll.get("n_periods"),
                        "skippedFactors": roll.get("skippedFactors") or [],
                        "latest": roll.get("latest"),
                        "elapsed": roll.get("elapsed")},
            "score": {"ok": True, "factors": score.get("factors"),
                      "picks": score.get("picks"),
                      "recentOnlyDeprecated": score.get("recentOnlyDeprecated")},
            "export": {"ok": True, "evalAsOf": export.get("evalAsOf"),
                       "dataThrough": export.get("dataThrough"),
                       "rollingPeriods": export.get("rollingPeriods")},
            # 本次运行是否「主体成功、个别环节缺」。页面据此决定：
            # 亮绿灯（fresh）但滚动卡片写一行灰字，而不是整页红字。
            "partial": bool(warnings),
            "warnings": list(warnings),
        }
        FR.set_phase("finish", targetDates=target_dates)
        FR.finish(True, cost=time.time() - t0, verify=verify_payload,
                  target_dates=target_dates, partial=bool(warnings),
                  warnings=warnings)
        result["cost"] = round(time.time() - t0, 1)
        result["partial"] = bool(warnings)
        result["warnings"] = list(warnings)
        if verbose:
            print("[%d/%d finish] ok=true%s，耗时 %.1fs（%.1f 分钟）"
                  % (N_STEPS, N_STEPS, "（partial：%s）" % "；".join(warnings)
                     if warnings else "", result["cost"], result["cost"] / 60.0),
                  flush=True)
        return result

    except Exception as exc:                             # noqa: BLE001 — 兜底，必须落 error
        error = "%s: %s" % (type(exc).__name__, str(exc)[:110])
        try:
            FR.finish(False, error=error, cost=time.time() - t0, warnings=warnings,
                      verify={"phase": phase})
        except Exception:                                # noqa: BLE001
            pass
        result.update(exit=FR.EXIT_INVALID, error=error,
                      cost=round(time.time() - t0, 1))
        if verbose:
            print("！！ 异常于 %s：%s" % (phase, error), flush=True)
        return result
    finally:
        # ---- 结算之后回写契约字段（修复「产物时间源滞后一轮」）----
        # step7 打分 / step8 导出在**结算之前**就写盘了，那时 `factor_run.json`
        # 顶层还是上一轮状态 → 产物里的 lastCalcAt 永远滞后一轮（第一次跑甚至是
        # None）。这里在 `FR.finish()` 之后**只回写契约字段**，不重跑打分与导出，
        # 也不碰任何数值字段。失败时同样回写：数据产物没变，但「这轮跑挂了」
        # 必须让页面看得见，否则页面顶着上一轮成功的绿徽标（共享知识第 3/8 条）。
        if conn is not None:
            try:
                import contract_sync
                import export_factor_api as _EFA
                import factor_score as _FS
                sync = contract_sync.backfill(
                    conn, [_FS.OUT, _EFA.OUT],
                    factor_time=str(config.get("factorTime", "16:10")),
                    verbose=verbose)
                result["contractSync"] = {"ok": bool(sync.get("ok")),
                                          "patched": sync.get("patched") or [],
                                          "reason": sync.get("reason")}
            except Exception as exc:                   # noqa: BLE001 — 回填失败不改变结算结果
                result["contractSync"] = {"ok": False, "patched": [],
                                          "reason": "%s: %s" % (type(exc).__name__,
                                                               str(exc)[:100])}
                if verbose:
                    print("！！ 契约字段回写失败：%s" % result["contractSync"]["reason"],
                          flush=True)
        if conn is not None:
            try:
                conn.close()
            except Exception:                            # noqa: BLE001
                pass
        FR.release_lock(lock.get("token"))


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="每日因子重算编排（plan/compute/cache/…）")
    ap.add_argument("--db", default=DB)
    ap.add_argument("--trigger", default="manual",
                    choices=["manual", "scheduler", "retry"])
    ap.add_argument("--workers", type=int, default=8)
    ap.add_argument("--today", default=None, help="今天（YYYY-MM-DD），限定缺口上界")
    ap.add_argument("--dry-run", action="store_true", help="只做 plan，不写任何东西")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args(argv)

    res = run_pipeline(db=args.db, trigger=args.trigger, workers=args.workers,
                       today=args.today, dry_run=args.dry_run,
                       verbose=not args.quiet)
    if res.get("error") and not args.quiet:
        print("结果：exit=%s error=%s" % (res["exit"], res["error"]), flush=True)
    return int(res.get("exit", FR.EXIT_OK))


if __name__ == "__main__":
    raise SystemExit(main())
