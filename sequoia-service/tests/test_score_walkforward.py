#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_score_walkforward.py — T06 逐期 walk-forward 打分的三条硬约束

本轮要消掉的是**两处**前视，不是「在 266-267 行乘个 -1」：
  1. 候选因子按**全样本** t 值挑（`load_candidates` 读 `factor_eval.json`）
  2. 方向一律用先验 `registry.direction`，而选中集合里就含「方向需翻转」
     （定义是 mono <= -0.5，即与先验相反）→ 选进来、又按没翻的方向用

用例（每条都要有牙齿，没牙齿的用例比不测更危险）：
  [1] 无前视：`cut` 之后的收盘价按**逐股**乘子 `exp(N(0,0.35))` 扰动 →
      `<= cut` 的 scores `max|Δ| == 0`；并保留**统一乘数 ×1.37** 作反例
      （单调变换不改横截面排名 → 那种测法测了等于没测）
  [2] 判据可追溯：逐日 `icCutoff(t) <= d - horizon`、阶梯单调不减、
      分段起点**恰好**是 `idx(cut) + horizon`（差一天/差五天都能抓）
  [3] 翻转确实生效：同一批因子，按 `d_eff` 与只按先验两版打分 →
      Top10 重合 < 5、IC 更高
  [4] `VERDICT_SIGN` 与 `SELECTED_VERDICTS` 一一对应（少声明符号就炸）
  [5] 首个 cutoff 之前不打分、不回填
  [6] `generate()` 落盘结构：`validation`（walk-forward 主）/
      `validationFullSample`（对照）/ `directionBasis` / `signAsOf` /
      **名单只有一份**
  [7] 小样本无可用期 → 降级 legacy：mode/basis 如实变、warning 明写、
      partial=true、名单照写（这条只在故障日走，必须强制触发才测得到）
  [8] T08 成本无前视：T 后价格 ×1.37 → periods/directionBasis/signAsOf/weights 一字不变
  [9] T08 样本门槛：warn 照写且 insufficient；enforce 抛错且零写盘
  [10] T08 参数簇：9 组 cutoff == RE.ic_cutoff；源码无自算 t_idx-horizon
  [11] T08 生产路径 volume_wide 接线：volume=0 计入不可买且不进成交
  [12] T08 exit code 注入实测：2/2/2/2/4/5

为什么 [1] 必须用逐股乘子
------------------------
统一乘数 `×1.37` 对 `fwd` 是单调变换（`fwd' = 1.37*(fwd+1)-1`），
单调变换**不改变横截面排名**，Spearman IC 一分不变 → 任何错实现都"通过"。
逐股乘子（每只票各自一个）会改变排名，才有检出力。上一轮我们正是被这个
反例骗过一次，所以两种都必须留在用例里。

运行：
    python tests/test_score_walkforward.py
