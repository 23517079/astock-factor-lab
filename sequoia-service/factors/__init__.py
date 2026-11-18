#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""factors — 因子库

分层：
    registry.py  因子元数据（名称/分类/方向/最小样本/公式说明/数据依赖）
    compute.py   因子计算（逐日序列 + 最新截面），严格禁止前视偏差

典型用法：
    from factors import compute_one, all_names
    values = compute_one(df)            # {factor: float|None}
    values = compute_one(df, ["mom20", "vol20"])
"""

from factors.compute import compute_one, compute_series
from factors.registry import (
    CATEGORIES,
    CATEGORY_LABELS,
    FACTOR_DEFS,
    all_names,
    by_category,
    directions,
    get,
    min_bars_map,
)

__all__ = [
    "CATEGORIES",
    "CATEGORY_LABELS",
    "FACTOR_DEFS",
    "all_names",
    "by_category",
    "compute_one",
    "compute_series",
    "directions",
    "get",
    "min_bars_map",
]
