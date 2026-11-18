"""把「对照组合」合并进主推荐文件（thin wrapper，已降级）

现状（2026-11-16 起）
--------------------
每日 pipeline 不再生成「仅近期有效」的对照组合（决策 5：`recentOnly` 冻结为
历史断面，只继承上一版并打 `deprecated: true`）。所以本脚本降级为一个
**可选的手工工具**：

  * `data/picks_recent.json` 存在 → 合并进主文件；
  * 不存在 → 打印一句说明，**退出码 0**（不再 `SystemExit`）。

最后这一点是刻意的：它以前是「重新计算选股.bat 的第 N 步」，缺文件就整个 bat
失败；现在它连定时链路都不在，一个可选的对照文件缺失不该让任何人以为
「今天的选股失败了」。

为什么仍然合并、而不是让接口读两个文件
--------------------------------------
「智能选股」页要能一键在两版之间切换。如果前端分两次请求，切换瞬间会出现
「名单换了但验证指标还没换」的错配窗口，用户会看到一个自相矛盾的页面。
合并成一份、原子替换，前端只读一次就没有这个窗口。

注意：本文件里**不允许出现任何因子名列表**（T03 验收第 9 条用 grep 断言）。
因子清单只能来自主文件与对照文件本身，硬编码一份就必然与 registry 分叉。

用法
----
    python merge_picks.py                       # 主文件 + data/picks_recent.json
    python merge_picks.py --recent other.json   # 指定对照文件
    python merge_picks.py --recent ""           # 显式跳过合并
"""

from __future__ import annotations

import argparse
import json
import os
from typing import Any, Dict


def load(path: str) -> Dict[str, Any]:
    """读 JSON（缺失/损坏返回空 dict，调用方按「没有对照结果」处理）。"""
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def write_atomic(path: str, payload: Dict[str, Any]) -> None:
    """`.tmp → os.replace`（页面读不到半份 JSON）。"""
    parent = os.path.dirname(os.path.abspath(path))
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def merge(main_path: str, recent_path: str) -> Dict[str, Any]:
    """把对照组合并进主文件。

    Returns:
        `{"ok", "merged", "reason", "factors", "dropped"}`
    """
    main = load(main_path)
    if not main:
        return {"ok": False, "merged": False, "reason": "主文件不存在或损坏：%s" % main_path,
                "factors": 0, "dropped": []}
    if not recent_path or not os.path.exists(recent_path):
        return {"ok": True, "merged": False,
                "reason": "没有对照结果（%s），跳过合并" % (recent_path or "-"),
                "factors": 0, "dropped": []}

    rec = load(recent_path)
    keep = list(rec.get("factors") or [])
    if not keep:
        return {"ok": True, "merged": False,
                "reason": "对照结果里没有因子，跳过合并", "factors": 0, "dropped": []}

    main_factors = list(main.get("factors") or [])
    extra = [n for n in keep if n not in main_factors]
    if extra:
        # 不在主组合里的因子无法两版比较：跳过而不是让整条链路失败
        return {"ok": True, "merged": False,
                "reason": "对照结果里的 %s 不在主组合里，无法比较" % ", ".join(extra),
                "factors": 0, "dropped": []}

    # dropped 以主文件里各因子的近期状态为准，避免两处对「失效」的定义分叉
    dropped = [n for n in main_factors
               if ((main.get("recent") or {}).get(n) or {}).get("state") == "reversed"]

    main["recentOnly"] = {
        "factors": keep,
        "dropped": dropped,
        "weights": rec.get("weights") or {},
        "validation": rec.get("validation") or {},
        "oos": rec.get("oos") or {},
        "picks": rec.get("picks") or {},
        "generatedAt": rec.get("generatedAt"),
        "deprecated": bool(rec.get("deprecated", False)),
    }
    # `recentOnlyMergedAt` 已废弃（决策 7：页面/报告均未读，grep 已确认）。
    # 写一个没人读的时间戳只会成为下一轮「页面时间与内容不一致」的隐患来源。
    main.pop("recentOnlyMergedAt", None)
    write_atomic(main_path, main)
    return {"ok": True, "merged": True, "reason": None,
            "factors": len(keep), "dropped": dropped}


def main() -> int:
    ap = argparse.ArgumentParser(description="合并两版推荐结果（可选，thin wrapper）")
    ap.add_argument("--main", default="data/factor_score.json")
    ap.add_argument("--recent", default="data/picks_recent.json")
    args = ap.parse_args()

    res = merge(args.main, args.recent)
    if not res.get("ok"):
        print("！！ %s" % res.get("reason"))
        return 1
    if not res.get("merged"):
        print("跳过：%s" % res.get("reason"))
        return 0
    print("已合并 → %s：对照组合 %d 个因子，剔除已失效 %d 个"
          % (args.main, res["factors"], len(res["dropped"])))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
