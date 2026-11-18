#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/test_factors.py — 因子基础设施的确定性测试（不联网、不依赖真实数据）

四组断言，针对的是「因子基础设施最容易埋雷的地方」：
  1. 前视偏差负例 —— 把最后 50 天改成极端值，前 250 天的因子值必须一字不变。
     这条是本任务最重要的测试：只要计算里出现 shift(-n)/iloc[i+1]/bfill，
     未来信息就会渗透到历史，回测会好看得离谱而实盘一塌糊涂。
  2. IC 正确性 —— 人为造出「因子值与未来收益完全单调」的数据，rank_ic 必须 ≈ 1。
     防止方向算反、rank 实现写错、合并键错位这类逻辑错误。
  3. 幂等性 —— 同一批数据写两次面板，行数不变。
  4. 分组单调性 —— 单调因子必须给出单调的分组收益。

全部用确定性随机数（固定 seed），可重复运行。

运行：
    python tests/test_factors.py
"""
import os
import sqlite3
import sys
import tempfile

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

from factors import all_names, compute_one, compute_series, registry  # noqa: E402
import factor_eval as FE                                          # noqa: E402
import factor_panel as FP                                         # noqa: E402

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


def make_dates(n, start="2023-01-03"):
    """生成 n 个连续「交易日」（用工作日近似，不需要真实日历）。"""
    return [d.strftime("%Y-%m-%d") for d in pd.bdate_range(start=start, periods=n)]


def make_ohlcv(n=300, seed=7, symbol="TEST01"):
    """造一只票的行情：几何随机游走 + 温和放量，保证各因子都能算出值。"""
    rng = np.random.default_rng(seed)
    close = 10.0 * np.exp(np.cumsum(rng.normal(0.0005, 0.02, n)))
    open_ = close * (1 + rng.normal(0, 0.005, n))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, n)))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, n)))
    volume = np.abs(rng.normal(2e6, 3e5, n)) + 1e5
    turnover = volume * close
    return pd.DataFrame({
        "symbol": symbol,
        "date": make_dates(n),
        "open": open_, "high": high, "low": low, "close": close,
        "volume": volume, "turnover": turnover,
    })


# ────────────────────────────────────────────────────────────
print("=== 1. 前视偏差负例（改未来 50 天，前 250 天因子值必须不变）===")
df = make_ohlcv(300, seed=11)
base = compute_series(df)
ok("因子数 >= 20", base.shape[1] >= 20, "实际 %d 个" % base.shape[1])
ok("覆盖全部分类", len({d["category"] for d in registry.FACTOR_DEFS}) >= 8,
   "%d 类" % len({d["category"] for d in registry.FACTOR_DEFS}))

# 把最后 50 天改成极端值（价格 ×5、成交量 ×10）
tampered = df.copy()
tail = tampered.index[-50:]
for col in ("open", "high", "low", "close"):
    tampered.loc[tail, col] = tampered.loc[tail, col] * 5.0
tampered.loc[tail, "volume"] = tampered.loc[tail, "volume"] * 10.0
tampered.loc[tail, "turnover"] = (tampered.loc[tail, "volume"]
                                  * tampered.loc[tail, "close"])
after = compute_series(tampered)

head_a = base.iloc[:250].fillna(-12345.678)
head_b = after.iloc[:250].fillna(-12345.678)
same = head_a.equals(head_b)
ok("前 250 天因子值完全不变", same,
   "" if same else "变化列：%s" % [c for c in base.columns
                                  if not head_a[c].equals(head_b[c])][:8])

# 反证：尾部必须发生变化，否则说明测试是空的（改动没进入计算范围）
tail_a = base.iloc[250:].fillna(-12345.678)
tail_b = after.iloc[250:].fillna(-12345.678)
changed_cols = [c for c in base.columns if not tail_a[c].equals(tail_b[c])]
ok("尾部因子值确实发生变化（反证测试有效）", len(changed_cols) >= 10,
   "%d 个因子受影响" % len(changed_cols))

# 静态扫描：源码层面也不允许出现偷看未来的写法
src_path = os.path.join(HERE, "factors", "compute.py")
src = open(src_path, "r", encoding="utf-8").read()
banned = [("shift(-", "负向移位"), (".bfill(", "向后填充"), ("fillna(method=", "旧式填充")]
hits = [desc for token, desc in banned if token in src]
ok("compute.py 无 shift(-n)/bfill/fillna(method=)", not hits, "命中：%s" % hits)

# ────────────────────────────────────────────────────────────
print("\n=== 2. IC 正确性（因子值与未来 5 日收益完全单调 → rank_ic ≈ 1）===")
N_SYM, N_DAY = 80, 60
dates = make_dates(N_DAY)
rng = np.random.default_rng(2024)
panel = {}
fwd = {}
for s in range(N_SYM):
    sym = "S%03d" % s
    # 因子值 = 股票序号 + 微噪声（横截面单调）
    panel[sym] = [s + rng.normal(0, 0.01) for _ in dates]
    # 未来 5 日收益也随股票序号单调递增（+ 微噪声），因此 Spearman ≈ 1
    fwd[sym] = [s * 1e-4 + rng.normal(0, 1e-6) for _ in dates]
panel_wide = pd.DataFrame(panel, index=dates)
fwd_wide = pd.DataFrame(fwd, index=dates)

ic = FE.daily_ic(panel_wide, fwd_wide, min_stocks=30)
ok("每日都算出 IC", len(ic) == N_DAY, "有效天 %d/%d" % (len(ic), N_DAY))
ok("rank_ic 均值 > 0.95（≈1）", float(ic.mean()) > 0.95, "IC=%.6f" % float(ic.mean()))

# 反向构造：因子与收益完全负相关 → IC ≈ -1（防止方向写反）
neg = FE.daily_ic(panel_wide, -fwd_wide, min_stocks=30)
ok("反向数据 IC < -0.95", float(neg.mean()) < -0.95, "IC=%.6f" % float(neg.mean()))

# 样本不足的日子必须被跳过
few = FE.daily_ic(panel_wide.iloc[:, :10], fwd_wide.iloc[:, :10], min_stocks=30)
ok("样本 < 30 的日期被跳过", len(few) == 0, "有效天 %d" % len(few))

# 手算核对：两列小样本 Spearman
# x 秩 = 1..8，y = [2,1,4,3,8,7,6,5]，d = x-y 的平方和 = 24
# ρ = 1 - 6*24/(8*63) = 0.7142857…
x = [1, 2, 3, 4, 5, 6, 7, 8]
y = [2.0, 1.0, 4.0, 3.0, 8.0, 7.0, 6.0, 5.0]
manual = FE.spearman(x, y)
ok("spearman 手算核对(0.714286)", abs(manual - 0.7142857142857143) < 1e-9,
   "得到 %.6f" % manual)

# BH-FDR 手算核对：p = [0.001, 0.02, 0.5] → q = [0.003, 0.03, 0.5]
q = FE.bh_fdr([0.001, 0.02, 0.5])
ok("bh_fdr 手算核对", np.allclose(q, [0.003, 0.03, 0.5], atol=1e-12), "得到 %s" % q)

# ────────────────────────────────────────────────────────────
print("\n=== 3. 幂等性（同一批数据写两次，面板行数不变）===")
tmpdir = tempfile.mkdtemp(prefix="factor_test_")
db_path = os.path.join(tmpdir, "test.db")
conn = sqlite3.connect(db_path)
conn.execute(
    "CREATE TABLE IF NOT EXISTS stock_daily ("
    "id INTEGER PRIMARY KEY AUTOINCREMENT, symbol TEXT, date TEXT, "
    "open REAL, high REAL, low REAL, close REAL, volume REAL, turnover REAL, "
    "UNIQUE(symbol,date))"
)
frames = []
for i, (seed, sym) in enumerate([(101, "000001"), (102, "000002"), (103, "600000")]):
    f = make_ohlcv(300, seed=seed, symbol=sym)
    frames.append(f)
all_df = pd.concat(frames, ignore_index=True)
conn.executemany(
    "INSERT OR REPLACE INTO stock_daily(symbol,date,open,high,low,close,volume,turnover) "
    "VALUES(?,?,?,?,?,?,?,?)",
    [(r.symbol, r.date, r.open, r.high, r.low, r.close, r.volume, r.turnover)
     for r in all_df.itertuples(index=False)],
)
conn.commit()
conn.close()

test_factors = ["mom20", "vol20", "ma_gap20", "turn20", "r2_20"]
stats1 = FP.build_panel(db_path=db_path, symbols=["000001", "000002", "600000"],
                        start="2023-06-01", end=all_df["date"].max(),
                        factors=test_factors, workers=1, progress=False)
conn = sqlite3.connect(db_path)
n1 = conn.execute("SELECT COUNT(*) FROM factor_panel").fetchone()[0]
conn.close()
ok("首次写入行数 > 0", n1 > 0 and stats1["rows_inserted"] == n1,
   "写入 %d 行" % n1)

stats2 = FP.build_panel(db_path=db_path, symbols=["000001", "000002", "600000"],
                        start="2023-06-01", end=all_df["date"].max(),
                        factors=test_factors, workers=1, progress=False)
conn = sqlite3.connect(db_path)
n2 = conn.execute("SELECT COUNT(*) FROM factor_panel").fetchone()[0]
dup = conn.execute(
    "SELECT COUNT(*) FROM (SELECT date,symbol,factor,COUNT(*) c FROM factor_panel "
    "GROUP BY date,symbol,factor HAVING c > 1)").fetchone()[0]
conn.close()
ok("重跑后面板行数不变", n2 == n1, "第一次 %d，第二次 %d" % (n1, n2))
ok("重跑未写入新行（全部被跳过）", stats2["rows_inserted"] == 0
   and stats2["rows_skipped"] == n1,
   "写入 %d，跳过 %d" % (stats2["rows_inserted"], stats2["rows_skipped"]))
ok("无重复主键行", dup == 0, "重复组 %d" % dup)

# 三只票都应该在面板里（每只票每天每因子一行）
conn = sqlite3.connect(db_path)
cnt = conn.execute("SELECT COUNT(DISTINCT symbol) FROM factor_panel").fetchone()[0]
conn.close()
ok("三只票都进了面板", cnt == 3, "distinct symbol = %d" % cnt)

# ────────────────────────────────────────────────────────────
print("\n=== 4. 分组单调性（单调因子 → 分组收益单调，Spearman > 0.9）===")
res = FE.evaluate_factor(panel_wide, fwd_wide, direction=1, horizon=5,
                         n_groups=10, min_stocks=30)
groups = res["groups"]
group_means = [g["mean"] for g in groups]
ok("10 组全部有值", all(m is not None for m in group_means), str(group_means[:3]))
ascending = all(group_means[i] < group_means[i + 1] for i in range(len(group_means) - 1))
# 注意：第 1 组是「预期最好组」，收益最高，所以组号递增时收益应递减
descending = all(group_means[i] > group_means[i + 1] for i in range(len(group_means) - 1))
ok("第 1 组最优时收益严格递减", descending, "前 3 组 %s" % [round(m, 6) for m in group_means[:3]])
ok("monotonicity > 0.9", res["monotonicity"] is not None and res["monotonicity"] > 0.9,
   "monotonicity=%.4f" % (res["monotonicity"] or 0.0))
ok("多空年化为正", res["long_short"]["mean"] is not None and res["long_short"]["mean"] > 0,
   "多空均值 %.6f" % (res["long_short"]["mean"] or 0.0))

# direction=-1 时分组要翻转：把因子取负，第 1 组仍应是最好
res_flip = FE.evaluate_factor(-panel_wide, fwd_wide, direction=-1, horizon=5,
                              n_groups=10, min_stocks=30)
gm_flip = [g["mean"] for g in res_flip["groups"]]
flip_desc = all(gm_flip[i] > gm_flip[i + 1] for i in range(len(gm_flip) - 1))
ok("direction=-1 时分组同步翻转", flip_desc,
   "第1组 %.6f 第10组 %.6f" % (gm_flip[0], gm_flip[-1]))

# ────────────────────────────────────────────────────────────
print("\n=== 5. Newey-West 修正（重叠标签下 t 值不能再虚高 sqrt(h) 倍）===")


def ar1_series(phi, n, seed, mean=0.05):
    """造 AR(1) 序列：x_t = phi*x_{t-1} + e_t，再整体加一个均值让 t 不为 0。"""
    rng = np.random.default_rng(seed)
    noise = rng.normal(0.0, 1.0, n)
    x = np.zeros(n)
    x[0] = noise[0]
    for t in range(1, n):
        x[t] = phi * x[t - 1] + noise[t]
    return x + mean


def naive_t(x):
    """朴素 t：把序列当独立样本（重叠标签下的错误做法，仅作对照）。"""
    x = np.asarray(x, dtype="float64")
    return float(x.mean() / x.std(ddof=1) * np.sqrt(len(x)))


# (a) 强自相关（phi=0.6）：理论长期方差膨胀 (1+phi)/(1-phi)=4 → t 应缩到约 1/2。
#     注意 lag=19 的 Bartlett 截断会让理论值略高于 0.50（约 0.53），所以阈值取 0.6。
ratios_06 = []
for seed in range(6):
    series = ar1_series(0.6, 2000, seed)
    ratios_06.append(abs(FE.newey_west_t(series, 19) / naive_t(series)))
mean_ratio_06 = float(np.mean(ratios_06))
ok("AR(1) phi=0.6 → NW t 缩到朴素 t 的一半左右", mean_ratio_06 < 0.6,
   "6 条序列平均 |t_nw/t_naive| = %.3f（理论 0.53，无限阶 0.50）" % mean_ratio_06)

# (b) 更强自相关（phi=0.85）：膨胀 (1+.85)/(1-.85)=12.3 → t 应缩到 0.3 倍以下
series_85 = ar1_series(0.85, 2000, 7)
ratio_85 = abs(FE.newey_west_t(series_85, 19) / naive_t(series_85))
ok("AR(1) phi=0.85 → |t_NW| < 0.5 × |t_朴素|", ratio_85 < 0.5,
   "ratio = %.3f（理论 0.28）" % ratio_85)

# (c) 无自相关（白噪声 n=2000）：NW 应与朴素 t 接近，不能把显著的搞成不显著
ratios_iid = []
for seed in range(6):
    series = np.random.default_rng(1000 + seed).normal(0.1, 1.0, 2000)
    ratios_iid.append(abs(FE.newey_west_t(series, 19) / naive_t(series)))
mean_ratio_iid = float(np.mean(ratios_iid))
ok("白噪声 → NW t 与朴素 t 接近（比值 0.8~1.25）",
   0.8 < mean_ratio_iid < 1.25, "平均 ratio = %.3f" % mean_ratio_iid)

# (d) 手工核对 NW 公式：const 序列（方差 0）返回 nan；样本 < 3 返回 nan
ok("常数序列 NW t 为 nan", np.isnan(FE.newey_west_t([0.1, 0.1, 0.1, 0.1], 3)),
   "%s" % FE.newey_west_t([0.1] * 4, 3))
ok("样本 < 3 时 NW t 为 nan", np.isnan(FE.newey_west_t([0.1, 0.2], 1)))

# (e) 手工核对：lag=0 时 NW t 必须等于「用总体标准差(ddof=0)算的 t」——
#     NW 公式的 gamma0 用 1/n 归一，所以对照项也要用 ddof=0
x_demo = np.random.default_rng(5).normal(0.2, 1.0, 300)
naive_ddof0 = float(x_demo.mean() / x_demo.std(ddof=0) * np.sqrt(len(x_demo)))
ok("lag=0 时 NW t == 总体标准差口径的 t",
   abs(FE.newey_west_t(x_demo, 0) - naive_ddof0) < 1e-12,
   "nw=%.12f 对照=%.12f" % (FE.newey_west_t(x_demo, 0), naive_ddof0))

# ────────────────────────────────────────────────────────────
print("\n=== 6. 建仓口径（entry）：T+1 开盘 vs T 日收盘 ===")
n_day = 20
px_dates = make_dates(n_day)
rng_px = np.random.default_rng(31)
close_px = pd.DataFrame(
    {"A": 10 * np.exp(np.cumsum(rng_px.normal(0, 0.01, n_day))),
     "B": 20 * np.exp(np.cumsum(rng_px.normal(0, 0.01, n_day)))},
    index=px_dates)
open_px = close_px * (1 + rng_px.normal(0, 0.004, close_px.shape))

fwd_close = FE.load_forward_returns(close_px, 5, entry="close")
fwd_s1c = FE.load_forward_returns(close_px, 5, entry="s1_close")
fwd_s1o = FE.load_forward_returns(close_px, 5, entry="s1_open", open_wide=open_px)

hand_close = close_px.iloc[5, 0] / close_px.iloc[0, 0] - 1.0
hand_s1o = close_px.iloc[5, 0] / open_px.iloc[1, 0] - 1.0
ok("entry=close 等于 close[T+h]/close[T]-1",
   abs(fwd_close.iloc[0, 0] - hand_close) < 1e-12,
   "得到 %.8f 手算 %.8f" % (fwd_close.iloc[0, 0], hand_close))
ok("entry=s1_open 等于 close[T+h]/open[T+1]-1",
   abs(fwd_s1o.iloc[0, 0] - hand_s1o) < 1e-12,
   "得到 %.8f 手算 %.8f" % (fwd_s1o.iloc[0, 0], hand_s1o))
ok("两种口径收益必须不同", abs(fwd_close.iloc[0, 0] - fwd_s1o.iloc[0, 0]) > 1e-9,
   "close=%.6f s1_open=%.6f" % (fwd_close.iloc[0, 0], fwd_s1o.iloc[0, 0]))
ok("s1_close 与 s1_open 也不同",
   abs(fwd_s1c.iloc[0, 0] - fwd_s1o.iloc[0, 0]) > 1e-9,
   "s1_close=%.6f s1_open=%.6f" % (fwd_s1c.iloc[0, 0], fwd_s1o.iloc[0, 0]))
ok("默认 entry 是 s1_open",
   abs(FE.load_forward_returns(close_px, 5, entry="s1_open",
                               open_wide=open_px).iloc[0, 0] - hand_s1o) < 1e-12)
ok("最后 horizon 行为 NaN（无未来数据）", bool(fwd_s1o.iloc[-1].isna().all()))
# 缺失的票必须是 NaN，不能被填成 0（0 会被当成暴跌 -100% 参与 IC）
partial = open_px.copy()
partial.loc[px_dates[1], "A"] = np.nan
fwd_partial = FE.load_forward_returns(close_px, 5, entry="s1_open", open_wide=partial)
ok("缺开盘价的票返回 NaN 而不是 0",
   bool(np.isnan(fwd_partial.iloc[0, 0]))
   and abs(fwd_partial.iloc[0, 1] - fwd_s1o.iloc[0, 1]) < 1e-12,
   "A=%.6f（应为 NaN） B 与完整版一致" % fwd_partial.iloc[0, 0])
try:
    FE.load_forward_returns(close_px, 5, entry="s1_open")
    ok("缺 open_wide 时抛错", False, "未抛异常")
except ValueError:
    ok("缺 open_wide 时抛错", True)
try:
    FE.load_forward_returns(close_px, 0)
    ok("horizon=0 抛错", False, "未抛异常")
except ValueError:
    ok("horizon=0 抛错", True)

# ────────────────────────────────────────────────────────────
print("\n=== 7. 覆盖率拆成两个口径 + direction_note ===")
cov_dates = make_dates(40)
cov_syms_panel = ["P%02d" % i for i in range(10)]
cov_syms_extra = ["M%03d" % i for i in range(90)]
rng_cov = np.random.default_rng(77)
cov_panel = pd.DataFrame(
    {s: rng_cov.normal(0, 1, len(cov_dates)) for s in cov_syms_panel}, index=cov_dates)
# 面板里故意挖掉 10% 的格子，看 panel_completeness 是否能反映
cov_panel.iloc[::10, 0] = np.nan
cov_univ = pd.DataFrame(
    {s: 10.0 for s in cov_syms_panel + cov_syms_extra}, index=cov_dates)
cov_fwd = pd.DataFrame(
    {s: rng_cov.normal(0, 0.02, len(cov_dates)) for s in cov_syms_panel}, index=cov_dates)
cov_res = FE.evaluate_factor(cov_panel, cov_fwd, direction=1, horizon=5,
                             n_groups=5, min_stocks=5, universe_wide=cov_univ)
ok("panel_completeness ≈ 0.99（池内缺格子）",
   cov_res["panel_completeness"] is not None
   and abs(cov_res["panel_completeness"] - 0.99) < 0.02,
   "%.4f" % (cov_res["panel_completeness"] or -1))
ok("market_coverage ≈ 0.10（面板只占全市场 10/100）",
   cov_res["market_coverage"] is not None
   and abs(cov_res["market_coverage"] - 0.10) < 0.02,
   "%.4f" % (cov_res["market_coverage"] or -1))
ok("两个覆盖率必须分开且不等", cov_res["panel_completeness"] != cov_res["market_coverage"])
ok("旧字段 coverage 已移除", "coverage" not in cov_res, str(sorted(cov_res.keys())))

# direction_note：让日 IC 在 ±1 之间交替（均值 ≈ -1/61，标准差 ≈ 1），
# 于是 IC 符号与先验相反、但 NW t 极小 → 必须给提示
alt_n_sym, alt_n_day = 60, 61
alt_dates = make_dates(alt_n_day)
alt_factor = pd.DataFrame({("K%02d" % i): [i for _ in alt_dates]
                           for i in range(alt_n_sym)}, index=alt_dates, dtype="float64")
alt_fwd = pd.DataFrame(index=alt_dates, columns=alt_factor.columns, dtype="float64")
for k, d in enumerate(alt_dates):
    # 偶数日反向（IC=-1）、奇数日同向（IC=+1）；0..60 共 61 天，偶数日多一天，
    # 于是日 IC 均值 = -1/61 < 0，符号与 direction=+1 相反
    sign = -1.0 if k % 2 == 0 else 1.0
    alt_fwd.loc[d] = alt_factor.loc[d] * sign * 1e-4
alt_res = FE.evaluate_factor(alt_factor, alt_fwd, direction=1, horizon=5,
                             n_groups=5, min_stocks=30)
ok("IC 符号与先验相反且不显著 → 给出 direction_note",
   alt_res["direction_note"] is not None
   and "勿按 direction" in alt_res["direction_note"],
   "ic_mean=%.4f t_nw=%.3f note=%s"
   % (alt_res["rank_ic"]["mean"] or 0, alt_res["rank_ic"]["t_nw"] or 0,
      alt_res["direction_note"]))
ok("IC 与先验一致时不提示", res["direction_note"] is None, str(res["direction_note"]))

# ────────────────────────────────────────────────────────────
print("\n=== 8. 边角：horizon<=0 的年化、Amihud 脏数据容忍 ===")
ok("_annualize(mean, 0) 返回 None", FE._annualize(0.01, 0) is None,
   str(FE._annualize(0.01, 0)))
ok("_annualize(mean, -1) 返回 None", FE._annualize(0.01, -1) is None)
ok("_annualize(mean, 5) 正常年化", abs(FE._annualize(0.005, 5) - (1.005 ** 50.4 - 1)) < 1e-9,
   "%.6f" % (FE._annualize(0.005, 5) or 0))

dirty = make_ohlcv(120, seed=13)
dirty.loc[dirty.index[60], "turnover"] = 0.0      # 单日脏数据
amihud = compute_series(dirty, ["amihud20"])["amihud20"]
nan_after = int(amihud.iloc[60:].isna().sum())
ok("单日成交额为 0 不再拖累其后 20 天（NaN 数 < 5）", nan_after < 5,
   "脏点之后 NaN 数 %d（修复前会到 20）" % nan_after)
ok("脏点前后仍有有效值", bool(amihud.iloc[59:61].notna().any()),
   "60 日附近值 %.6g" % float(amihud.iloc[60]))

# ────────────────────────────────────────────────────────────
print("\n=== 9. 补充：compute_one 数据不足返回 None 而不是 0 ===")
short = make_ohlcv(15, seed=5)
one = compute_one(short)
ok("样本不足时 mom120 为 None", one["mom120"] is None, str(one["mom120"]))
ok("样本足够时 ret1 有值", one["ret1"] is not None, str(one["ret1"]))
ok("返回值域没有 NaN/inf",
   all(v is None or np.isfinite(v) for v in one.values()), "None 个数 %d"
   % sum(1 for v in one.values() if v is None))
ok("全部因子都有键", set(one.keys()) == set(all_names()), "%d 个键" % len(one))

# ────────────────────────────────────────────────────────────
print("\n=== 10. h=1 的 lag=0 / 静默全 0 防护 / None 显示 / 反向显著提示 ===")

# (a) h=1 无重叠 → lag 必须是 0；此时 NW 方差退化为 gamma0，t 应等于朴素 t
#     （只差 ddof 因子：t_nw = t_value * sqrt(n/(n-1))，因为 NW 的 gamma0 用 1/n 归一）
#     n 取 400，让 ddof 差异（约 0.13%）落在 0.5% 阈值内，与真实数据（n≈350）一致
H1_N = 400
h1_dates = make_dates(H1_N)
h1_factor = pd.DataFrame({("H%02d" % i): [i + (0.3 * ((i * j) % 5)) for j in range(H1_N)]
                          for i in range(40)}, index=h1_dates, dtype="float64")
h1_fwd = pd.DataFrame(index=h1_dates, columns=h1_factor.columns, dtype="float64")
rng_h1 = np.random.default_rng(404)
for d in h1_dates:
    h1_fwd.loc[d] = h1_factor.loc[d] * 1e-4 + rng_h1.normal(0, 1e-3, h1_factor.shape[1])
h1_res = FE.evaluate_factor(h1_factor, h1_fwd, direction=1, horizon=1,
                            n_groups=5, min_stocks=10)
n1 = h1_res["rank_ic"]["n_days"]
t1_nw, t1_naive = h1_res["rank_ic"]["t_nw"], h1_res["rank_ic"]["t_value"]
ok("h=1 → nw_lag == 0", h1_res["rank_ic"]["nw_lag"] == 0,
   "nw_lag=%s" % h1_res["rank_ic"]["nw_lag"])
ok("h=1 → |t_nw| 与 |t_朴素| 差 < 0.5%（仅 ddof 差异）",
   t1_nw is not None and t1_naive is not None
   and abs(abs(t1_nw) - abs(t1_naive)) / abs(t1_naive) < 0.005,
   "t_nw=%.6f t_naive=%.6f n=%d" % (t1_nw or 0, t1_naive or 0, n1))
ok("h=1 → t_nw 精确等于 t_朴素×sqrt(n/(n-1))",
   t1_nw is not None and abs(t1_nw - t1_naive * np.sqrt(n1 / (n1 - 1))) < 1e-12,
   "差 %.3e" % abs((t1_nw or 0) - (t1_naive or 0) * np.sqrt(n1 / (n1 - 1))))
h5_res = FE.evaluate_factor(h1_factor, h1_fwd, direction=1, horizon=5,
                            n_groups=5, min_stocks=10)
ok("h=5 → nw_lag == 4", h5_res["rank_ic"]["nw_lag"] == 4,
   "nw_lag=%s" % h5_res["rank_ic"]["nw_lag"])

# (b) entry=s1_close + horizon=1：分子分母同价（恒 0），必须报错而不是静默全 0
try:
    FE.load_forward_returns(close_px, 1, entry="s1_close")
    ok("s1_close + h=1 抛 ValueError", False, "未抛异常")
except ValueError as exc:
    ok("s1_close + h=1 抛 ValueError", "恒为 0" in str(exc), str(exc)[:60])
ok("s1_close + h=2 正常可用",
   bool(np.isfinite(FE.load_forward_returns(close_px, 2, entry="s1_close").iloc[0, 0])))

# (c) print_single 对 None 一律显示 "-"，不得出现 +0.0000 这种伪读数
import contextlib  # noqa: E402
import io  # noqa: E402

empty_res = FE.evaluate_factor(pd.DataFrame(), pd.DataFrame(), direction=1, horizon=5)
fake_payload = {"meta": {"horizon": 5, "entry": "s1_open"}, "factors": {"mom20": empty_res},
                "fdr": {}, "corr_matrix": {}}
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    FE.print_single(fake_payload, "mom20")
single_out = buf.getvalue()
ok("n_days=0 时 print_single 不出现 +0.0000", "+0.0000" not in single_out
   and "0.0000" not in single_out, single_out.strip().replace("\n", " | ")[:110])
ok("n_days=0 时 IC 显示为 -", "IC均值 -" in single_out,
   [ln for ln in single_out.splitlines() if "IC均值" in ln][:1])

# (d) 反向提示两档：显著反号必须给强提示；同向显著不得提示
sig_n_sym, sig_n_day = 60, 200
sig_dates = make_dates(sig_n_day)
vals = np.arange(sig_n_sym, dtype="float64")
sig_factor = pd.DataFrame({("S%02d" % i): np.full(sig_n_day, vals[i])
                           for i in range(sig_n_sym)}, index=sig_dates)
rng_sig = np.random.default_rng(909)
rev = pd.DataFrame(rng_sig.normal(0, 1, (sig_n_day, sig_n_sym)),
                   index=sig_dates, columns=sig_factor.columns)
sig_fwd_rev = -0.6 * sig_factor + rev            # 与先验 direction=+1 显著反向
sig_fwd_same = 0.6 * sig_factor + rev            # 与先验同向且显著

rev_res = FE.evaluate_factor(sig_factor, sig_fwd_rev, direction=1, horizon=5,
                             n_groups=5, min_stocks=10)
same_res = FE.evaluate_factor(sig_factor, sig_fwd_same, direction=1, horizon=5,
                              n_groups=5, min_stocks=10)
ok("显著反号 → 强提示（含 t_NW 与「复核 registry.direction」）",
   rev_res["direction_note"] is not None
   and "显著" in rev_res["direction_note"]
   and "复核 registry.direction" in rev_res["direction_note"],
   "t_nw=%.2f note=%s" % (rev_res["rank_ic"]["t_nw"] or 0, rev_res["direction_note"]))
ok("显著反号 → direction_sign_mismatch=True", bool(rev_res["direction_sign_mismatch"]))
ok("同向显著 → 不提示", same_res["direction_note"] is None
   and not same_res["direction_sign_mismatch"],
   "t_nw=%.2f" % (same_res["rank_ic"]["t_nw"] or 0))
# 强提示下第 1 组不能再被标成「预期最好」
buf2 = io.StringIO()
with contextlib.redirect_stdout(buf2):
    FE.print_single({"meta": {"horizon": 5, "entry": "s1_open"},
                     "factors": {"mom20": rev_res}, "fdr": {}, "corr_matrix": {}}, "mom20")
rev_out = buf2.getvalue()
ok("反号时不再标注「第 1 组 = 预期最好」", "预期最好" not in rev_out,
   "含「按先验」=%s" % ("按先验" in rev_out))

# (e) 口径标注：decay_caliber 与 meta.entry_note_h1
ok("decay_caliber: s1_open+h1 = 日内", FE.decay_caliber("s1_open", 1) == "日内",
   FE.decay_caliber("s1_open", 1))
ok("decay_caliber: s1_open+h5 = 含隔夜", FE.decay_caliber("s1_open", 5) == "含隔夜",
   FE.decay_caliber("s1_open", 5))
ok("decay_caliber: close+h1 = 含隔夜（同日收盘口径无隔夜概念之分）",
   FE.decay_caliber("close", 1) == "含隔夜", FE.decay_caliber("close", 1))
_src = open(os.path.join(HERE, "factor_eval.py"), encoding="utf-8").read()
ok("meta 里有 entry_note_h1", "entry_note_h1" in _src)
ok("空结果也带 direction_sign_mismatch 字段", "direction_sign_mismatch" in empty_res)

print("\n========================================")
print("RESULT: pass=%d fail=%d" % (PASS, FAIL))
if FAILS:
    print("FAILED:", ", ".join(FAILS))
print("========================================")
sys.exit(0 if FAIL == 0 else 1)
