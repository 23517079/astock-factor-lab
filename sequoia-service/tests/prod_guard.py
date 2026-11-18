#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""tests/prod_guard.py — 「测试有没有偷偷写生产 `data/`」的统一断言。

为什么需要它
-----------
2026-11-16 真实发生过一次：一个「只重定向了 `FACTOR_RUN_PATH`」的测试跑了
`run_factor_pipeline`，`factor_ts` 那一路用的是**生产目录**，于是在
`data/factor_ts/` 里落了一份合法但空心的 `meta.json`（24 个因子全部
`suspect`、0 个 npz）。危害不是「数据被改坏」，而是下次 `ensure()` 可能
认为「已初始化」而跳过重建。

这类污染的麻烦之处：**写出来的文件格式完全合法**——字段齐、schema 对、
`json.load` 能读，肉眼和解析都发现不了，只是数据来自合成库。
唯一能证伪的办法是：跑测试前后对比生产产物的 `mtime`/`size` 是否逐位不变。

用法（四个会跑 pipeline / 写缓存的测试统一这么写）
------------------------------------------------
    import prod_guard

    before = prod_guard.snapshot()
    ...         # 跑测试
    changed = prod_guard.diff(before)
    ok("生产 data/ 一个字节都没被写", not changed, str(changed))

注意
----
默认**不含** `data/panel_cache/`：补缓存（`--build-all`）期间它一定在变，
把它算进基线会让没有污染的测试假红。缓存类测试（`test_panel_cache_append`）
另有针对缓存目录的专门断言，不要混用。
"""
from __future__ import annotations

import os
import sys
from typing import Dict, Iterable, List, Optional, Tuple

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PROD_DATA = os.path.join(HERE, "data")

# 生产 `data/` 下「pipeline / 因子链会写」的产物清单（相对 `data/`）
WATCHED: Tuple[str, ...] = (
    "factor_run.json",
    "factor_run.lock",
    "factor_score.json",
    "factor_api.json",
    "factor_rolling.json",
    "factor_eval.json",
    "factor_segments.json",
    "picks_recent.json",
    "panel_rebuild.json",
    "scheduler_state.json",
    os.path.join("factor_ts", "meta.json"),
)


def _stat_sig(path: str) -> Optional[Tuple[int, int]]:
    """文件的 (mtime_ns, size)；不存在返回 None（「从无到有」也算变化）。"""
    try:
        info = os.stat(path)
    except OSError:
        return None
    return (int(info.st_mtime_ns), int(info.st_size))


def snapshot(paths: Optional[Iterable[str]] = None) -> Dict[str, Optional[Tuple[int, int]]]:
    """给生产产物拍一次快照。

    Args:
        paths: 相对 `data/` 的路径清单；None 用 `WATCHED`。

    Returns:
        {相对路径: (mtime_ns, size) 或 None}。整目录（如 `factor_ts`）会展开成
        目录下每个文件的快照，这样「新建了一个 npz」也能抓到。
    """
    out: Dict[str, Optional[Tuple[int, int]]] = {}
    for rel in (paths if paths is not None else WATCHED):
        full = os.path.join(PROD_DATA, rel)
        if os.path.isdir(full):
            for root, _dirs, files in os.walk(full):
                for name in files:
                    item = os.path.join(root, name)
                    out[os.path.relpath(item, PROD_DATA)] = _stat_sig(item)
            out[rel + os.sep] = _stat_sig(full)          # 目录本身也记一笔
            continue
        out[rel] = _stat_sig(full)
    return out


def diff(before: Dict[str, Optional[Tuple[int, int]]],
         paths: Optional[Iterable[str]] = None) -> List[str]:
    """对比两次快照，返回**所有变化项**（新增 / 被改 / 被删）。空列表 = 没被碰过。"""
    after = snapshot(paths)
    changed: List[str] = []
    for key in sorted(set(before) | set(after)):
        if before.get(key) != after.get(key):
            changed.append(key)
    return changed


def assert_untouched(before: Dict[str, Optional[Tuple[int, int]]],
                     paths: Optional[Iterable[str]] = None) -> None:
    """`diff()` 的断言版：有任何变化就 `AssertionError`（失败信息带路径）。

    适合放在测试的 `finally` 里当护栏——即使用例本身因为别的原因挂了，
    也能顺手报出「你还写了生产文件」这个更严重的问题。
    """
    changed = diff(before, paths)
    if changed:
        raise AssertionError(
            "生产 data/ 被测试写过（这些文件不该变）：%s" % ", ".join(changed))


if __name__ == "__main__":                                   # 手动体检用
    snap = snapshot()
    for key, val in sorted(snap.items()):
        print("%-32s %s" % (key, val))
    sys.exit(0)