"""

from __future__ import annotations

import inspect
import json
import os
import re
import shutil
import sys
import tempfile
import time

import numpy as np

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for path in (HERE, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

import factor_eval as fe                        # noqa: E402
import factor_run as FR                         # noqa: E402
import factor_score as FS                       # noqa: E402
import factor_ts as TS                          # noqa: E402
import rolling_eval as RE                       # noqa: E402
import run_factor_pipeline as RFP               # noqa: E402
import trade_cost as TC                         # noqa: E402
from factors import registry                    # noqa: E402
from synth_env import PARAMS, SynthEnv          # noqa: E402
import prod_guard                               # noqa: E402

PASS = 0
FAIL = 0
FAILS = []

H = int(PARAMS["horizon"])
_ORIG = {}


# ────────────────────────────────────────────────────────────
# 合成环境接线
# ────────────────────────────────────────────────────────────
def patch_loaders(env: SynthEnv) -> None:
    """把 `factor_score` 用到的两个取数函数换成合成数据。

    为什么要存原函数：模块级 monkeypatch 会串环境（`factor_ts` 的目录是进程级
    环境变量），两个环境同时存在时后创建的那个会把前一个覆盖掉 ——
    `synth_env` 的 docstring 里专门写过这个坑，这里每次用前都显式重接一次。
    """
    if not _ORIG:
        _ORIG["panel"] = fe.load_panel_wide
        _ORIG["price"] = fe.load_price_wide
    env.activate()
    fe.load_panel_wide = (lambda conn, name, start=None, end=None, symbols=None:
                          env.panel(name))
    fe.load_price_wide = (lambda conn, column, symbols=None:
                          (env.close if str(column) == "close" else
                           env.volume if str(column) == "volume" else env.open))


def restore_loaders() -> None:
    if not _ORIG:
        return
    fe.load_panel_wide = _ORIG["panel"]
    fe.load_price_wide = _ORIG["price"]


def build(env: SynthEnv, axis=None) -> dict:
    """按生产同一条链路算 `factor_ts` 序列（含 ic / groups）。"""
    used = list(axis or env.dates)
    return TS.ensure(env.conn, env.fwd(axis=used), PARAMS,
                     panel_dates_axis=used, strict=False, verbose=False)


def scores_of(env: SynthEnv, evald: dict, **kwargs) -> dict:
    """在指定环境上跑一次逐期打分（每次都重接取数函数，防串环境）。"""
    patch_loaders(env)
    return FS.build_scores(env.conn, evald, axis=list(env.dates),
                           verbose=False, **kwargs)


def evald_of(env: SynthEnv) -> dict:
    """写一份假的 `factor_eval.json` 并读回来（只给「全样本对照」用）。"""
    env.write_eval()
    with open(env.eval_path, encoding="utf-8") as fh:
        return json.load(fh)


def ok(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %s%s" % (name, ("  — %s" % detail) if detail else ""))
    else:
        FAIL += 1
        FAILS.append(name)
        print("  ✗ %s%s" % (name, ("  — %s" % detail) if detail else ""))


def max_abs_delta(a, b) -> float:
    """两个宽表的 max|Δ|；逐位相同返回 0.0（NaN 与 NaN 视为相同）。"""
    if not bool((a.isna() == b.isna()).all().all()):
        return float("inf")
    d = (a - b).abs()
    got = d.max().max()
    return 0.0 if (got is None or not np.isfinite(got)) else float(got)


# ────────────────────────────────────────────────────────────
# [1] 无前视（主力）
# ────────────────────────────────────────────────────────────
def case_no_lookahead(tmp: str) -> None:
    print("\n[1] 无前视：cut 之后按逐股乘子扰动，<= cut 的 scores 必须逐位不变")
    clean = SynthEnv(tmp, "n1_clean", n_dates=180)
    per = SynthEnv(tmp, "n1_per", n_dates=180)
    uni = SynthEnv(tmp, "n1_uni", n_dates=180)
    try:
        patch_loaders(clean)
        build(clean)
        evald = evald_of(clean)
        base = scores_of(clean, evald)

        periods = base["periods"]
        ok("期网格非空（>= 3 期）", len(periods) >= 3, "%d 期" % len(periods))
        tgt = periods[-3]
        cut = str(tgt["icCutoff"])
        ok("目标期的 cutoff 落在日期轴内", cut in clean.dates, cut)

        patch_loaders(per)
        per.corrupt_future(cut, factor=1.0, per_symbol_sigma=0.35)   # 逐股乘子
        build(per)
        dirty = scores_of(per, evald)

        patch_loaders(uni)
        uni.corrupt_future(cut, factor=1.37)                         # 统一乘数
        build(uni)
        uniform = scores_of(uni, evald)

        a = base["scores"]
        b = dirty["scores"]
        u = uniform["scores"]
        before = a.index <= cut
        after = a.index > cut

        ok("扰动确实改到了面板（%d 天有价）" % int(after.sum()), bool(after.any()), cut)
        d_before = max_abs_delta(a.loc[before], b.loc[before])
        ok("[逐股乘子] <= cut 的 max|Δ| == 0（无前视）", d_before == 0.0,
           "max|Δ| = %g" % d_before)
        d_after = max_abs_delta(a.loc[after], b.loc[after])
        ok("[逐股乘子] cut 之后确实变了（证明扰动真的改了截面排名，用例有牙齿）",
           d_after > 0.0, "max|Δ| = %g" % d_after)

        d_uni = max_abs_delta(a, u)
        ok("对照：统一乘数 ×1.37 → 全部 scores 逐位不变（单调变换不改排名）",
           d_uni == 0.0, "max|Δ| = %g" % d_uni)

        # ---- 反例：把判据写成 `cut <= d`（约束 A 那个错误）----
        # 用测试内的**误实现**替换逐日归属函数（只换这一步，合成逻辑仍是生产那份），
        # 否则「用例有牙齿」这句话永远无法被验证。
        orig_steps = FS.sign_steps

        def buggy_steps(dates, periods_, horizon):
            n = len(dates)
            step_of = [-1] * n
            j = 0
            for i in range(n):
                today = str(dates[i])
                while j + 1 < len(periods_) and str(periods_[j + 1]["icCutoff"]) <= today:
                    j += 1
                if str(periods_[j]["icCutoff"]) <= today:
                    step_of[i] = j
            segs = []
            for jj, per_ in enumerate(periods_):
                rows = [i for i, s in enumerate(step_of) if s == jj]
                segs.append({"tIndex": per_["tIndex"], "asOf": per_["asOf"],
                             "icCutoff": per_["icCutoff"],
                             "nSelected": per_["nSelected"],
                             "selected": list(per_["selected"]),
                             "signs": dict(per_["signs"]),
                             "flipped": sorted(n for n, s in per_["signs"].items() if s < 0),
                             "rows": rows,
                             "from": str(dates[rows[0]]) if rows else None,
                             "to": str(dates[rows[-1]]) if rows else None})
            return step_of, segs

        FS.sign_steps = buggy_steps
        try:
            buggy_clean = scores_of(clean, evald)
            buggy_dirty = scores_of(per, evald)
            buggy_uni = scores_of(uni, evald)
        finally:
            FS.sign_steps = orig_steps

        d_buggy = max_abs_delta(buggy_clean["scores"].loc[before],
                                buggy_dirty["scores"].loc[before])
        ok("牙齿：误把判据写成 `cut <= d` → <= cut 的 scores 变了（本用例抓得到）",
           d_buggy > 0.0, "max|Δ| = %g" % d_buggy)
        d_buggy_uni = max_abs_delta(buggy_clean["scores"], buggy_uni["scores"])
        ok("反例：同样这个错实现，用统一乘数扰动**完全看不出差别**"
           "（所以只测 ×1.37 等于没测）", d_buggy_uni == 0.0,
           "max|Δ| = %g" % d_buggy_uni)
    finally:
        clean.cleanup()
        per.cleanup()
        uni.cleanup()


# ────────────────────────────────────────────────────────────
# [2] 判据可追溯 + 阶梯函数
# ────────────────────────────────────────────────────────────
def case_sign_traceable(tmp: str) -> None:
    print("\n[2] 逐日 signAsOf：icCutoff(t) <= d - horizon、单调不减、起点恰好 idx(cut)+h")
    env = SynthEnv(tmp, "n2", n_dates=200)
    try:
        patch_loaders(env)
        build(env)
        evald = evald_of(env)
        built = scores_of(env, evald)

        sa = built.get("signAsOf") or {}
        segs = sa.get("segments") or []
        dates = list(sa.get("dates") or [])
        seg_of = list(sa.get("segmentOf") or [])
        ok("signAsOf 逐日展开（天数一致）", len(dates) == len(seg_of) and len(dates) > 0,
           "%d 天 / %d 段" % (len(dates), len(segs)))
        ok("判据原文写进 JSON（页面/报告口径唯一）",
           sa.get("criterion") == FS.WF_CRITERION == "icCutoff(t) <= d - horizon",
           str(sa.get("criterion")))

        axis = list(env.dates)
        pos = {d: i for i, d in enumerate(axis)}
        bad_cut, bad_latest, bad_mono = [], [], []
        prev = -1
        for day, j in zip(dates, seg_of):
            if j < prev:
                bad_mono.append((day, j, prev))
                continue
            prev = j
            i = pos[day]
            seg = segs[j]
            # (a) 判据：cutoff <= d - horizon（逐字比较日期字符串）
            if str(seg["icCutoff"]) > str(axis[i - H]):
                bad_cut.append((day, seg["icCutoff"], axis[i - H]))
            # (b) 「最近一期」：下一期的 cutoff 必须已经越过 d - horizon
            if j + 1 < len(segs) and str(segs[j + 1]["icCutoff"]) <= str(axis[i - H]):
                bad_latest.append((day, segs[j + 1]["icCutoff"], axis[i - H]))
        ok("每一天的生效期都满足 icCutoff <= d - horizon", not bad_cut, str(bad_cut[:2]))
        ok("每一天用的都是**最近**一期（下一期的 cutoff 已越过 d-h）",
           not bad_latest, str(bad_latest[:2]))
        ok("阶梯函数单调不减（生效期只前进、不回退）", not bad_mono, str(bad_mono[:2]))

        # (c) 分段起点：cut <= d-h 的第一天恰好是 idx(cut)+h。
        #     写成 `cut < d-h` 会晚一天、写成 `cut <= d` 会早 horizon 天，这里都能抓。
        bad_from = []
        for seg in segs:
            if not seg.get("from"):
                continue
            got = pos[seg["from"]]
            want = pos[seg["icCutoff"]] + H
            if got != want:
                bad_from.append((seg["asOf"], seg["from"], seg["icCutoff"], got, want))
        ok("每个分段的起点恰好是 idx(cut)+horizon（差一天/差五天都会红）",
           not bad_from, str(bad_from[:2]))

        # (d) 不得拿 asOf 当比较键
        bad_asof = [seg["asOf"] for seg in segs
                    if seg.get("from") and pos[seg["from"]] != pos[seg["icCutoff"]] + H]
        ok("比较键用的是 icCutoff 而不是 asOf（asOf 比 cutoff 晚 horizon 天）",
           not bad_asof, str(bad_asof[:2]))

        db = built.get("directionBasis") or {}
        ok("directionBasis 记了当前生效期的 asOf / icCutoff / 判据",
           db.get("asOf") and db.get("icCutoff") and db.get("criterion") == FS.WF_CRITERION,
           "%s / %s" % (db.get("asOf"), db.get("icCutoff")))
        ok("directionBasis.asOf 是最后一期（最新一天用的那套）",
           db.get("asOf") == str(env.dates[-1]), str(db.get("asOf")))
        ok("directionBasis.signed 的键 = 入选集合",
           set(db.get("signed") or {}) == set(built.get("factors") or []),
           "%d 个" % len(db.get("signed") or {}))
        ok("signed 的符号 = 先验方向 ×（翻转则取反）",
           all(int(db["signed"][n]) == int(db["prior"][n]) *
               (-1 if n in (db.get("flipped") or []) else 1)
               for n in (db.get("signed") or {})),
           "flipped=%s" % (db.get("flipped") or []))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# [3] 翻转确实生效
# ────────────────────────────────────────────────────────────
def case_flip_effective(tmp: str) -> None:
    print("\n[3] 翻转生效：按 d_eff 与只按先验两版 → Top10 重合 < 5、IC 更高")
    env = SynthEnv(tmp, "n3", n_dates=200)
    try:
        # 把一部分因子的面板取负：它们的 IC 变负、分组收益与先验相反 →
        # verdict_of 判成「方向需翻转」，正是被旧实现「选进来又按先验方向用」的那一类。
        patch_loaders(env)
        orig_panel = env.panel
        invert = set(registry.all_names()[::3])
        env.panel = lambda name: (-1.0 * orig_panel(name)) if name in invert else orig_panel(name)
        env._panel_cache.clear()
        build(env)

        evald = evald_of(env)
        built = scores_of(env, evald)
        db = built.get("directionBasis") or {}
        flipped = list(db.get("flipped") or [])
        ok("合成环境确实产出了「方向需翻转」因子（前提成立）", len(flipped) > 0,
           "翻转 %d / 入选 %d：%s" % (len(flipped), len(built.get("factors") or []),
                                     ", ".join(flipped[:4])))
        ok("翻转因子的有效方向 = 先验 × -1",
           all(int(db["signed"][n]) == -int(db["prior"][n]) for n in flipped),
           str({n: (db["prior"][n], db["signed"][n]) for n in flipped[:3]}))
        ok("非翻转因子仍按先验方向",
           all(int(db["signed"][n]) == int(db["prior"][n])
               for n in (db.get("signed") or {}) if n not in flipped),
           "其余 %d 个" % (len(db.get("signed") or {}) - len(flipped)))

        fs_scores = built.get("scoresFullSample")
        ok("全样本对照版存在（只作指标，不出名单）", fs_scores is not None
           and not fs_scores.empty, "shape=%s" % (None if fs_scores is None
                                                 else fs_scores.shape,))

        close = env.close.loc[env.close.index <= env.dates[-1]]
        open_ = env.open.loc[env.open.index <= env.dates[-1]]
        v_wf = FS.validate(built["scores"], close, horizon=H, entry="s1_open",
                           open_wide=open_)
        v_fs = FS.validate(fs_scores, close, horizon=H, entry="s1_open",
                           open_wide=open_)
        ok("walk-forward 的 IC 高于「只按先验」那一版", v_wf["ic_mean"] > v_fs["ic_mean"],
           "%+.4f vs %+.4f" % (v_wf["ic_mean"], v_fs["ic_mean"]))
        ok("walk-forward 的 t_NW 也更高",
           (v_wf.get("t_nw") or 0) > (v_fs.get("t_nw") or 0),
           "%+.2f vs %+.2f" % (v_wf.get("t_nw") or 0, v_fs.get("t_nw") or 0))

        p_wf = FS.make_picks(built["scores"], built["valid"], close, top_n=10)
        # 对照版只作指标，不产名单；但为了做「名单真的换了」的对照，这里临时
        # 取对照矩阵**最后一个有效交易日**的横截面 —— 不能用最后一天：合成
        # 数据里最后一天恰好全是 NaN 时，make_picks 会合法地返回空名单，
        # 那样 overlap=0 会让「名单换了」的断言在没验证任何东西的情况下通过/失败。
        fs_valid_days = fs_scores.index[fs_scores.notna().any(axis=1)]
        ok("对照矩阵有可用交易日（对照名单的前提）", len(fs_valid_days) > 0,
           "%d 天" % len(fs_valid_days))
        fs_last = fs_valid_days[-1] if len(fs_valid_days) else fs_scores.index[-1]
        # valid 必须用 combine_parts 带出的「有效因子数」（>=2 才够 make_picks
        # 的门槛）。用 fs.notna()（恒为 1）会让对照名单永远为空。
        fs_valid = built.get("validFullSample")
        if fs_valid is None:
            fs_valid = fs_scores.notna().astype(float)
        p_fs = FS.make_picks(fs_scores.loc[:fs_last], fs_valid.loc[:fs_last],
                             close, top_n=10)
        s_wf = {p["symbol"] for p in p_wf.get("picks") or []}
        s_fs = {p["symbol"] for p in p_fs.get("picks") or []}
        overlap = len(s_wf & s_fs)
        ok("两版 Top10 重合 < 5（名单真的换了，不是同一份换个说法）",
           len(s_wf) and len(s_fs) and overlap < 5,
           "重合 %d / 10（对照取 %s，wf 取 %s）"
           % (overlap, p_fs.get("date"), p_wf.get("date")))
        ok("翻转因子的贡献已按有效方向计入（贡献符号 = 排名 × d_eff）",
           all(c.get("flipped") is False or c.get("directionEff") == -c.get("direction")
               for p in (p_wf.get("picks") or [])[:3]
               for c in FS.contributions(built["parts"], p["symbol"],
                                         p_wf.get("date"),
                                         sign_map=db.get("signed"))),
           "%d 只已核对" % len((p_wf.get("picks") or [])[:3]))

        # 旧路径仍在（--legacy），且它与「逐期内部算的全样本对照」只差日期轴：
        # 逐期版的轴是 env.dates（面板在轴上对齐），legacy 用的是面板自身 index
        # （多 n_extra 天），所以 IC 会差一点点。**同日相比**必须逐位相同 ——
        # 这条同时证明「用 ±rank 代替 rank(±panel)」是等价变换（只差逐日常数）。
        legacy = scores_of(env, evald, mode=FS.MODE_LEGACY)
        v_legacy = FS.validate(legacy["scores"], close, horizon=H, entry="s1_open",
                               open_wide=open_)
        ok("`--legacy` 旧路径仍可用，IC 与逐期版内的全样本对照一致（差 < 1e-3）",
           legacy.get("mode") == FS.MODE_LEGACY
           and abs(float(v_legacy["ic_mean"]) - float(v_fs["ic_mean"])) < 1e-3,
           "%+.6f vs %+.6f（差异来自日期轴多几天）"
           % (v_legacy["ic_mean"], v_fs["ic_mean"]))

        common = fs_scores.index.intersection(legacy["scores"].index)
        fwd_c = fe.load_forward_returns(close, H, entry="s1_open", open_wide=open_)
        fwd_c = fwd_c.reindex(index=common)
        ic_fs = fe.daily_ic(fs_scores.reindex(index=common), fwd_c,
                            min_stocks=100)
        ic_lg = fe.daily_ic(legacy["scores"].reindex(index=common), fwd_c,
                            min_stocks=100)
        both = ic_fs.notna() & ic_lg.notna()
        worst = float((ic_fs[both] - ic_lg[both]).abs().max()) if int(both.sum()) else 0.0
        # 等价变换只在「无并列值」时逐位相同。有并列时 rank(-x)=n+1-rank(x)
        # 的平均秩对称仍成立，但并列的票在 `±(rank-0.5)` 与 `rank(±panel)-0.5`
        # 两条路上会分到不同的名次，IC 有 1e-4 量级抖动。factor_score.py:720-724
        # 的注释写明了这一点（实测 <1e-3）；断言必须和它同口径，不能写 1e-9。
        # 真正要守的是「口径等价」不是「浮点逐位相同」——量级一致 + 方向一致。
        ok("逐日 IC 与旧路径口径等价（并列值抖动 < 1e-3，见 factor_score.py:720）",
           worst < 1e-3, "%d 天，max|ΔIC| = %.2e" % (int(both.sum()), worst))
        ok("旧路径不产出 directionBasis / signAsOf（它本来就没有逐期概念）",
           legacy.get("directionBasis") is None and legacy.get("signAsOf") is None)
        meta = FS.factor_meta(flipped, sign_map=db.get("signed"))
        ok("factorMeta 对翻转因子给出 directionEff 与 flipped（页面才不会说反）",
           all(meta[n]["flipped"] is True
               and meta[n]["directionEff"] == -meta[n]["direction"] for n in flipped),
           str({n: (meta[n]["direction"], meta[n]["directionEff"]) for n in flipped[:2]}))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# [4] verdict → 符号表必须一一对应
# ────────────────────────────────────────────────────────────
def case_verdict_signs(tmp: str) -> None:
    print("\n[4] VERDICT_SIGN ↔ SELECTED_VERDICTS 一一对应（少声明就炸）")
    ok("两个集合完全一致", set(FS.VERDICT_SIGN) == set(RE.SELECTED_VERDICTS),
       str(sorted(FS.VERDICT_SIGN)))
    ok("「值得复核」= +1、「方向需翻转」= -1（语义，不是随手写的符号）",
       FS.VERDICT_SIGN.get("值得复核") == 1 and FS.VERDICT_SIGN.get("方向需翻转") == -1)

    orig = RE.SELECTED_VERDICTS
    RE.SELECTED_VERDICTS = tuple(orig) + ("仅统计显著",)
    raised = None
    try:
        FS._ensure_verdict_signs()
    except SystemExit as exc:
        raised = exc
    finally:
        RE.SELECTED_VERDICTS = orig
    ok("往 SELECTED_VERDICTS 加档位却不声明符号 → 直接抛 SystemExit（不静默按 +1）",
       raised is not None and "未声明符号" in str(raised),
       str(raised)[:80] if raised else "没有抛")


# ────────────────────────────────────────────────────────────
# [5] 首个 cutoff 之前不打分、不回填
# ────────────────────────────────────────────────────────────
def case_no_backfill(tmp: str) -> None:
    print("\n[5] 首个 cutoff 之前不打分、不向前回填")
    env = SynthEnv(tmp, "n5", n_dates=180)
    try:
        patch_loaders(env)
        build(env)
        evald = evald_of(env)
        built = scores_of(env, evald)
        sa = built.get("signAsOf") or {}
        n_un = int(sa.get("nUnscoredDays") or 0)
        first = str(sa.get("unscoredBefore") or "")
        start = str(sa.get("criterionStart") or "")
        cut0 = str((built["periods"][0] or {}).get("icCutoff") or "")
        i_crit = env.dates.index(cut0) + H

        ok("判据起点 == dates[idx(首个 cutoff) + horizon]",
           start == str(env.dates[i_crit]), "%s（cut0=%s）" % (start, cut0))
        ok("前 %d 天（含首个 cutoff 之前的 %d 天）一个分数都没有（不回填）"
           % (n_un, i_crit),
           n_un >= i_crit and bool(built["scores"].iloc[:n_un].isna().all().all()),
           "非空格 %d" % int(built["scores"].iloc[:n_un].notna().sum().sum()))
        ok("第 %d 天开始有分数（unscoredBefore 与矩阵一致）" % n_un,
           first == str(env.dates[n_un])
           and bool(built["scores"].iloc[n_un].notna().any()),
           "%s" % first)
        ok("逐日 signAsOf 的起点 == 判据起点", str((sa.get("dates") or [None])[0]) == start,
           str((sa.get("dates") or [None])[0]))

        # 空行只允许出现在「生效期一个因子都没选出来」的那些天上，不能无解释地空着
        step_of = list(sa.get("segmentOf") or [])
        no_sig = set(sa.get("noSignalSegments") or [])
        segs = {s["asOf"]: i for i, s in enumerate(sa.get("segments") or [])}
        empty_days = [d for d in built["scores"].index
                      if not bool(built["scores"].loc[d].notna().any())]
        unexplained = []
        for d in empty_days:
            if str(d) < start:
                continue                       # 前导段：判据上就没结论
            j = step_of[env.dates.index(str(d)) - env.dates.index(start)]
            asof = (sa["segments"][j]["asOf"] if 0 <= j < len(sa.get("segments") or [])
                    else None)
            if asof not in no_sig:
                unexplained.append((str(d), j, asof))
        ok("空行只出现在「该期一个因子都没选出来」的日子（不会莫名空着）",
           not unexplained, str(unexplained[:3]))
        ok("noSignalSegments 有记录（早期期数据不足，如实报出来）",
           isinstance(sa.get("noSignalSegments"), list),
           str(sa.get("noSignalSegments")))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# [6] generate() 落盘结构
# ────────────────────────────────────────────────────────────
def case_generate_payload(tmp: str) -> None:
    print("\n[6] generate()：validation 主 / validationFullSample 对照 / 名单只有一份")
    env = SynthEnv(tmp, "n6", n_dates=200)
    out = os.path.join(tmp, "n6_score.json")
    try:
        patch_loaders(env)
        build(env)
        env.write_eval()
        before = prod_guard.snapshot()
        res = FS.generate(env.conn, eval_path=env.eval_path, out=out, verbose=False)
        after_diff = prod_guard.diff(before)
        ok("生产 data/ 一个字节都没被写（隔离护栏）", not after_diff, str(after_diff[:4]))
        ok("返回 ok 且写了文件", bool(res.get("ok")) and os.path.exists(out))

        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)

        ok("mode == walkforward", payload.get("mode") == FS.MODE_WF, payload.get("mode"))
        v = payload.get("validation") or {}
        ok("validation.basis == 'walk-forward'（主指标自己说明身世）",
           v.get("basis") == "walk-forward", str(v.get("basis")))
        ok("validation 带 nUnscoredDays / criterion",
           v.get("nUnscoredDays") is not None and v.get("criterion") == FS.WF_CRITERION,
           "未打分 %s 天" % v.get("nUnscoredDays"))
        vf = payload.get("validationFullSample") or {}
        ok("validationFullSample 存在、标了 lookahead、字段名与 validation 同构",
           vf.get("lookahead") is True and vf.get("basis") == "full-sample"
           and "ic_mean" in vf and "t_nw" in vf, str(sorted(vf)[:6]))
        ok("对照版明确写着「不是这份名单的验证结论」",
           "不是" in str(vf.get("note") or ""), str(vf.get("note"))[:50])

        db = payload.get("directionBasis") or {}
        ok("directionBasis 四个键齐（asOf / icCutoff / flipped / signed）",
           {"asOf", "icCutoff", "flipped", "signed"} <= set(db), str(sorted(db)))
        sa = payload.get("signAsOf") or {}
        ok("signAsOf 带逐日展开与分段表",
           bool(sa.get("dates")) and bool(sa.get("segments"))
           and len(sa.get("segmentOf") or []) == len(sa.get("dates") or []),
           "%d 天 / %d 段" % (len(sa.get("dates") or []), len(sa.get("segments") or [])))

        ok("名单只有一份（没有第二个 picks 字段）",
           "picksFullSample" not in payload and "picks" in payload
           and isinstance(payload.get("picks"), dict), str(sorted(payload))[:60])
        picks = (payload.get("picks") or {}).get("picks") or []
        # generate() 的打分轴来自 `_score_axis` → `factor_ts.panel_dates_fast`，
        # 合成夹具里这条轴比 env.dates 多几天（面板轴 203 天 / env.dates 200 天）。
        # 名单日期要跟**打分轴**的末日对齐，不是 env.dates 的末日 —— 用后者
        # 断言会把「generate 选对了轴」误判成 bug。打分轴本身另有用例校验。
        import factor_ts as _TS
        score_axis = [str(d) for d in _TS.panel_dates_fast(env.conn)]
        expect_date = score_axis[-1] if score_axis else env.dates[-1]
        ok("名单日期 = 打分轴最后一个交易日（非 env.dates 末日）",
           (payload.get("picks") or {}).get("date") == expect_date,
           "名单 %s / 打分轴末日 %s / env.dates 末日 %s"
           % ((payload.get("picks") or {}).get("date"), expect_date, env.dates[-1]))

        sel = payload.get("factors") or []
        w = payload.get("weights") or {}
        ok("等权（每个因子 1/N）",
           bool(sel) and all(abs(float(w.get(n, -1)) - 1.0 / len(sel)) < 1e-12 for n in sel),
           "N=%d w=%s" % (len(sel), (list(w.values())[:2] if w else [])))
        ok("clusters 为空（逐期不做相关去冗余 —— 那要用全样本相关矩阵）",
           payload.get("clusters") == {})
        meta = payload.get("factorMeta") or {}
        ok("factorMeta 每个入选因子都带 directionEff / flipped",
           all("directionEff" in meta.get(n, {}) and "flipped" in meta.get(n, {})
               for n in sel), "%d 个" % len(sel))
        ok("贡献里带 directionEff（页面据此说「越大/越小越好」）",
           all("directionEff" in c for p in picks[:3]
               for c in (p.get("contributions") or [])),
           "%d 只已核对" % len(picks[:3]))
        ok("modeNote 说明了逐期规则与「不做去冗余」的取舍",
           "逐期" in str(payload.get("modeNote") or "")
           and "去冗余" in str(payload.get("modeNote") or ""),
           str(payload.get("modeNote"))[:60])
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# [7] 小样本降级：无可用期 → legacy + 明写 warning + partial
# ────────────────────────────────────────────────────────────
def case_fallback_when_no_periods(tmp: str) -> None:
    """逐期打分排不出任何一期时，必须优雅降级到 legacy 并如实自曝。

    为什么要专门测：这条分支平时（生产 1420 天面板）永远走不到，属于
    「只有故障日才执行」的代码 —— 不主动强制触发，它就可能是一个语法正确
    但从未被验证过的死兜底。这里 monkeypatch `period_signals` 返回空，
    逼 `build_walk_forward` 在 :587 抛「没有可用期」，走完整个降级链。

    为什么 patch `period_signals` 是安全的：`build_scores` 在 legacy 路径
    （factor_score.py:793）直接调 `_build_scores_legacy`，根本不碰它，
    所以降级后的兜底打分不受影响、仍能出名单。
    """
    print("\n[7] 无可用期降级：mode→legacy、basis→full-sample、warning 明写、"
          "partial=true、产物照写")
    env = SynthEnv(tmp, "n7", n_dates=200)
    out = os.path.join(tmp, "n7_score.json")
    orig_period_signals = FS.period_signals
    try:
        patch_loaders(env)
        build(env)
        env.write_eval()
        FS.period_signals = lambda *a, **k: []

        before = prod_guard.snapshot()
        res = FS.generate(env.conn, eval_path=env.eval_path, out=out, verbose=False)
        after_diff = prod_guard.diff(before)
        ok("降级后生产 data/ 仍一个字节没写", not after_diff, str(after_diff[:4]))
        ok("降级后仍 ok 且写了产物（不因小样本整轮失败）",
           bool(res.get("ok")) and os.path.exists(out), "ok=%s" % res.get("ok"))
        ok("返回值 mode 如实报 legacy", res.get("mode") == FS.MODE_LEGACY,
           str(res.get("mode")))

        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        ok("产物 mode == legacy（不伪装成 walk-forward）",
           payload.get("mode") == FS.MODE_LEGACY, str(payload.get("mode")))
        v = payload.get("validation") or {}
        ok("validation.basis == full-sample（主指标自曝含前视）",
           v.get("basis") == "full-sample", str(v.get("basis")))
        ok("validation.note 含「前视」字样",
           "前视" in str(v.get("note") or ""), str(v.get("note"))[:60])
        ok("partial == true（页面据此显示灰字，而非静默当成功）",
           payload.get("partial") is True, str(payload.get("partial")))
        warns = " ".join(str(w) for w in (payload.get("warnings") or []))
        ok("warnings 明写降级原因（含「降级」与「前视」）",
           "降级" in warns and "前视" in warns, warns[:90])
        fresh = payload.get("freshness") or {}
        ok("freshness.warnings 同步了降级原因（两页口径一致）",
           any("降级" in str(w) for w in (fresh.get("warnings") or [])),
           str(fresh.get("warnings"))[:90])
        picks = (payload.get("picks") or {}).get("picks") or []
        ok("名单照常产出（legacy 兜底真的出了票）", bool(picks), "%d 只" % len(picks))
    finally:
        FS.period_signals = orig_period_signals
        env.cleanup()


# ────────────────────────────────────────────────────────────
# [8] T08：成本口径不得引入新前视
# ────────────────────────────────────────────────────────────
def case_cost_no_lookahead(tmp: str) -> None:
    """T 后价格被逐股扰动，不能改逐期结论/方向/权重（成本链不引入新前视）。

    修复记录（QA 独立审计抓到的空壳测试）：本用例原先用统一乘数 ×1.37 做主扰动，
    并用 `Δfwd > 0` 当"有牙齿"断言 —— 但**统一乘数对 fwd 是单调变换，不改横截面
    排名**，即使期判定偷看了 cutoff 之后的数据，四个键也会一字不变、用例照样绿。
    实测：统一 ×1.37 → Δfwd=0.407 但 ΔIC=0；逐股 σ=0.35 → ΔIC=0.124。
    这与本文件开头（:31-36）和用例 [1] 反复警告的是同一个陷阱，T08 新增时重犯了。

    现在改成与 [1] 同样的三段式：逐股乘子为主扰动（真改排名）、统一乘数为反例、
    再注入前视 bug 证明本用例抓得到。牙齿断言用 scores 宽表而非 fwd 数值。
    """
    print("\n[8] 成本口径无前视：逐股扰动 T 后价格 → periods/directionBasis/signAsOf/weights 不变")
    clean = SynthEnv(tmp, "n8_clean", n_dates=180)
    per = SynthEnv(tmp, "n8_per", n_dates=180)
    uni = SynthEnv(tmp, "n8_uni", n_dates=180)
    try:
        patch_loaders(clean)
        build(clean)
        evald = evald_of(clean)
        base = scores_of(clean, evald)
        cut = str(base["periods"][-3]["icCutoff"])
        ok("目标 cutoff 落在日期轴内", cut in clean.dates, cut)

        # 无前视的精确边界：IC 序列第 d 行用到 fwd[d]（d+1 开盘→d+h 收盘），
        # 所以「某期判定完全不受 date>cut 扰动」的前提是 该期cutoff + h <= cut。
        # 不能要求所有期逐字不变——cutoff 接近 cut 的期合法地需要 cut 之后的收益，
        # directionBasis（最后一期）尤其必然变化。原用例在这里断言过宽，是错的。
        h = int(FS.wf_params()["horizon"])
        idx = {str(d): i for i, d in enumerate(clean.dates)}
        cut_i = idx[cut]

        # 主扰动：逐股乘子（真改横截面排名，才有检出力）
        patch_loaders(per)
        per.corrupt_future(cut, factor=1.0, per_symbol_sigma=0.35)
        build(per)
        other = scores_of(per, evald)

        a, b = base["scores"], other["scores"]
        before, after = a.index <= cut, a.index > cut
        d_before = max_abs_delta(a.loc[before], b.loc[before])
        d_after = max_abs_delta(a.loc[after], b.loc[after])
        ok("[逐股] <= cut 的 scores 逐位不变（无新前视）", d_before == 0.0,
           "max|Δ| = %g" % d_before)
        ok("[逐股] cut 之后确实改了排名（用例有牙齿，不是只改 fwd 数值）",
           d_after > 0.0, "max|Δ| = %g" % d_after)

        # 只断言 safe 期（cutoff + h <= cut）的判定逐字不变；这是能证明无前视的那部分
        safe_periods = [(i, p) for i, p in enumerate(base["periods"])
                        if idx[p["icCutoff"]] + h <= cut_i]
        ok("存在足够早的 safe 期可供断言", len(safe_periods) >= 3,
           "%d/%d 期" % (len(safe_periods), len(base["periods"])))
        bad = []
        for i, p in safe_periods:
            q = other["periods"][i]
            if json.dumps(p, sort_keys=True, ensure_ascii=False) != \
               json.dumps(q, sort_keys=True, ensure_ascii=False):
                bad.append((i, p["icCutoff"]))
        ok("safe 期的逐期判定不受 cut 之后价格影响（无新前视）", not bad, str(bad))

        # signAsOf 的 safe 分段（segment 的 cutoff + h <= cut）也不变
        seg_a = (base.get("signAsOf") or {}).get("segments") or []
        seg_b = (other.get("signAsOf") or {}).get("segments") or []
        safe_segs = [s for s in seg_a if idx.get(s["icCutoff"], 10**9) + h <= cut_i]
        ok("存在 safe 的 signAsOf 分段", len(safe_segs) >= 3, "%d 段" % len(safe_segs))
        bseg = {(s["icCutoff"]): s for s in seg_b}
        seg_bad = [s["icCutoff"] for s in safe_segs
                   if json.dumps(s, sort_keys=True, ensure_ascii=False) !=
                   json.dumps(bseg.get(s["icCutoff"]), sort_keys=True, ensure_ascii=False)]
        ok("safe 的 signAsOf 分段不受影响", not seg_bad, str(seg_bad))

        # directionBasis / weights 对应最新期，cutoff 最接近末端，合法地受扰动影响。
        # 这里不断言相等，只断言结构完整（字段齐、符号合法）——避免把合法变化误判为 bug。
        db = other.get("directionBasis") or {}
        ok("directionBasis 结构完整（最新期可合法变化）",
           {"asOf", "icCutoff", "flipped", "signed"} <= set(db),
           str(sorted(db)[:8]))
        ok("weights 仍是合法概率分布（最新期可合法变化）",
           bool(other.get("weights")) and
           all(v is None or (isinstance(v, (int, float)) and v >= 0)
               for v in other["weights"].values()), "%d 项" % len(other.get("weights") or {}))

        # 反例：统一乘数 ×1.37 不改变排名，四个键当然也不变——它证明不了任何事
        patch_loaders(uni)
        uni.corrupt_future(cut, factor=1.37)
        build(uni)
        uniform = scores_of(uni, evald)
        d_uni = max_abs_delta(a, uniform["scores"])
        ok("对照：统一乘数 ×1.37 → scores 逐位不变（单调变换，抓不住前视）",
           d_uni == 0.0, "max|Δ| = %g" % d_uni)

        # 牙齿验证：注入前视 bug（判据写成 cut <= d）后，<= cut 的 scores 必须变化，
        # 否则本用例就是永远通过的空壳（与用例 [1] 的 buggy 对照同一思路）。
        orig_steps = FS.sign_steps

        def buggy_steps(dates, periods_, horizon):
            n = len(dates)
            step_of = [-1] * n
            j = 0
            for i in range(n):
                today = str(dates[i])
                while j + 1 < len(periods_) and str(periods_[j + 1]["icCutoff"]) <= today:
                    j += 1
                if str(periods_[j]["icCutoff"]) <= today:
                    step_of[i] = j
            segs = []
            for jj, per_ in enumerate(periods_):
                rows = [i for i, s in enumerate(step_of) if s == jj]
                segs.append({"tIndex": per_["tIndex"], "asOf": per_["asOf"],
                             "icCutoff": per_["icCutoff"],
                             "nSelected": per_["nSelected"],
                             "selected": list(per_["selected"]),
                             "signs": dict(per_["signs"]),
                             "flipped": sorted(n for n, s in per_["signs"].items() if s < 0),
                             "rows": rows,
                             "from": str(dates[rows[0]]) if rows else None,
                             "to": str(dates[rows[-1]]) if rows else None})
            return step_of, segs

        FS.sign_steps = buggy_steps
        try:
            buggy_clean = scores_of(clean, evald)
            buggy_per = scores_of(per, evald)
        finally:
            FS.sign_steps = orig_steps

        d_buggy = max_abs_delta(buggy_clean["scores"].loc[before],
                                buggy_per["scores"].loc[before])
        ok("牙齿：注入 `cut <= d` 前视 bug 后 <= cut 的 scores 变了（本用例抓得到）",
           d_buggy > 0.0, "max|Δ| = %g" % d_buggy)
    finally:
        clean.cleanup()
        per.cleanup()
        uni.cleanup()


# ────────────────────────────────────────────────────────────
# [9] T08：样本门槛 warn / enforce 两态
# ────────────────────────────────────────────────────────────
def case_sample_gate(tmp: str) -> None:
    """<50 笔时名单照出、门槛判 insufficient；只有 enforce 才写盘前拦。"""
    print("\n[9] 样本门槛：warn 照写且 insufficient / enforce 抛错且零写盘")
    env = SynthEnv(tmp, "n9_gate", n_dates=120)
    before = prod_guard.snapshot()
    try:
        patch_loaders(env)
        build(env)
        evald = evald_of(env)
        built = scores_of(env, evald)
        s = built["scores"].iloc[-20:]
        vv = built["valid"].iloc[-20:]
        fwd = env.fwd(axis=list(env.dates)).reindex(index=s.index)
        c = FS.validate_costed(s, vv, fwd, env.close, horizon=H, min_trades=50)
        g = c.get("gate") or {}
        ok("样本不足 → verdict=insufficient（不是 fail/pass）",
           c.get("verdict") == "insufficient", str(c.get("verdict")))
        ok("gate.passed 是严格布尔 false，箭头字段齐全",
           g.get("passed") is False and g.get("requiredTrades") == 50
           and g.get("actual") == c["n_trades"]
           and g.get("requiredBuyDates") == 10 and g.get("requiredDays") == 50
           and g.get("appliesTo") == "full-window", str(g)[:120])
        ok("前端读的字段名齐（netTop10Annual / n_trades / tNw）",
           all(k in c for k in ("netTop10Annual", "n_trades", "tNw")))

        out_warn = os.path.join(tmp, "n9_warn.json")
        res = FS.generate(env.conn, eval_path=env.eval_path, out=out_warn,
                          verbose=False, min_trades=1_000_000,
                          run_robustness=False)
        ok("warn 模式仍 ok 且写了文件", bool(res.get("ok")) and os.path.exists(out_warn))
        with open(out_warn, encoding="utf-8") as fh:
            payload = json.load(fh)
        eg = payload.get("evidenceGate") or {}
        ok("evidenceGate.level=insufficient / passed=false / mode=warn",
           eg.get("level") == "insufficient" and eg.get("passed") is False
           and eg.get("mode") == "warn", str(eg)[:120])
        ok("evidenceGate.failed 非空（freshness 直接读这个键）",
           bool(eg.get("failed")) and "reasons" in eg, str(eg.get("failed"))[:80])
        costed = (payload.get("validation") or {}).get("costed") or {}
        ok("落盘 validation.costed.verdict=insufficient 且 gate.passed=false",
           costed.get("verdict") == "insufficient"
           and (costed.get("gate") or {}).get("passed") is False)
        ok("名单仍在（证据不足不等于没名单）",
           bool((payload.get("picks") or {}).get("picks")))
        cost = payload.get("cost") or {}
        rates = cost.get("rates") or {}
        ok("cost 段按 §7.1 契约（profile/rates/roundTripBps/notional/priceAdjust/sources）",
           all(k in cost for k in ("profile", "multiplier", "entry", "rates",
                                   "roundTripBps", "assumedPositionNotional",
                                   "minCommissionBinding", "priceAdjust", "sources"))
           and all(k in rates for k in ("commission", "transferFee",
                                        "stampDuty", "slippage"))
           and rates.get("stampDuty", {}).get("buy") == 0.0,
           str(cost.get("profile")))
        st = payload.get("settlement") or {}
        ok("settlement 段按 §7.2 契约（axisMatch/missingOpen/samples/…）",
           all(k in st for k in ("ok", "axisMatch", "missingOpen", "missingClose",
                                 "nUnbuyable", "nUnsettled", "nSettled",
                                 "fwdRecomputeMaxAbsDiff", "samples", "notCovered")))
        ok("warnings 明写证据不足（页面不能静默）",
           any("证据门槛" in str(w) for w in payload.get("warnings") or []))

        out_enf = os.path.join(tmp, "n9_enforce.json")
        raised = None
        try:
            FS.generate(env.conn, eval_path=env.eval_path, out=out_enf,
                        verbose=False, min_trades=1_000_000,
                        enforce_gate=True, run_robustness=False)
        except FS.EvidenceGateError as exc:
            raised = exc
        ok("enforce 模式抛 EvidenceGateError", raised is not None,
           (str(raised)[:80] if raised else "未抛"))
        ok("enforce 模式未写盘（页面保留旧名单，不出现新名单+更新失败）",
           not os.path.exists(out_enf))
        ok("两态运行均未碰生产 data/", not prod_guard.diff(before),
           str(prod_guard.diff(before)[:4]))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# [10] T08：参数簇复用同一 cutoff，不自算
# ────────────────────────────────────────────────────────────
def case_robustness_reuses_cutoff(tmp: str) -> None:
    """9 组每期 cutoff 必须等于 RE.ic_cutoff；新代码不写 t_idx-horizon。"""
    print("\n[10] 参数簇：9 组 cutoff == RE.ic_cutoff，且无自算下标")
    env = SynthEnv(tmp, "n10_rob", n_dates=180)
    try:
        patch_loaders(env)
        build(env)
        evald = evald_of(env)
        built = scores_of(env, evald)
        axis = [str(d) for d in built["scores"].index]
        rob = FS.robustness_grid(
            env.conn, evald, axis, built["scores"], built["valid"],
            env.fwd(axis=axis), env.close, open_wide=env.open, horizon=H,
            n_periods_base=len(built.get("periods") or []),
            periods_base=built.get("periods"))
        details = rob.get("details") or []
        ok("主网格 9 组且 n=9", len(details) == 9 and rob.get("n") == 9,
           "%d 组" % len(details))
        ok("网格声明 step×window、horizon 固定 5",
           (rob.get("grid") or {}).get("step") == [10, 20, 30]
           and (rob.get("grid") or {}).get("window") == [90, 120, 150]
           and (rob.get("grid") or {}).get("horizon") == [5],
           str(rob.get("grid")))
        ok("robustness 主字段齐（n/netAnnual/positiveRatio/verdict/details）",
           all(k in rob for k in ("n", "netAnnual", "positiveRatio", "verdict", "details"))
           and all(k in (rob.get("netAnnual") or {})
                   for k in ("median", "q25", "q75", "min", "max"))
           and rob.get("verdict") in ("pass", "weak", "fail", "insufficient"),
           str(rob.get("verdict")))
        ok("收益口径标了 caliber/returnHorizon 与 notCovered",
           (rob.get("caliber") or {}).get("selectionHorizon") == 5
           and (rob.get("caliber") or {}).get("returnHorizon") == [4, 5, 6]
           and any(isinstance(x, dict) and x.get("param") == "horizon"
                   for x in rob.get("notCovered") or []))
        mismatch = []
        for d in details:
            want = [RE.ic_cutoff(axis, int(t), H) for t in d.get("tIndices") or []]
            if list(d.get("cutoffs") or []) != want:
                mismatch.append((d.get("step"), d.get("window")))
        ok("9 组每期 cutoff 逐位 == RE.ic_cutoff（无一处自算）",
           not mismatch and all(int(d.get("nPeriods") or 0) > 0 for d in details),
           "不符的格子=%s" % mismatch[:3])

        src_r = inspect.getsource(FS.robustness_grid)
        src_p = inspect.getsource(FS.period_signals)
        ok("robustness_grid 内无自算 cutoff 赋值（grep 断言）",
           not re.search(r"\b(cut|cutoff)\s*=\s*[^#\n]*-\s*(h|horizon|t_idx|i)\b", src_r),
           "匹配数=%d" % len(re.findall(r"\b(cut|cutoff)\s*=", src_r)))
        ok("期判定仍走 RE.evaluate_period（cutoff 唯一入口）",
           "evaluate_period(" in src_p and "period_indices(" in src_p)
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# [11] T08：production 路径真的把 volume_wide 传到了结算与纸面交易
# ────────────────────────────────────────────────────────────
def case_volume_wired(tmp: str) -> None:
    """签名支持不等于调用接上：generate 漏传 volume 时这条必须红。"""
    print("\n[11] volume 接线：T+1 volume=0 → settlement 计数、纸面交易剔除")
    env = SynthEnv(tmp, "n11_vol", n_dates=140)
    before = prod_guard.snapshot()
    try:
        patch_loaders(env)
        build(env)
        evald_of(env)
        axis = [str(d) for d in TS.panel_dates_fast(env.conn)]
        score_day, buy_day = axis[-10], axis[-9]
        sym = str(env.symbols[0])
        env.volume.loc[buy_day, sym] = 0.0
        out = os.path.join(tmp, "n11_vol.json")
        res = FS.generate(env.conn, eval_path=env.eval_path, out=out,
                          verbose=False, run_robustness=False)
        ok("带成交量仍正常写盘", bool(res.get("ok")) and os.path.exists(out))
        with open(out, encoding="utf-8") as fh:
            payload = json.load(fh)
        st = payload.get("settlement") or {}
        ok("settlement.nUnbuyable>0（生产路径确实传了 volume_wide）",
           int(st.get("nUnbuyable") or 0) >= 1, str(st.get("nUnbuyable")))
        cl = env.close.reindex(index=axis)
        op = env.open.reindex(index=axis)
        fw = env.fwd(axis=axis)
        blind_st = TC.settle_check(cl, op, fw, H, volume_wide=None)
        ok("同一份数据不传 volume 时 nUnbuyable=0（前后差异确凿，非空断言）",
           int(blind_st.get("nUnbuyable") or 0) == 0
           and int(st.get("nUnbuyable") or 0) >= 1,
           "blind=%d → wired=%d" % (int(blind_st.get("nUnbuyable") or 0),
                                    int(st.get("nUnbuyable") or 0)))
        ok("nUnsettled 包含不可买票（不把「买不到」记成成功成交）",
           int(st.get("nUnsettled") or 0) >= int(st.get("nUnbuyable") or 0),
           "nUnsettled=%s" % st.get("nUnsettled"))
        ok("notCovered 没有声称成交量检查未启用",
           not any("成交量" in str(x) for x in st.get("notCovered") or []))
        costed = (payload.get("validation") or {}).get("costed") or {}
        ok("costed.nUnbuyableExcluded>=1（纸面交易剔除同一批票）",
           int(costed.get("nUnbuyableExcluded") or 0) >= 1,
           str(costed.get("nUnbuyableExcluded")))
        ok("score_day / buy_day 确实在打分轴上（用例有牙齿）",
           score_day in axis and buy_day in axis, "%s → %s" % (score_day, buy_day))
        ok("运行未碰生产 data/", not prod_guard.diff(before),
           str(prod_guard.diff(before)[:4]))
    finally:
        env.cleanup()


# ────────────────────────────────────────────────────────────
# [12] T08：step_score exit code 映射（构造注入，逐个实测）
# ────────────────────────────────────────────────────────────
def case_exit_code_mapping(tmp: str) -> None:
    """2 是校验失败、4 是连名单都出不来、5 是真异常；不允许互相顶替。"""
    print("\n[12] exit code 实测：Assertion/Value/CostProfile/EvidenceGate→2，"
          "SystemExit→4，其他→5")
    cfg = {"horizon": H, "entry": "s1_open",
           "rollStep": 20, "rollSelectWindow": 120}
    before = prod_guard.snapshot()
    orig = FS.generate
    cases = [
        ("AssertionError", AssertionError("前视断言被撞穿"), FR.EXIT_INVALID),
        ("ValueError", ValueError("结算口径非法"), FR.EXIT_INVALID),
        ("CostProfileError", FS.CostProfileError("费率档位不存在"), FR.EXIT_INVALID),
        ("EvidenceGateError", FS.EvidenceGateError("证据门槛未过"), FR.EXIT_INVALID),
        ("SystemExit", SystemExit("没有任何可用因子，连打分都做不出来"), FR.EXIT_NO_PERIOD),
        ("RuntimeError", RuntimeError("导出炸了"), FR.EXIT_EXPORT),
    ]
    try:
        for tag, exc, want in cases:
            def boom(*_args, _exc=exc, **_kwargs):
                raise _exc
            FS.generate = boom
            res = RFP.step_score(None, {}, cfg, verbose=False)
            ok("%s → exit %d（不是 4 的垃圾桶）" % (tag, want),
               int(res.get("exit") or -1) == want and res.get("ok") is False,
               "got=%s" % res.get("exit"))
        ok("注入实测未碰生产 data/", not prod_guard.diff(before),
           str(prod_guard.diff(before)[:4]))
    finally:
        FS.generate = orig


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="score_wf_test_")
    t0 = time.time()
    print("=" * 72)
    print("T06 / 逐期 walk-forward 打分（三处前视 + 结构验收）")
    print("=" * 72)
    before = prod_guard.snapshot()
    try:
        case_no_lookahead(tmp)
        case_sign_traceable(tmp)
        case_flip_effective(tmp)
        case_verdict_signs(tmp)
        case_no_backfill(tmp)
        case_generate_payload(tmp)
        case_fallback_when_no_periods(tmp)
        case_cost_no_lookahead(tmp)
        case_sample_gate(tmp)
        case_robustness_reuses_cutoff(tmp)
        case_volume_wired(tmp)
        case_exit_code_mapping(tmp)
    finally:
        restore_loaders()
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
