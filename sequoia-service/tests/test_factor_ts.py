#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_factor_ts.py — T03 因子时间序列（`data/factor_ts/`）的验收测试

覆盖的验收条目（说明书 §9 T03）：
  3. 随机 3 个日期独立重算 `ic` / `groups`，与存档一致（`ic` 容差 1e-5）
  4. 篡改 `meta.horizon` → `ensure()` 判失效并**全量重建**，不复用旧序列
  5. 人为把 `ts.last` 改到 `panelMaxDate` → 走 `.bak` 保护式重算：
     一致则替换、不一致则保留 `.bak` + `state="suspect"`，全程不丢数据
  6. 断言 `ts.last <= panelMax - horizon`（约束 A 的数据层探针）

外加几条只有实现者才会想到的守卫：
  * `WILL_PERSIST == ("ic", "groups")` 且 npz 里两个量都在、`groups` 是 (n, 10)
    —— 只存 `ic` 会让 `verdict_of` 的 `mono` 恒为 None，选中集合恒空且不报错；
  * `ensure()` 的 `coverage()` 断言（不齐必须抛 `CacheIncomplete`，退出码 2）；
  * `append()` 的日期集来自 `factor_panel` 真实行（不是调用方传的 axis）；
  * 对账三档：`ok` / `warn(dataWarn)` / `warn(caliberWarn)` / `suspect` 优先级。

全部在**合成数据 + 临时目录**上跑（见 `tests/synth_env.py`）：
生产库、`data/panel_cache/`、`data/factor_ts/` 一律不碰。

运行：
    python tests/test_factor_ts.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
if TESTS_DIR not in sys.path:
    sys.path.insert(0, TESTS_DIR)
TESTS = os.path.dirname(os.path.abspath(__file__))
for path in (HERE, TESTS):
    if path not in sys.path:
        sys.path.insert(0, path)

import factor_eval as FE                        # noqa: E402
import factor_run as FR                         # noqa: E402
import factor_ts as TS                          # noqa: E402
import rolling_eval as RE                       # noqa: E402
from factors import registry                    # noqa: E402
from synth_env import PARAMS, SynthEnv          # noqa: E402
import prod_guard                               # noqa: E402

PASS = 0
FAIL = 0
FAILS = []


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  — %s" % detail) if detail else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  ✗ %s%s" % (name, ("  — %s" % detail) if detail else ""))


def build(env: SynthEnv, axis=None, params=None):
    """跑一次 `ensure()`（测试一律 `strict=False`：合成环境没有真实缓存目录）。"""
    used = list(axis or env.dates)
    return TS.ensure(env.conn, env.fwd(axis=used), params or PARAMS,
                     panel_dates_axis=used, strict=False, verbose=False)


