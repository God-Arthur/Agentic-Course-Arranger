"""Pre-flight: do the five reports the task brief names actually carry readable numbers in landed data?

§四 of the brief requires the experiment to report 需求识别质量、最终效用、重要需求满足率、低分位效用 and
运行成本. ``analyze_need_v2.py`` prints all five, and every read goes through ``_require_column``, so a
renamed column stops the run. What that does *not* catch is the other half of the vacuity family: the
column exists in the header but every landed row leaves it empty or non-numeric — ``_means`` then
returns ``None`` and the verdict still looks like a table.

And a third half, which this probe found the first time it ran: a column can be fully numeric and
still say nothing, because **every row holds the same number**. Three completely different things look
identical on disk, so the probe refuses to treat them alike:

* 结果 — ``hard_violations`` all 0 is exactly what §一 asks us to check.
* 构造 — ``llm_calls`` = 12 per seed because 72 profiles are asked in batches of 6; K2's
  ``key_need_recall_pct`` = 100.0 because its records *are* the hidden labels.
* 未测量 — ``solver_seconds`` = 0.0 because ``solve_assignment`` never times anything and the row
  assembly filled the hole with a default.

Only the first kind may be quoted as a finding; the second may be quoted only as its own mechanism;
the third must not be quoted at all. An unexplained constant column fails the probe.

It prints no means, no deltas, no wins and no direction: the batch is still running, and a
partial-batch number is exactly what the pre-registration forbids reading. Run it read-only, anytime:

    .venv/bin/python results_need_v2/report_field_probe.py

It reads landed CSVs only — no solver, no LLM, no writes.
"""
from __future__ import annotations

import glob
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from analyze_need_v2 import (I2, J2, K2, OUT_DIR, _require_column, _usable, load_raw_rows)

# 任务书 §四 点名的五项，逐字对照判定脚本 §2/§4 打印的键。
REQUIRED = [
    ("最终效用", "total_preference_utility", (I2, J2, K2)),
    ("需求识别质量", "need_f1_pct", (I2, J2, K2)),
    ("重要需求满足率", "key_need_fulfillment_pct", (I2, J2, K2)),
    ("重要需求识别召回", "key_need_recall_pct", (I2, J2, K2)),
    ("低分位效用（p10 学生）", "student_p10_utility", (I2, J2, K2)),
    ("运行成本：调用", "llm_calls", (J2,)),
    ("运行成本：提示 token", "llm_prompt_tokens", (J2,)),
    ("运行成本：补全 token", "llm_completion_tokens", (J2,)),
    ("运行成本：缓存命中", "cache_hits", (J2,)),
    ("运行成本：缓存未命中", "cache_misses", (J2,)),
    ("规则未被越过", "hard_violations", (I2, J2, K2)),
    ("运行成本：逐臂求解耗时", "solver_seconds", (I2, J2, K2)),
]

# 键 = (臂, 列)。整列同一个数时，只有这三种解释能放行，而且每种都必须写清机制——
# "看起来正常"不是理由，能查到代码里的出处才是。
RESULT_IS_CONSTANT = "结果"
FORCED_BY_DESIGN = "构造"
NEVER_MEASURED = "未测量"
CONSTANT_EXPLAINED = {
    (I2, "hard_violations"): (RESULT_IS_CONSTANT, "§一 要核对的就是这一列全 0：三臂都没有越过培养方案的硬约束"),
    (J2, "hard_violations"): (RESULT_IS_CONSTANT, "§一 要核对的就是这一列全 0：三臂都没有越过培养方案的硬约束"),
    (K2, "hard_violations"): (RESULT_IS_CONSTANT, "§一 要核对的就是这一列全 0：三臂都没有越过培养方案的硬约束"),
    (J2, "llm_calls"): (FORCED_BY_DESIGN,
                        "semantic_needs.py:38 的 STUDENTS_PER_CALL=6：每颗种子 72 份档案按 6 份一批问，"
                        "72÷6=12 次，恒定是分批构造的结果（也正因此 §8 那条 216 次 = 18 种子 × 12）"),
    (K2, "key_need_recall_pct"): (FORCED_BY_DESIGN,
                                 "semantic_needs.py:535 的 key_tp÷key_total：K2 的需求记录由隐藏真值标签"
                                 "直接生成（build_need_model_v2 parser='truth'），真值里的重要需求必然全在"
                                 "记录中，所以召回 = 100.0 是定义式结果，不能读成'K2 识别得准'"),
    (I2, "solver_seconds"): (NEVER_MEASURED,
                             "planner_core.solve_assignment 的诊断字典只有 engine/status/objective/"
                             "best_bound/bound_gap，没有任何计时刻度；experiments 的行装配原先用 "
                             ".get('solver_seconds', 0.0) 补默认值，所以落盘的 0 不是『求解花了 0 秒』。"
                             "本批不发布逐臂求解耗时，运行成本改用 llm_calls、两类 token 与批次墙钟"
                             "（扣除宿主挂起）说话；生产端那个默认值已改为 None"),
    (J2, "solver_seconds"): (NEVER_MEASURED, "同上：v2 三臂都不经手动过求解计时"),
    (K2, "solver_seconds"): (NEVER_MEASURED, "同上：v2 三臂都不经手动过求解计时"),
}


