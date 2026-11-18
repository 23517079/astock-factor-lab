#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/synth_env.py — T03 三个测试共用的**合成数据环境**

为什么不用生产库
----------------
1. `factor_ts.ensure()` 进门就 `coverage()` 断言 24 因子缓存齐备。测试跑的时候
   缓存可能只补齐了 8/24（上线前的补齐要 60–90 分钟），测试会因此失败；
2. 本轮的验收里有「篡改 `ts.last` 到 panelMaxDate」「把 T 之后的未来价格整体
   ×1.37」这类**破坏性实验**，在真库/真缓存上做等于自己制造事故；
3. 合成数据能精确控制「日期数 / 样本数 / 信号强度」，让断言有确定的期望值。

设计要点
--------
* **价格 → fwd → 因子** 三段真实链路：价格用几何随机游走，`fwd` 由
  `factor_eval.load_forward_returns` 算（与生产同一份代码），因子值只依赖
  `close` 截至当日的路径（20 日动量 + 个股噪声）——
  因此「把 T 之后的价格 ×1.37」**不会改变 T 及之前的因子值**，
  只会改变 `(T-h, T]` 区间的 `fwd`。这正是前视测试需要的不对称性。
* 日期、股票、噪声全部由固定种子派生，多次运行结果逐位相同。
* 环境变量（`FACTOR_TS_DIR` / `FACTOR_EVAL_PATH` / `PANEL_REBUILD_PATH`）
  指向临时目录，**绝不触碰 `data/`**。