# ────────────────────────────────────────────────────────────
# case 1：初始化结构与两个不变式
# ────────────────────────────────────────────────────────────
def case_structure(tmp: str) -> None:
    print("\n[1] 初始化结构：24 个 npz / ic+groups 双量 / 轴严格递增 / 末端不越过 horizon")
    env = SynthEnv(tmp, "s1", n_dates=260)
    try:
        env.patch_panel_loader()
        res = build(env)
        ok("ensure 动作为 init", res["action"] == "init", res["action"])
        ok("24 个因子全部建成", len(res["rebuilt"]) == len(registry.all_names()),
           "%d 个" % len(res["rebuilt"]))
        missing = [n for n in registry.all_names() if not os.path.exists(TS.ts_path(n))]
        ok("磁盘上 24 个 npz 都存在", not missing, "缺 %s" % missing)

        meta = TS.load_meta()
        ok("meta 四个口径字段齐全",
           all(meta.get(k) is not None for k in ("horizon", "entry", "minStocks", "nGroups")),
           {k: meta.get(k) for k in ("horizon", "entry", "minStocks", "nGroups")})
        ok("meta.sig 三项齐全（panelMinDate / panelRebuildId / factorSig）",
           set((meta.get("sig") or {}).keys()) ==
           {"panelMinDate", "panelRebuildId", "factorSig"},
           meta.get("sig"))
        ok("meta.willPersist 明确记录 ic+groups",
           tuple(meta.get("willPersist") or ()) == ("ic", "groups"),
           str(meta.get("willPersist")))

        name = "mom20"
        with np.load(TS.ts_path(name)) as z:
            keys = set(z.files)
            groups_shape = tuple(z["groups"].shape)
            n_dates = int(z["dates"].size)
            n_ic = int(z["ic"].size)
            dates = np.asarray(z["dates"]).astype("U10")
        ok("npz 里同时有 ic 与 groups 两个量", keys == {"dates", "groups", "ic"}, str(keys))
        ok("groups 形状是 (n, nGroups=10)", groups_shape[1] == PARAMS["nGroups"],
           str(groups_shape))
        ok("n(ic) == n(dates)（失效矩阵第 5 条的判据）", n_ic == n_dates,
           "%d vs %d" % (n_ic, n_dates))
        ok("dates 严格递增", bool(np.all(dates[1:] > dates[:-1])),
           "%s ~ %s" % (dates[0], dates[-1]))

        cutoff = env.cutoff()
        bad = []
        for factor in registry.all_names():
            last = TS.last_date(factor)
            if last is None or str(last) > str(cutoff):
                bad.append((factor, last))
        ok("全部因子 ts.last <= panelMax - horizon（约束 A 数据层探针）",
           not bad, "cutoff=%s 越界=%s" % (cutoff, bad[:3]))

        # 每因子独立轴：预热期不同 → n 不完全相同，但都 <= 面板天数 - horizon
        ns = {n: (TS._load_series(n) or {}).get("dates", np.asarray([])).size
              for n in registry.all_names()}
        ok("每因子独立轴且长度合法",
           all(0 < v <= len(env.dates) for v in ns.values()),
           "n 范围 %d ~ %d" % (min(ns.values()), max(ns.values())))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 2：随机 3 个日期独立重算（验收第 3 条，容差 1e-5）
