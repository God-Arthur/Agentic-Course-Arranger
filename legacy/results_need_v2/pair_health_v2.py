#!/usr/bin/env python3
"""批次进行中的**配对健康度**读数：只回答"已经落盘的种子还够不够做那次符号检验"，不回答效应方向。

用途：主判据要求 20 颗独立测试种子、三臂齐全，而 J2 一旦回退成规则解析就不能作为 AI 证据
（`need_v2_arms.py` 里 `valid_for_ai_claim` 的那条布尔）。批次要跑三小时以上，等到跑完才发现
可用配对已经掉到门槛之下，就只剩"这一批白跑了"这一个结论可写。本工具在批次期间随时可跑：
只读已**完成**分块的 `raw_rows.csv`（`summary.json` 在同分块跑完时才写，见 run_need_v2_chunks.sh），
不碰求解器、不发 LLM 请求、不导入 analyze_need_v2 的样本完整性检查（那检查故意拒绝半批数据）。

**本工具刻意不印任何均值、差值或胜/负局数**：那些是 `analyze_need_v2.py` 在种子齐了之后按预注册
口径算的东西，半批状态下印出来会被读成中期效应结论，而样本还没定。这里只有计数与状态分布。

    .venv/bin/python results_need_v2/pair_health_v2.py
"""
from __future__ import annotations

import csv
import json
import os
import sys
from collections import Counter, defaultdict
from typing import Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from need_v2_arms import NEED_ARMS_V2, PARSER_VERSION_V2  # noqa: E402

TEST_SEEDS = set(range(7, 27))
I2, J2 = NEED_ARMS_V2[0], NEED_ARMS_V2[1]


def _chunks() -> List[str]:
    out = []
    for name in sorted(os.listdir(HERE)):
        if not name.startswith("test_"):
            continue
        d = os.path.join(HERE, name)
        if os.path.exists(os.path.join(d, "summary.json")) and os.path.exists(os.path.join(d, "raw_rows.csv")):
            out.append(name)
    return out


def _rows(chunk: str) -> List[Dict[str, str]]:
    path = os.path.join(HERE, chunk, "raw_rows.csv")
    with open(path, encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    print("=== 配对健康度（批次进行中的运维读数，不是判定；不含任何均值、差值或胜负局数）===")
    chunks = _chunks()
    if not chunks:
        print("还没有任何完成分块：本工具无话可说。")
        return 0
    rows: List[Dict[str, str]] = []
    for chunk in chunks:
        rows.extend(_rows(chunk))
        print(f"计入分块：{chunk}")
    for name in sorted(d for d in os.listdir(HERE) if d.startswith("test_") and d not in chunks):
        print(f"跳过（分块未收尾，可能正在写）：{name}")

    by_seed: Dict[int, Dict[str, Dict[str, str]]] = defaultdict(dict)
    for r in rows:
        seed = int(r["seed"])
        if seed not in TEST_SEEDS:
            print(f"！种子 {seed} 不属于测试集，已从读数里排除")
            continue
        if r.get("need_version") != PARSER_VERSION_V2:
            print(f"！种子 {seed} 的 {r['mechanism']} 行需求模型版本是 {r.get('need_version')}，不是 {PARSER_VERSION_V2}")
            continue
        by_seed[seed][r["mechanism"]] = r

    print(f"\n已落盘测试种子：{len(by_seed)} / {len(TEST_SEEDS)}，缺 {sorted(TEST_SEEDS - set(by_seed))}")
    incomplete = {s: sorted(set(NEED_ARMS_V2) - set(a)) for s, a in sorted(by_seed.items())
                  if set(NEED_ARMS_V2) - set(a)}
    print(f"三臂不齐的种子：{incomplete or '无'}")

    print("\n状态与可用性计数（按臂）：")
    for arm in NEED_ARMS_V2:
        got = [by_seed[s][arm] for s in by_seed if arm in by_seed[s]]
        status = Counter(r["status"] for r in got)
        fallback = sum(1 for r in got if r.get("llm_fallback", "").strip().lower() in {"true", "1"})
        usable = sum(1 for r in got if r["status"] == "已运行"
                     and r.get("valid_for_ai_claim", "true").strip().lower() in {"true", "1"})
        solver = Counter(r.get("solver_status", "") for r in got if r["status"] == "已运行")
        print(f"  {arm}：行 {len(got)}｜状态 {dict(status)}｜求解器 {dict(solver)}｜"
              f"回退 {fallback}｜valid_for_ai_claim 为真 {usable}")

    paired = [s for s in by_seed
              if arm_ok(by_seed[s], J2) and arm_ok(by_seed[s], I2)
              and _num(by_seed[s][J2]) is not None and _num(by_seed[s][I2]) is not None]
    print(f"\n可进入主判据的配对种子数（I2 与 J2 都「已运行」、都可用、效用都读得出）：{len(paired)}")
    print(f"预注册的显著门槛按 20 颗种子的非平局配对冻结；上面的数掉得越多，"
          f"判定越可能只能写成「样本不足以支撑宣称」。")
    if not os.path.exists(os.path.join(HERE, "FROZEN_VERDICT_V2.md")):
        print("判定文件仍不存在：真正的判定只由 analyze_need_v2.py 在种子齐了之后产出。")
    else:
        print("！判定文件已在盘上，本工具的历史读数不再具有时效，请以判定与守卫为准。")
    return 0


def arm_ok(per_arm: Dict[str, Dict[str, str]], arm: str) -> bool:
    r = per_arm.get(arm)
    if r is None or r["status"] != "已运行":
        return False
    if arm in (J2,):
        return r.get("valid_for_ai_claim", "true").strip().lower() in {"true", "1"}
    return True


def _num(r: Dict[str, str]):
    try:
        return float(r["total_preference_utility"])
    except (TypeError, ValueError):
        return None


if __name__ == "__main__":
    raise SystemExit(main())