def main() -> int:
    chunks = sorted(os.path.basename(p) for p in glob.glob(os.path.join(OUT_DIR, "*"))
                    if os.path.exists(os.path.join(p, "raw_rows.csv")))
    if not chunks:
        print("FAIL：还没有任何完成分块，本探针无从核对。")
        return 1
    # 不用 _load：它按预注册拒绝半批数据（那是判定该做的事），本探针要读的正是半成品。
    rows = []
    for chunk in chunks:
        rows += load_raw_rows(os.path.join(OUT_DIR, chunk, "raw_rows.csv"))
    landed = sorted({int(r["seed"]) for r in rows})
    print(f"读的是已收尾分块 {chunks}，落盘种子 {landed}（共 {len(landed)} 颗），行数 {len(rows)}；"
          "只数覆盖，不报数值、不报差值、不报方向。")

    problems = []
    for label, key, arms in REQUIRED:
        for mech in arms:
            try:
                _require_column(rows, mech, key)
            except SystemExit as exc:
                problems.append(f"{label} / {key} @ {mech}：{exc}")
                continue
            sample = [r for r in rows if r["mechanism"] == mech and _usable(r)]
            numeric = sum(1 for r in sample if isinstance(r.get(key), (int, float)))
            print(f"覆盖 {label} | {key} | {mech}：{numeric}/{len(sample)} 行带数值")
            if numeric == 0:
                problems.append(f"{label} / {key} @ {mech}：列在，但 {len(sample)} 行可用行里"
                                f"一个数值都没有——判定会印成 None，等于没报")
                continue
            if numeric != len(sample):
                problems.append(f"{label} / {key} @ {mech}：{numeric}/{len(sample)} 行有数值，"
                                "部分行的该项为空——判定的均值会把它们当缺失跳过，需说明")
                continue
            vals = {r[key] for r in sample}
            if len(vals) > 1 or len(sample) < 2:
                continue
            only = next(iter(vals))
            explained = CONSTANT_EXPLAINED.get((mech, key))
            if explained is None:
                problems.append(f"{label} / {key} @ {mech}：{len(sample)} 行可用行的该项全是 {only}，"
                                "而这张清单里没有它——整列常数既不可能是测量结果也不该被引用，"
                                "先查生产端（是否 .get 默认值补了空、是否由构造决定），查清再决定"
                                "写进 §7C 还是撤下")
                continue
            kind, reason = explained
            print(f"  ↑ 该项 {len(sample)} 行全是 {only}｜定性：{kind}｜机制：{reason}")
            if kind == NEVER_MEASURED:
                print("  ↑ 因此本批不把这列当成本证据引用；判定与 README 都不引用它。")
    if problems:
        print(f"问题 = {len(problems)} 条（批次还在跑，这些现在还能改生产端）：")
        for item in problems:
            print(f"  - {item}")
        return 1
    print("问题 = 无：任务书 §四 点名的五项报告与成本字段，在已落盘的每一臂可用行上都有可读数值；"
          "整列为常数的项都已在清单里定性与写明机制。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