"""

from __future__ import annotations

import hashlib
import os
import shutil
import sqlite3
import sys
from typing import Any, Dict, List, Optional, Sequence

import numpy as np
import pandas as pd

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import factor_eval as FE                        # noqa: E402
import factor_ts as TS                          # noqa: E402
from factors import registry                    # noqa: E402

# 合成环境的默认口径（与 run_factor_pipeline.DEFAULTS 一致）
PARAMS: Dict[str, Any] = {"horizon": 5, "entry": "s1_open",
                          "minStocks": 100, "nGroups": 10}
# 逐日样本数：必须 > minStocks=100，否则 daily_ic 全部丢日
N_SYMBOLS = 320
# 起始日期（只影响字符串排序，不代表真实交易日历）
START_DATE = "2021-01-04"


def _trading_dates(n: int, start: str = START_DATE) -> List[str]:
    """生成 n 个「工作日」日期字符串（不含节假日，够用且稳定）。"""
    out: List[str] = []
    stamp = pd.Timestamp(start)
    while len(out) < n:
        if stamp.weekday() < 5:
            out.append(stamp.strftime("%Y-%m-%d"))
        stamp = stamp + pd.Timedelta(days=1)
    return out


def _seed_of(text: str) -> int:
    """稳定种子（`hash()` 受 PYTHONHASHSEED 影响，不能用）。"""
    return int(hashlib.sha1(text.encode("utf-8")).hexdigest()[:8], 16)


class SynthEnv:
    """一个自洽的合成环境：临时目录 + 临时库 + 价格矩阵 + 因子宽表。

    Attributes:
        dates: 用于建序列的日期轴（长度 `n_dates`）。
        dates_all: 价格/因子矩阵的完整日期轴（`n_dates + n_extra`），
            `dates` 之后的 `n_extra` 天用于「追加」用例。
        close / open: 价格宽表（index=dates_all, columns=symbols）。
        volume: 成交量宽表（恒为正；测试里可手动置 0 造「不可买」）。
        conn: 只读用的临时 sqlite 连接（含 factor_panel 表）。
    """

    def __init__(self, root: str, name: str = "env", n_dates: int = 260,
                 n_extra: int = 3, horizon: int = 5) -> None:
        self.dir = os.path.join(root, name)
        os.makedirs(self.dir, exist_ok=True)
        self.ts_dir = os.path.join(self.dir, "factor_ts")
        self.db_path = os.path.join(self.dir, "synth.db")
        self.eval_path = os.path.join(self.dir, "factor_eval.json")
        self.rebuild_path = os.path.join(self.dir, "panel_rebuild.json")

        # 环境变量：本模块所有路径都从它们读（生产路径不受影响）
        os.environ["FACTOR_TS_DIR"] = self.ts_dir
        os.environ["FACTOR_EVAL_PATH"] = self.eval_path
        os.environ["PANEL_REBUILD_PATH"] = self.rebuild_path

        self.horizon = int(horizon)
        self.dates_all = _trading_dates(n_dates + n_extra)
        self.dates = self.dates_all[:n_dates]
        self.symbols = ["S%04d" % i for i in range(N_SYMBOLS)]

        self.close, self.open = self._make_prices(seed=20261116)
        rng = np.random.default_rng(20261117)
        vol = rng.uniform(1.0e6, 5.0e7, size=self.close.shape)
        self.volume = pd.DataFrame(vol, index=self.close.index,
                                   columns=self.close.columns, dtype="float64")
        self._panel_cache: Dict[str, pd.DataFrame] = {}

        self.conn = sqlite3.connect(self.db_path)
        self.conn.execute("CREATE TABLE IF NOT EXISTS factor_panel "
                          "(date TEXT, symbol TEXT, factor TEXT, value REAL)")
        self.conn.execute("CREATE UNIQUE INDEX IF NOT EXISTS ux_factor_panel "
                          "ON factor_panel(date, symbol, factor)")
        # 库里必须有真实日期行：`factor_ts.append()` 的候选日期来自
        # `SELECT DISTINCT date FROM factor_panel WHERE date > last`，
        # 不是调用方传进来的 axis（防「按 plan.targetDates 追加」那个坑）。
        # 这里给**完整日期轴**各插一行（含末尾 5 天，它们会被 cutoff 排除）。
        self.conn.executemany(
            "INSERT OR REPLACE INTO factor_panel(date, symbol, factor, value) "
            "VALUES(?,?,?,?)",
            [(d, self.symbols[0], f, 0.0)
             for d in self.dates_all for f in registry.all_names()])
        self.conn.commit()

    # ── 价格 ──
    def _make_prices(self, seed: int) -> tuple[pd.DataFrame, pd.DataFrame]:
        """带**负自相关**的日收益（AR(1)，系数 -0.45）造收盘价。

        为什么不能是独立同分布：iid 收益下任何因果因子对未来收益的 IC 都约等于 0，
        于是「滚动验证选出 0 个因子」会成为合成数据的默认结果 —— 那样测试就
        分不清「实现正确但没信号」和「`groups` 没落盘导致判定链断掉」。
        负自相关让「过去跌得多 → 未来涨得多」，因子有真实预测力，
        `verdict_of` 才会真的走到「值得复核 / 方向需翻转」这两档。

        开盘价 = 收盘价 × (1 + 小幅隔夜跳空)，保证 `entry="s1_open"` 有独立的价格来源。
        """
        rng = np.random.default_rng(seed)
        n_d, n_s = len(self.dates_all), len(self.symbols)
        eps = rng.normal(0.0004, 0.02, size=(n_d, n_s))
        rets = np.zeros_like(eps)
        for t in range(1, n_d):
            rets[t] = -0.45 * rets[t - 1] + eps[t]
        rets = np.clip(rets, -0.09, 0.09)
        close = 10.0 * np.cumprod(1.0 + rets, axis=0)
        gaps = rng.normal(0.0, 0.004, size=(n_d, n_s))
        open_ = close * (1.0 + gaps)
        index = pd.Index(self.dates_all, name="date")
        cols = pd.Index(self.symbols, name="symbol")
        return (pd.DataFrame(close, index=index, columns=cols),
                pd.DataFrame(open_, index=index, columns=cols))

    def corrupt_future(self, cutoff_date: str, factor: float = 1.37,
                       per_symbol_sigma: float = 0.0,
                       seed: int = 99) -> np.ndarray:
        """把 `date > cutoff_date` 的收盘价与开盘价整体改动（说明书 §6 的主力用例）。

        Args:
            factor: **统一乘数**（说明书原文用 1.37）。
            per_symbol_sigma: > 0 时改用**逐股**对数正态乘数（`exp(N(0, sigma))`）。
            seed: 逐股乘数的随机种子（固定 → 可复现）。

        Returns:
            实际使用的乘数向量（长度 = 股票数）。

        为什么需要「逐股乘数」这一档
        ---------------------------
        统一乘数 `×1.37` 对 `fwd` 是**单调变换**：
            `fwd'[d] = 1.37*(fwd[d]+1) - 1`（只发生在 `(T-h, T]` 区间）
        单调变换**不改变横截面排名**，因此 Spearman IC 一分不变 ——
        也就是说「统一 ×1.37」这套用例**抓不住** `<= T` 的错实现（见测试里的对照）。
        逐股乘数会改变排名，才是真正有牙齿的污染方式。
        """
        rng = np.random.default_rng(seed)
        n_s = len(self.symbols)
        if per_symbol_sigma > 0:
            mult = float(factor) * np.exp(rng.normal(0.0, per_symbol_sigma, size=n_s))
        else:
            mult = np.full(n_s, float(factor), dtype="float64")
        mask = self.close.index > str(cutoff_date)
        self.close.loc[mask] = self.close.loc[mask].mul(mult, axis=1)
        self.open.loc[mask] = self.open.loc[mask].mul(mult, axis=1)
        self._panel_cache.clear()
        return mult

    # ── 因子面板 ──
    def panel(self, name: str) -> pd.DataFrame:
        """合成单个因子的宽表：**因果**的 w 日反转 + 个股噪声。

        * 窗口 `w` 由因子名派生（5~20），让 24 个因子彼此略有差异；
        * 只用 `close` 截至当日的路径 —— 因此「把 T 之后的价格 ×1.37」
          不会改变 T 及之前的因子值（前视测试依赖这个性质）。
        """
        hit = self._panel_cache.get(name)
        if hit is not None:
            return hit
        window = 5 + _seed_of(name) % 16                     # 5 ~ 20
        prior = self.close / self.close.shift(window) - 1.0
        rev = -prior                                        # 过去跌得多 → 值大
        z = rev.sub(rev.mean(axis=1), axis=0).div(rev.std(axis=1, ddof=0), axis=0)
        rng = np.random.default_rng(_seed_of("noise:" + name))
        noise = pd.DataFrame(
            rng.normal(0.0, 1.0, size=self.close.shape),
            index=self.close.index, columns=self.close.columns)
        wide = (z + noise * 0.10).astype("float64")
        wide = wide.iloc[20:]                       # 去掉最长窗口的预热期，避免整列 NaN
        self._panel_cache[name] = wide
        return wide

    def fwd(self, horizon: Optional[int] = None, entry: str = "s1_open",
            axis: Optional[Sequence[str]] = None) -> pd.DataFrame:
        """按生产同一份代码算未来收益（`load_forward_returns`）。

        `axis` 决定「价格到今天为止」的边界：`fwd` 末 `horizon` 行必然为 NaN
        （还没有未来的收盘价可用）。传入 `dates` 就等价于「今天只到 dates[-1]」，
        序列因此天然止于 `panelMax - horizon` —— 这正是生产的行为，
        合成数据必须复现它，否则 `ts.last` 会越过探针（一旦越过，
        `verify_lag` 会正确地报错，测试就会失败在一个假问题上）。
        """
        got_close = self.close
        got_open = self.open
        if axis is not None:
            idx = [str(d) for d in axis]
            got_close = self.close.reindex(index=idx)
            got_open = self.open.reindex(index=idx)
        return FE.load_forward_returns(got_close, int(horizon or self.horizon),
                                       entry=entry, open_wide=got_open)

    # ── 与 factor_ts 的接线 ──
    def activate(self) -> "SynthEnv":
        """把本环境的路径与取面板函数**接到 `factor_ts` 上**（多环境场景必须显式调）。

        为什么不能只靠构造函数：`FACTOR_TS_DIR` 是进程级环境变量，
        同一测试里存在两个环境时，后创建的那个会把前一个的目录覆盖掉。
        凡是「读/写某个环境的序列」的地方，都要先 `env.activate()`，
        否则会静默读到另一个环境（甚至生产目录）的 npz。
        """
        os.environ["FACTOR_TS_DIR"] = self.ts_dir
        os.environ["FACTOR_EVAL_PATH"] = self.eval_path
        os.environ["PANEL_REBUILD_PATH"] = self.rebuild_path
        self.patch_panel_loader()
        return self

    def patch_panel_loader(self) -> None:
        """把 `factor_ts` 的取面板函数换成合成数据（生产实现彻底读不到）。"""
        TS._load_panel_wide = lambda conn, name: self.panel(name)   # noqa: SLF001

    def cutoff(self, horizon: Optional[int] = None,
               axis: Optional[Sequence[str]] = None) -> str:
        """面板轴末端往前 horizon 个交易日的日期。"""
        used = list(axis or self.dates)
        got = TS.cutoff_date(used, int(horizon or self.horizon))
        assert got is not None
        return got

    def write_eval(self, ic_series: Optional[Dict[str, List[List[Any]]]] = None,
                   names: Optional[Sequence[str]] = None) -> str:
        """写一份假的 `factor_eval.json`（对账用）。"""
        import json
        payload: Dict[str, Any] = {"meta": {"horizon": self.horizon},
                                   "factors": {}}
        if ic_series is None:
            for name in (names or registry.all_names()):
                ser = TS._load_series(name)                     # noqa: SLF001
                if ser is None:
                    continue
                payload["factors"][name] = {
                    "rank_ic": {"t_nw": 2.5, "mean": float(np.nanmean(ser["ic"]))},
                    "monotonicity": 0.8,
                    "ic_series": [[str(d), float(v)] for d, v in
                                  zip(ser["dates"].tolist(), ser["ic"].tolist())],
                }
        else:
            for name, series in ic_series.items():
                payload["factors"][name] = {
                    "rank_ic": {"t_nw": 2.5, "mean": 0.05},
                    "monotonicity": 0.8, "ic_series": series}
        with open(self.eval_path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        return self.eval_path

    def series(self, name: str) -> Optional[Dict[str, Any]]:
        """直接读磁盘上的序列（绕开任何缓存）。"""
        return TS._load_series(name)                            # noqa: SLF001

    def set_last(self, name: str, date: str, ic_value: float) -> None:
        """把某因子序列的人为末端改到 `date`（用于失效矩阵第 6 条）。

        刻意直接调 `_save_series`：模拟「上次实现有 bug，把未来那天也写进去了」。
        """
        ser = self.series(name)
        assert ser is not None, "序列不存在：%s" % name
        dates = np.append(ser["dates"], np.asarray([date], dtype="U10"))
        ic = np.append(ser["ic"], np.asarray([ic_value], dtype="float32"))
        groups = np.vstack([ser["groups"],
                            np.zeros((1, ser["groups"].shape[1]), dtype="float32")])
        TS._save_series(name, dates, ic, groups)                # noqa: SLF001

    def cleanup(self) -> None:
        """还原环境变量并删临时目录。"""
        for key in ("FACTOR_TS_DIR", "FACTOR_EVAL_PATH", "PANEL_REBUILD_PATH"):
            os.environ.pop(key, None)
        try:
            self.conn.close()
        except sqlite3.Error:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)