# ────────────────────────────────────────────────────────────
def case_recompute(tmp: str) -> None:
    print("\n[2] 随机 3 个日期独立重算 ic / groups（容差 1e-5）")
    env = SynthEnv(tmp, "s2", n_dates=260)
    try:
        env.patch_panel_loader()
        build(env)
        fwd = env.fwd(axis=env.dates)
        rng = np.random.default_rng(20261116)
        worst_ic = 0.0
        worst_grp = 0.0
        checked = 0
        for factor in ("mom60", "ret1", "dist_high250"):
            ser = TS._load_series(factor)
            assert ser is not None
            direction = int(registry.get(factor)["direction"])
            picks = rng.choice(len(ser["dates"]), size=3, replace=False)
            for pos in picks:
                date = str(ser["dates"][pos])
                sub = env.panel(factor).loc[[date]]
                ic = FE.daily_ic(sub, fwd, min_stocks=PARAMS["minStocks"])
                groups, _m = FE.group_returns(sub, fwd, direction,
                                              n_groups=PARAMS["nGroups"],
                                              min_stocks=PARAMS["minStocks"])
                if len(ic) == 0:
                    continue
                checked += 1
                worst_ic = max(worst_ic, abs(float(ic.iloc[0]) - float(ser["ic"][pos])))
                g = groups.reindex(index=ic.index,
                                   columns=list(range(1, PARAMS["nGroups"] + 1)))
                got = g.to_numpy(dtype="float64")[0]
                want = np.asarray(ser["groups"][pos], dtype="float64")
                finite = np.isfinite(got) & np.isfinite(want)
                if finite.any():
                    worst_grp = max(worst_grp, float(np.max(np.abs(got[finite] - want[finite]))))
        ok("3 个因子 × 3 个随机日期都重算成功", checked == 9, "checked=%d" % checked)
        ok("ic 最大绝对差 <= 1e-5（float32 存档）", worst_ic <= 1e-5, "%.2e" % worst_ic)
        ok("groups 最大绝对差 <= 1e-5", worst_grp <= 1e-5, "%.2e" % worst_grp)
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 3：追加 / 幂等 / skipped 语义
# ────────────────────────────────────────────────────────────
def case_append(tmp: str) -> None:
    print("\n[3] 追加：日期集来自 factor_panel 真实行 / 幂等 / skipped 只记越界日期")
    env = SynthEnv(tmp, "s3", n_dates=260, n_extra=3)
    try:
        env.patch_panel_loader()
        build(env, axis=env.dates)
        before = {n: TS.last_date(n) for n in registry.all_names()}
        cutoff1 = env.cutoff(axis=env.dates)
        ok("初次建库后末端正好是 panelMax-horizon",
           all(v == cutoff1 for v in before.values()), "%s" % cutoff1)

        t0 = time.time()
        app = TS.append(env.conn, env.fwd(axis=env.dates_all),
                        panel_dates_axis=env.dates_all, verbose=False)
        ok("append 成功且有新增", bool(app["ok"]) and app["appended"] > 0,
           "+%d 点，%.1fs" % (app["appended"], time.time() - t0))
        cutoff2 = env.cutoff(axis=env.dates_all)
        ok("append 后末端是新的 panelMax-horizon",
           all(TS.last_date(n) == cutoff2 for n in registry.all_names()),
           "%s（原 %s）" % (cutoff2, cutoff1))
        ok("skipped 只记「因 > panelMax-horizon 算不出 IC」的日期",
           app["skipped"] == [d for d in env.dates_all if d > cutoff2],
           "%s" % app["skipped"])
        ok("候选日期全部来自库里真实行（unexpectedDates 为空）",
           app["unexpectedDates"] == [], "%s" % app["unexpectedDates"][:3])

        again = TS.append(env.conn, env.fwd(axis=env.dates_all),
                          panel_dates_axis=env.dates_all, verbose=False)
        ok("连跑两次 append 幂等（第二次 +0）",
           bool(again["ok"]) and again["appended"] == 0, "+%d" % again["appended"])
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 4：slice_until 不泄漏未来
# ────────────────────────────────────────────────────────────
def case_slice(tmp: str) -> None:
    print("\n[4] slice_until：cut 之后一行都不能出现 / window 截断正确")
    env = SynthEnv(tmp, "s4", n_dates=260)
    try:
        env.patch_panel_loader()
        build(env)
        name = "rev10"
        ser = TS._load_series(name)
        cut = str(ser["dates"][120])
        sl = TS.slice_until(name, cut, window=30)
        ok("切片非空", sl is not None and sl["n"] == 30, "n=%s" % (sl or {}).get("n"))
        ok("切片最后一行 <= cut", sl["last"] <= cut, "%s <= %s" % (sl["last"], cut))
        ok("切片内没有任何 date > cut", bool(np.all(sl["dates"] <= cut)),
           "max=%s" % max(str(d) for d in sl["dates"]))
        full = TS.slice_until(name, cut, window=None)
        expect = int(np.sum(ser["dates"] <= cut))
        ok("window=None 时返回 cut 之前的全部行", full["n"] == expect,
           "%d vs %d" % (full["n"], expect))
        empty = TS.slice_until(name, "1990-01-01", window=10)
        ok("cut 早于序列起点时返回空（不抛异常）",
           empty is not None and empty["n"] == 0, "n=%s" % (empty or {}).get("n"))
        ok("不存在的因子返回 None", TS.slice_until("no_such_factor", cut) is None)
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 5：失效矩阵第 1/3/4 条 → 全量重建（验收第 4 条）
# ────────────────────────────────────────────────────────────
def case_invalidation_global(tmp: str) -> None:
    print("\n[5] 失效矩阵第 1/3/4 条：口径或面板签名变化 → 全量重建")
    env = SynthEnv(tmp, "s5", n_dates=180)
    try:
        env.patch_panel_loader()
        build(env)
        first_built = TS.load_meta().get("builtAt")

        meta = TS.load_meta()
        meta["horizon"] = 3
        TS.save_meta(meta)
        # builtAt 是秒级 ISO（factor_run.ISO_FMT）。首建与本步可能落在同一秒，
        # 用 != 断言会把「同一秒内的正确重建」误判成失败（首轮 1 fail、复跑 71/0）。
        # 这里改成「不早于」：既允许同秒，又要求它至少是本次重建时刻，
        # 不是把断言放宽成永真 —— 若 ensure 漏写 builtAt，仍会被 just_before 抓住。
        just_before = time.strftime("%Y-%m-%dT%H:%M:%S")
        # 再用确定性假时钟把「确实刷新了」钉死：不 sleep、不依赖两轮跑在不同秒。
        # builtAt 唯一来源是 FR.iso_now()，换掉它就能严格断言新值被写进去。
        real_iso = FR.iso_now
        fake_now = "2099-01-01T00:00:00"
        try:
            FR.iso_now = lambda: fake_now
            res = build(env)
        finally:
            FR.iso_now = real_iso
        ok("篡改 meta.horizon 后 ensure 判定 rebuild", res["action"] == "rebuild",
           res["action"])
        ok("reasons 明确指出是口径变化",
           any("口径变化" in r for r in res["reasons"]), str(res["reasons"]))
        meta = TS.load_meta()
        ok("重建后 meta.horizon 恢复成当前口径", int(meta["horizon"]) == PARAMS["horizon"],
           str(meta["horizon"]))
        ok("重建确实重算了（24 个因子都进了 rebuilt）",
           len(res["rebuilt"]) == len(registry.all_names()), "%d" % len(res["rebuilt"]))
        built_at = str(TS.load_meta().get("builtAt") or "")
        ok("builtAt 不回退且不早于本次重建时刻（秒级同秒允许）",
           bool(built_at) and built_at >= str(first_built) and built_at >= just_before,
           "%s → %s（重建前 %s）" % (first_built, built_at, just_before))
        ok("builtAt 确实被本次重建刷新（确定性假时钟）", built_at == fake_now,
           "%s → %s" % (first_built, built_at))

        meta = TS.load_meta()
        meta["sig"]["panelMinDate"] = "1999-01-01"
        TS.save_meta(meta)
        res = build(env)
        ok("篡改 panelMinDate 后同样全量重建", res["action"] == "rebuild",
           str(res["reasons"]))

        meta = TS.load_meta()
        meta["sig"]["factorSig"] = "deadbeef"
        TS.save_meta(meta)
        res = build(env)
        ok("篡改 factorSig 后同样全量重建",
           res["action"] == "rebuild" and any("因子集合" in r for r in res["reasons"]),
           str(res["reasons"]))

        res = build(env)
        ok("结构一致时 ensure 不再重建（noop）", res["action"] == "noop", res["action"])
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 6：失效矩阵第 5 条 → 只重建该因子
# ────────────────────────────────────────────────────────────
def case_invalidation_factor(tmp: str) -> None:
    print("\n[6] 失效矩阵第 5 条：单因子文件缺失 → 只修它（不重建全部）")
    env = SynthEnv(tmp, "s6", n_dates=180)
    try:
        env.patch_panel_loader()
        build(env)
        victim = "turn20"
        before = TS._load_series(victim)["ic"].copy()
        os.remove(TS.ts_path(victim))
        res = build(env)
        ok("只修了缺失的那一个", res["repaired"] == [victim] and not res["rebuilt"],
           "repaired=%s rebuilt=%s" % (res["repaired"], res["rebuilt"]))
        after = TS._load_series(victim)
        ok("修好后数值与原来逐位相同",
           after is not None and np.array_equal(before, after["ic"]),
           "n=%s" % (None if after is None else len(after["ic"])))

        moved = TS.invalidate([victim], reason="test")
        ok("invalidate 只改名成 .bak，不真删",
           moved["moved"] == [victim] and os.path.exists(TS.ts_path(victim) + ".bak"),
           str(moved["moved"]))
        ok("官方文件已不存在（下次 ensure 会重建）",
           not os.path.exists(TS.ts_path(victim)))
        res = build(env)
        ok("invalidate 之后 ensure 把它补回来", victim in res["repaired"],
           "repaired=%s" % res["repaired"])
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 7：第 6 条 —— 一致时替换、不一致时保留 .bak + suspect（验收第 5 条）
# ────────────────────────────────────────────────────────────
def case_protective(tmp: str) -> None:
    print("\n[7] 失效矩阵第 6 条：.bak 保护式重算（禁止直接截断）")
    # --- 7a：重叠区一致 → 替换 + 删 .bak ---
    env = SynthEnv(tmp, "s7a", n_dates=180)
    try:
        env.patch_panel_loader()
        build(env)
        victim = "vol20"
        panel_max = env.dates[-1]
        env.set_last(victim, panel_max, 0.1234)          # 模拟「上次把未来那天也写了」
        ok("人为把 ts.last 改到了 panelMaxDate",
           TS.last_date(victim) == panel_max, "%s" % TS.last_date(victim))
        res = build(env)
        actions = [(p["factor"], p["action"]) for p in res["protective"]]
        ok("第 6 条被触发且判定 replaced",
           (victim, "replaced") in actions, str(actions))
        ok("替换后删除了 .bak", not os.path.exists(TS.ts_path(victim) + ".bak"))
        ok("替换后末端回到 panelMax-horizon",
           TS.last_date(victim) == env.cutoff(), "%s" % TS.last_date(victim))
        ok("该因子仍是 ok（没有误标 suspect）",
           TS.states().get(victim) == "ok", str(TS.states().get(victim)))
    finally:
        env.cleanup()

    # --- 7b：重叠区不一致 → 保留 .bak + suspect + 滚动验证跳过 ---
    env = SynthEnv(tmp, "s7b", n_dates=180)
    try:
        env.patch_panel_loader()
        build(env)
        victim = "slope20"
        ser = TS._load_series(victim)
        corrupted = ser["ic"].copy()
        corrupted[-10] = corrupted[-10] + 0.5             # 破坏重叠区里的一天
        TS._save_series(victim, ser["dates"], corrupted, ser["groups"])
        env.set_last(victim, env.dates[-1], 0.4321)
        res = build(env)
        actions = [(p["factor"], p["action"]) for p in res["protective"]]
        ok("重叠区不一致 → 判定 suspect", (victim, "suspect") in actions, str(actions))
        ok("保留 .bak（退路不删）", os.path.exists(TS.ts_path(victim) + ".bak"))
        after = TS._load_series(victim)
        pos = None
        if after is not None:
            hits = np.flatnonzero(after["dates"] == str(ser["dates"][-10]))
            pos = int(hits[0]) if hits.size else None
        ok("正式文件保持不动（被破坏的那天还在）",
           pos is not None and abs(float(after["ic"][pos]) - float(corrupted[-10])) < 1e-9,
           "%.6f" % float(after["ic"][pos]) if pos is not None else "-")
        meta = TS.load_meta()
        entry = (meta.get("factors") or {}).get(victim) or {}
        ok("meta 里该因子 state=suspect 且有 suspectSince",
           entry.get("state") == "suspect" and entry.get("suspectSince"),
           "%s / %s" % (entry.get("state"), entry.get("suspectSince")))
        sus = TS.suspects()
        ok("suspects() 只含这一个因子",
           [s["factor"] for s in sus] == [victim], str(sus))

        prm = dict(PARAMS, nwLag=4, rollStep=20, rollSelectWindow=120)
        rr = RE.run(env.conn, prm, panel_dates_axis=env.dates, out=None, verbose=False)
        ok("滚动验证 skippedFactors 记录它（reason=suspect 且带 since）",
           rr["skippedFactors"] and rr["skippedFactors"][0]["factor"] == victim
           and rr["skippedFactors"][0]["reason"] == "suspect"
           and rr["skippedFactors"][0]["since"],
           str(rr["skippedFactors"]))
        # 「有没有参与判定」要看 n_considered（进 rows 的个数）：
        # n_evaluable 还受预热期影响（早期期可能整批「数据不足」），用它来断言
        # 「该因子是否被跳过」会得出错误结论 —— 跳过发生在 judge 之前。
        ok("since 之前的期该因子仍参与（n_considered = 24）",
           rr["periods"][0]["n_considered"] == len(registry.all_names()),
           "%d" % rr["periods"][0]["n_considered"])

        # since 落在期中时，之后的期必须少一个（逐期因子数变化可解释）
        since = rr["periods"][len(rr["periods"]) // 2]["asOf"]
        early = RE.evaluate_period(env.dates, rr["periods"][0]["tIndex"], prm,
                                   {victim: since})
        late = RE.evaluate_period(env.dates, rr["periods"][-1]["tIndex"], prm,
                                  {victim: since})
        ok("早于 since 的期 n_considered 更大、晚于 since 的更小",
           early["n_considered"] == late["n_considered"] + 1
           and victim in [s["factor"] for s in late["skipped"]],
           "%d vs %d（since=%s）" % (early["n_considered"], late["n_considered"], since))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 8：coverage 断言（共享知识第 15 条）
# ────────────────────────────────────────────────────────────
def case_coverage_guard(tmp: str) -> None:
    print("\n[8] ensure() 入口的 coverage 断言（不齐必须直接失败，退出码 2）")
    env = SynthEnv(tmp, "s8", n_dates=120)
    original = TS.PC.coverage
    try:
        env.patch_panel_loader()
        TS.PC.coverage = lambda conn=None, required=None: {
            "ok": False, "required": list(required or registry.all_names()),
            "present": ["mom20"], "missing": ["mom60", "turn20"],
            "badShape": [], "axis": {"dates": 100, "symbols": 300}}
        raised = None
        try:
            TS.ensure(env.conn, env.fwd(axis=env.dates), PARAMS,
                      panel_dates_axis=env.dates, strict=True, verbose=False)
        except TS.CacheIncomplete as exc:
            raised = exc
        ok("缓存不齐时抛 CacheIncomplete", raised is not None)
        ok("退出码是 2", getattr(raised, "exit_code", None) == 2,
           str(getattr(raised, "exit_code", None)))
        ok("错误信息里给出 --build-all 的提示",
           raised is not None and "--build-all" in str(raised), str(raised)[:80])

        TS.PC.coverage = original
        res = TS.ensure(env.conn, env.fwd(axis=env.dates), PARAMS,
                        panel_dates_axis=env.dates, strict=False, verbose=False)
        ok("strict=False（仅临时库测试）可以绕过断言", bool(res["ok"]), res["action"])
    finally:
        TS.PC.coverage = original
        env.cleanup()


# ────────────────────────────────────────────────────────────
# case 9：与 factor_eval.json 对账四档（§5.7）
# ────────────────────────────────────────────────────────────
def case_reconcile(tmp: str) -> None:
    print("\n[9] 与 factor_eval.json 对账：ok / dataWarn / caliberWarn / suspect 优先")
    env = SynthEnv(tmp, "s9", n_dates=180)
    try:
        env.patch_panel_loader()
        build(env)
        env.write_eval(names=("mom20", "rev10"))
        rec = TS.reconcile_with_eval(params=PARAMS)
        ok("完全一致时 state=ok", rec["state"] == "ok", rec["state"])
        ok("maxAbsDiff 为 0（float32 原样往返）", rec["maxAbsDiff"] <= 1e-12,
           "%.2e" % rec["maxAbsDiff"])
        ok("extraDays 为 0", rec["extraDays"] == 0, str(rec["extraDays"]))
        ok("重叠量字段叫 commonFactorDays（(因子,日) 累加，不是交易日数）",
           rec["commonFactorDays"] > 0 and "commonDays" not in rec,
           "%d 个(因子,日)" % rec["commonFactorDays"])
        ok("跨口径阈值 rtol = 1e-3（与同口径的 PROTECT_RTOL=1e-5 分开）",
           rec["rtol"] == TS.RECONCILE_NUM_RTOL == 1e-3
           and TS.PROTECT_RTOL == 1e-5,
           "rtol=%.0e protect=%.0e" % (rec["rtol"], TS.PROTECT_RTOL))

        ser = TS._load_series("mom20")
        series = [[str(d), float(v)] for d, v in zip(ser["dates"].tolist(),
                                                     ser["ic"].tolist())]

        # ① 口径量级的差（实测 P99.9=5.9e-05、max=2.04e-04）→ **不该**报警。
        #    旧实现用 1e-5 卡跨口径比对，导致 dataWarn 天天为真、state 永远 warn。
        series[50][1] = series[50][1] + 2e-4
        env.write_eval(ic_series={"mom20": series})
        rec = TS.reconcile_with_eval(params=PARAMS)
        ok("口径量级的差（2e-4）不再触发 dataWarn（阈值按实测量级定）",
           rec["dataWarn"] is False and rec["state"] == "ok",
           "%s dataWarn=%s maxAbsDiff=%.2e" % (rec["state"], rec["dataWarn"],
                                               rec["maxAbsDiff"]))
        ok("maxAbsDiff 仍照实记录（不是把差异抹掉，只是不升级成告警）",
           abs(rec["maxAbsDiff"] - 2e-4) < 1e-6, "%.2e" % rec["maxAbsDiff"])

        # ② 真实数值错误：某一天 IC 多出 0.05（差了一个量级）→ 必须变 warn。
        #    这条是「阈值上调之后仍然有牙齿」的证据，缺了它，把阈值调到 1 也能过。
        series[50][1] = series[50][1] - 2e-4 + 0.05
        env.write_eval(ic_series={"mom20": series})
        rec = TS.reconcile_with_eval(params=PARAMS)
        ok("真实数值错误（+0.05）→ state=warn 且 dataWarn=true",
           rec["state"] == "warn" and rec["dataWarn"] is True,
           "%s dataWarn=%s diff=%.2e" % (rec["state"], rec["dataWarn"],
                                         rec["maxAbsDiff"]))
        ok("maxAbsDiff 报出真实量级（≈0.05，没被 1e-3 抹平）",
           0.049 < rec["maxAbsDiff"] < 0.051, "%.4f" % rec["maxAbsDiff"])
        ok("caliberWarn 为 false（口径差异没超 5%）", rec["caliberWarn"] is False,
           "%.4f" % rec["extraPct"])
        ok("首次告警时间被记住（firstWarnAt）", bool(rec["firstWarnAt"]),
           str(rec["firstWarnAt"]))

        # caliberWarn：存档里有 200 天我们算不出来（100 口径更严）
        fake = [[d, 0.01] for d in env.dates[:200]] + series
        env.write_eval(ic_series={"mom20": fake})
        rec = TS.reconcile_with_eval(params=PARAMS)
        ok("extraPct > 5% → caliberWarn=true（仍是 warn，不是 suspect）",
           rec["caliberWarn"] is True and rec["state"] == "warn",
           "%.1f%% / state=%s" % (rec["extraPct"] * 100, rec["state"]))

        # suspect 优先级最高
        meta = TS.load_meta()
        TS._mark_suspect(meta, "mom20", "test")
        TS.save_meta(meta)
        rec = TS.reconcile_with_eval(params=PARAMS)
        ok("任一因子 suspect → state=suspect（压过 warn）",
           rec["state"] == "suspect" and rec["suspectFactors"] == ["mom20"],
           "%s / %s" % (rec["state"], rec["suspectFactors"]))
    finally:
        env.cleanup()


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="factor_ts_test_")
    t0 = time.time()
    print("=" * 72)
    print("T03 / factor_ts 验收测试（合成数据，不触碰生产库与 data/ 目录）")
    print("=" * 72)
    before = prod_guard.snapshot()
    try:
        case_structure(tmp)
        case_recompute(tmp)
        case_append(tmp)
        case_slice(tmp)
        case_invalidation_global(tmp)
        case_invalidation_factor(tmp)
        case_protective(tmp)
        case_coverage_guard(tmp)
        case_reconcile(tmp)
    finally:
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
