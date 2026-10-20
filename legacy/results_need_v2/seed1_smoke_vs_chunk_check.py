#!/usr/bin/env python3
"""Compare seed 1 between the rule-only smoke run and the first real v2 chunk, field by field.

Same code, same parameters, same world seed — so any difference is machine-level, not modelling.
The v1 batch already showed a reference arm (D) moving by +41 utilities on a rerun while I/J/K held,
and the arm loop here is pure (`need_v2_arms.run_need_v2_seed` only reads `world`; `simulate_selection`
keeps `remaining`/`final`/`used_slots` local and builds `rng = random.Random(seed)` per call). The
remaining candidate is CP-SAT picking a different optimal preset when several tie, which shows up as
**equal `solver_objective` but different preset rows**. This script decides between those readings.

The field list is validated against the CSV header before anything is compared: a typo in a key would
otherwise read as `None` on both sides and print "一致", which is a vacuous pass, not a check.

Run it after `dev_1_4` lands (it reads only CSVs, no solver, so it is safe during a batch):

    .venv/bin/python results_need_v2/seed1_smoke_vs_chunk_check.py
"""
from __future__ import annotations

import csv
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
KEYS = ("solver_status", "solver_engine", "solver_objective", "solver_best_bound", "solver_bound_gap",
        "total_preference_utility", "sim_min_utility", "sim_max_utility", "average_student_utility",
        "student_p10_utility", "key_need_fulfillment_pct", "required_course_fulfillment_pct",
        "preset_keep_pct", "preset_adjustment_pct", "round1_grants", "round2_grants",
        "unmet_required_final", "target_fill_pct", "seat_utilization_pct", "hard_violations",
        "need_f1_pct", "llm_calls", "llm_prompt_tokens", "llm_completion_tokens", "llm_fallback")


def seed_rows(path: str) -> tuple[dict, dict]:
    """Return (rows by mechanism, header set) for seed 1; empty when the file is not on disk yet."""
    if not os.path.exists(path):
        return {}, {}
    with open(path, encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = set(reader.fieldnames or [])
        rows = {row["mechanism"]: row for row in reader if row.get("seed") == "1"}
    return rows, header


def main() -> int:
    smoke, smoke_header = seed_rows(os.path.join(HERE, "smoke_s1", "raw_rows.csv"))
    chunk, chunk_header = seed_rows(os.path.join(HERE, "dev_1_4", "raw_rows.csv"))
    if not chunk:
        print("还没到时候：results_need_v2/dev_1_4/raw_rows.csv 不存在或没有种子 1 的行。")
        return 2
    if not smoke:
        print("缺冒烟行，无从比较。")
        return 2
    missing = [key for key in KEYS if key not in smoke_header | chunk_header]
    if missing:
        print(f"FAIL：比对的字段名在 CSV 表头里找不到：{missing}——先照表头改名，否则这一核对是空的。")
        return 1
    print(f"冒烟臂 {sorted(smoke)}；批次臂 {sorted(chunk)}；比对 {len(KEYS)} 个字段。")
    diffs = 0
    for mech in sorted(set(smoke) & set(chunk)):
        a, b = smoke[mech], chunk[mech]
        if a["status"] != b["status"]:
            print(f"{mech}: 状态本身不同（冒烟 {a['status']} / 批次 {b['status']}），"
                  "冒烟那一条按预注册是 STRAY，不参与判定，本臂不作可重复性结论。")
            continue
        if a["status"] != "已运行":
            print(f"{mech}: 两批都没运行（{a['status']}），跳过。")
            continue
        bad = [(key, a.get(key), b.get(key)) for key in KEYS if (a.get(key) or "") != (b.get(key) or "")]
        if not bad:
            print(f"{mech}: 比对字段全部一致（{len(KEYS)} 项）。")
            continue
        diffs += 1
        print(f"{mech}: 有 {len(bad)} 项不同：")
        for key, x, y in bad:
            print(f"    {key}: 冒烟 {x} → 批次 {y}")
    tie_break = [m for m in sorted(set(smoke) & set(chunk))
                 if smoke[m]["status"] == chunk[m]["status"] == "已运行"
                 and (smoke[m].get("solver_objective") or "") == (chunk[m].get("solver_objective") or "")
                 and (smoke[m].get("total_preference_utility") or "")
                 != (chunk[m].get("total_preference_utility") or "")]
    if tie_break:
        print(f"目标值相同而最终效用不同：{tie_break} —— 并列最优解里的平局裁决，"
              "不是求解器算错，也不是臂之间的状态泄漏。")
    print("结论：漂移臂数 = %d。注意冒烟分块被 need_v2_chunk_plan.py 标为 STRAY，不参与任何判定，"
          "本核对只用于说明参照臂的可重复性，不改变 §7C/§9 的判定口径。" % diffs)
    return 0


if __name__ == "__main__":
    sys.exit(main())
