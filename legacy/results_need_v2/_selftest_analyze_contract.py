#!/usr/bin/env python3
"""SELF-TEST ONLY — generates a synthetic verdict with the real `analyze_need_v2.build()` code path.

`_selftest_fake_verdict_v2.md` was typed by hand, so it only proved that the splice/guard pair agree
with *my idea* of the format. It never proved that the lines `analyze_need_v2.py` actually emits are
the lines `verdict_splice.py` finds and `verdict_readme_guard.py` re-checks — a mismatch in the
minusedash, in the "- 判据（" prefix, or in the table header would only surface 11 hours from now.

This feeds fake rows through `build()` itself. The numbers below are fabricated by a seeded RNG and
are NOT experiment results; nothing here touches the solver or the LLM, so it is safe to run while a
batch is in flight.

    .venv/bin/python results_need_v2/_selftest_analyze_contract.py
"""
from __future__ import annotations

import os
import random
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from analyze_need_v2 import build  # noqa: E402
import analyze_need_v2  # noqa: E402
from need_v2_arms import I2, J2, K2  # noqa: E402

OUT = os.path.join(HERE, "_selftest_from_analyze_v2.md")
CONFIG = {"n_students": 72, "sim_replicates": 3, "llm_model": "selftest-fake-model",
          "llm_requests_per_minute": 20}


def solver_status(mech, seed):
    """A *deliberately asymmetric* convergence shape, so §6's per-arm counts can be checked.

    If all three arms converged identically the disclosure would print the same number three times and
    any static string would match it — the check would be empty. Mirrors what the batch actually landed
    mid-run (I2 8/8, J2 6/8, K2 1/8 OPTIMAL; logs_v2/pair_health_v2_0157.log). Deterministic on purpose:
    the expected numbers below are recomputed from these rows without re-running the producer's helper.
    """
    if mech == I2:
        return "OPTIMAL"
    if mech == J2:
        return "FEASIBLE" if seed % 3 == 0 else "OPTIMAL"
    return "OPTIMAL" if seed % 5 == 0 else "FEASIBLE"


def rows_for(seeds):
    rng = random.Random(20261017)
    rows = []
    for seed in seeds:
        for mech in (I2, J2, K2):
            base = 13000 + seed * 100
            lift = {I2: 0, J2: rng.randint(-30, 40), K2: rng.randint(10, 60)}[mech]
            # 真实生产者在该类别一行都没有时交出 None 而不是 0，这里照做。
            n_pref, n_amb = rng.randint(0, 50), rng.randint(0, 50)
            rows.append({"seed": seed, "mechanism": mech, "status": "已运行",
                         "valid_for_ai_claim": True,
                         "solver_status": solver_status(mech, seed),
                         "total_preference_utility": base + lift,
                         "student_p10_utility": round((base + lift) / 72, 2),
                         "need_f1_pct": round(rng.uniform(40, 70), 2),
                         "key_need_recall_pct": round(rng.uniform(45, 85), 2),
                         "key_need_fulfillment_pct": round(rng.uniform(50, 80), 2),
                         "need_pairs_added": rng.randint(0, 40),
                         "need_coverage_pct": round(rng.uniform(20, 90), 2),
                         "hard_violations": 0,
                         "llm_calls": 12 if mech == J2 else 0,
                         "llm_prompt_tokens": 40000 if mech == J2 else 0,
                         "llm_completion_tokens": 9000 if mech == J2 else 0,
                         "llm_batch_retries": 0,
                         "cache_hits": 0, "cache_misses": 72 if mech == J2 else 0,
                         "v2_goals_per_student": round(rng.uniform(1, 3), 2),
                         "v2_goal_quote_reject_pct": round(rng.uniform(0, 20), 2),
                         "v2_goal_topic_coverage_pct": round(rng.uniform(30, 80), 2),
                         "v2_explicit_request_accuracy_pct": round(rng.uniform(50, 100), 2),
                         "v2_explicit_request_hallucinated": rng.randint(0, 3),
                         "v2_alternative_accuracy_pct": round(rng.uniform(40, 90), 2),
                         "v2_alternative_hallucinated": rng.randint(0, 5),
                         "v2_alternative_missed": rng.randint(0, 20),
                         "v2_class_overclaims_corrected": rng.randint(0, 8),
                         "v2_class_overclaim_rate_pct": round(rng.uniform(0, 15), 2),
                         "v2_open_questions": rng.randint(0, 30),
                         "v2_mean_uncertainty": round(rng.uniform(0.1, 0.5), 3),
                         "v2_rows_explicit": 0, "v2_rows_preference": n_pref,
                         "v2_rows_ambiguous": n_amb,
                         "v2_true_wanted_preference_pct": None if not n_pref else round(rng.uniform(20, 70), 2),
                         "v2_true_wanted_ambiguous_pct": None if not n_amb else round(rng.uniform(5, 40), 2)})
    return rows


def _pct_cells_out_of_range(report: str):
    """任何标签以 (%) 结尾的表格行，其数字格必须落在 0..100——百分比被加总时这里就会红。"""
    bad = []
    for line in report.splitlines():
        cells = [cell.strip() for cell in line.split("|")]
        if len(cells) < 4 or not cells[1].endswith("(%)"):
            continue
        for cell in cells[2:-1]:
            try:
                value = float(cell)
            except ValueError:
                continue
            if not 0.0 <= value <= 100.0:
                bad.append(f"{cells[1]} → {cell}")
    return bad


def _column_guard_variants(rows_for) -> list:
    """A renamed or missing column has to stop the verdict, not be read as 0.

    §4's cache sentence branches on `misses > hits`. With the old `int(r.get(key) or 0)` both sides
    read 0, so a typo'd column would have published "命中不少于未命中，成本没有按种子数线性上升" —
    the exact opposite of what the batch measured (1 hit / 431 misses).
    """
    out = []
    for drop in ("cache_misses", "v2_rows_preference", "llm_calls", "solver_status"):
        broken = [{k: v for k, v in row.items() if k != drop} for row in rows_for(range(7, 27))]
        try:
            build(broken, rows_for(range(1, 7)), CONFIG)
        except SystemExit as exc:
            out.append(f"OK：变异「列 {drop} 缺失」拒绝出稿——{str(exc)[:48]}……")
        else:
            out.append(f"FAIL：变异「列 {drop} 缺失」照样出了稿：它被读成 0/None 当数据用了。")
    try:
        build([r for r in rows_for(range(7, 27)) if r["mechanism"] != J2],
              rows_for(range(1, 7)), CONFIG)
    except SystemExit as exc:
        out.append(f"OK：变异「整臂 J2 不在样本里」拒绝出稿——{str(exc)[:48]}……")
    else:
        out.append("FAIL：整臂 J2 都不在样本里还出了稿：0 会被当成「这一轮一次调用都没有」。")
    return out


def _cache_branch_states(rows_for, build_fn=None) -> list:
    """Five data states must print five different sentences, and each sentence must be backed by the
    numbers that selected it.

    The first version of this check asserted only that the *label* of a branch appeared. The batch
    then measured 0 hits / 288 misses and the producer still printed "同一颗种子内部的复用是有效的" —
    a claim no field in the table supports, which this check waved through because the label
    "未命中多于命中" happened to be in the same sentence. So the zero-hit state is now asserted by its
    own wording plus a ban on the flattering sentence.
    """
    def line(rows: list) -> str:
        text = (build_fn or build)(rows, rows_for(range(1, 7)), CONFIG)
        found = [l for l in text.splitlines() if l.startswith("- 缓存复用实测")]
        return found[0] if found else ""

    def over(key: str, value):
        return [{**r, key: value} if r["mechanism"] == J2 else r
                for r in rows_for(range(7, 27))]

    zero = line(rows_for(range(7, 27)))              # 合成行本身就是 0 命中 / 有未命中（= 真实批次形状）
    partial = line([{**r, "cache_hits": 1000, "cache_misses": 2000} if r["mechanism"] == J2 else r
                    for r in rows_for(range(7, 27))])
    warm = line([{**r, "cache_hits": 72, "cache_misses": 50} if r["mechanism"] == J2 else r
                 for r in rows_for(range(7, 27))])
    dead = line([{**r, "valid_for_ai_claim": False} if r["mechanism"] == J2 else r
                 for r in rows_for(range(7, 27))])
    none = line(over("cache_misses", 0))
    out = []
    for label, text, want in (("零命中", zero, "一次命中都没有"),
                              ("部分命中", partial, "命中少于未命中"),
                              ("确有复用", warm, "命中不少于未命中"),
                              ("全部回退", dead, "无法判断"),
                              ("无记录", none, "缓存既没有命中也没有未命中记录")):
        if want not in text:
            out.append(f"FAIL：状态「{label}」没有印出「{want}」那半句，实际是：{text[:70]}")
    # 零命中必须用实测的「每次调用打包几份档案」来解释成本为什么没爆，而不是靠一句话带过。
    # 合成行 1440 次未命中 / 240 次调用 = 6.0 份；这句里的数字不是算出来的就会在这里红。
    if "打包 6.0 份档案" not in zero:
        out.append(f"FAIL：零命中那句没给出可核对的打包比（期望「打包 6.0 份档案」），"
                   f"实际是：{zero[zero.find('成本完全靠'):][:60]}")
    for label, text in (("零命中", zero), ("部分命中", partial)):
        if "同一颗种子内部的复用是有效" in text:
            out.append(f"FAIL：状态「{label}」仍在断言「同一颗种子内部的复用是有效的」——"
                       "表里每颗种子的档案只解析一次，没有任何字段能证明这句话。")
    if "33.3%" not in partial:
        out.append(f"FAIL：部分命中状态没印出算出来的命中率（期望 33.3%），实际是：{partial[:70]}")
    for label, text in (("全部回退", dead), ("无记录", none)):
        if "命中不少于未命中" in text:
            out.append(f"FAIL：状态「{label}」印出「命中不少于未命中」——空数据被读成了缓存有效。")
    states = {"零命中": zero, "部分命中": partial, "确有复用": warm, "全部回退": dead, "无记录": none}
    if len({t for t in states.values()}) != 5:
        same = [k for k, t in states.items() if list(states.values()).count(t) > 1]
        out.append(f"FAIL：五种缓存数据状态里有句子重复（{'、'.join(same)}），这条分支检查是空的。")
    if len(out) == 0:
        out.append("OK：缓存条目在「零命中 / 部分命中 / 确有复用 / 全部回退 / 无记录」五种状态下各印各话，"
                   "零命中用实测打包比解释成本、不再声称种子内部复用有效，两种空数据都选不到「有效」那半句。")
    return out


def _cache_states_check_is_live(rows_for) -> list:
    """Prove the five-state check above is not just transcribing whatever the producer says today.

    Both mutants reproduce a wording that was actually shipped: the first is the sentence the batch's
    0-hit / 288-miss data contradicted, the second drops the arithmetic that makes the cost claim
    checkable. Each mutant must be caught by the named FAIL, not merely by a non-zero exit.
    """
    src = open(os.path.join(ROOT, "analyze_need_v2.py"), encoding="utf-8").read()
    flattering = ("没有印出「一次命中都没有」", "仍在断言「同一颗种子内部的复用是有效")
    no_arithmetic = ("没给出可核对的打包比",)
    mutants = [
        ("奉承句放回零命中分支", 'cache_note = ("一次命中都没有：',
         'cache_note = ("未命中多于命中：这不等于缓存实现失效——同一颗种子内部的复用是有效的。',
         flattering),
        ("零命中句丢掉实测打包比", 'f"每次调用打包多份档案压下来{packed}。档案每颗种子重新生成',
         '"每次调用打包多份档案压下来。档案每颗种子重新生成', no_arithmetic),
    ]
    out = []
    for name, old, new, wants in mutants:
        if src.count(old) != 1:
            out.append(f"FAIL：变异「{name}」的锚点在 analyze_need_v2.py 里出现 {src.count(old)} 次"
                       "（应为 1 次）——生产者改了措辞，这条变异检查已经没有落点。")
            continue
        namespace = {"__name__": "analyze_need_v2_mutant", "__file__": "analyze_need_v2.py"}
        try:
            exec(compile(src.replace(old, new, 1), "<mutant>", "exec"), namespace)
            problems = _cache_branch_states(rows_for, namespace["build"])
        except Exception as exc:  # noqa: BLE001
            out.append(f"FAIL：变异「{name}」没跑起来（{type(exc).__name__}: {str(exc)[:60]}），"
                       "不能拿一个报错的变异冒充「抓到了」。")
            continue
        missed = [want for want in wants if not any(want in p and p.startswith("FAIL")
                                                    for p in problems)]
        if missed:
            out.append(f"FAIL：变异「{name}」没有被指名抓到（缺 {missed}）。实际输出："
                       + (" / ".join(problems)[:160] if problems else "全绿——这条检查是空的。"))
        else:
            out.append(f"OK：变异「{name}」被指名抓到：{problems[0][:58]}……")
    return out


def _unavailable_row_states(rows_for) -> list:
    """The "all seeds really called the model" clause must be entailed by the count it sits next to.

    `dropped` used to be `status == 已运行 and not valid_for_ai_claim`, so a J2 row whose *status*
    changed (solver/parse failure, a skipped arm) fell out of the count entirely: the verdict would
    have printed "0 行；本轮全部测试种子都真正走了 LLM 解析" about a run that had a failed row in it.
    """
    def line(rows: list) -> str:
        text = build(rows, rows_for(range(1, 7)), CONFIG)
        found = [l for l in text.splitlines() if l.startswith("- 被判不可用的 J2 行")]
        return found[0] if found else ""

    base = rows_for(range(7, 27))
    clean = line(base)
    fallback = line([{**r, "valid_for_ai_claim": False} if r["mechanism"] == J2 and r["seed"] == 9 else r
                     for r in base])
    failed = line([{**r, "status": "求解失败"} if r["mechanism"] == J2 and r["seed"] == 11 else r
                   for r in base])
    out = []
    if "- 被判不可用的 J2 行：0 行" not in clean or "本轮每一行 J2 的状态都是" not in clean:
        out.append(f"FAIL：全部可用的样本没印出「0 行 + 状态陈述」，实际是：{clean[:80]}")
    for label, text in (("一行回退", fallback), ("一行状态不是已运行", failed)):
        if "- 被判不可用的 J2 行：0 行" in text or "本轮每一行 J2 的状态都是" in text:
            out.append(f"FAIL：变异「{label}」仍被当成「全部走了 LLM 解析」——那句宣称与计数不匹配：{text[:90]}")
    if "求解失败" not in failed:
        out.append(f"FAIL：状态被改坏的行没有在句子里报出自己的状态，读者无法知道它是回退还是没跑：{failed[:90]}")
    if len({clean, fallback, failed}) != 3:
        out.append("FAIL：三种状态印出同一句话，这条检查是空的。")
    if not out:
        out.append("OK：「被判不可用的 J2 行」按 _usable 计数，回退与状态异常都算，那句「全部走了 LLM 解析」"
                   "只在真的没有例外时才出现。")
    return out


MARK = "solver_status 为 OPTIMAL 的行数实测为"
OLD_ASSERT = "对 I2 与 J2 同分布"


def _expected_counts(rows: list) -> str:
    """Recompute the per-arm proved-optimum counts straight from the fake rows.

    Deliberately NOT calling `analyze_need_v2._proven_optimal` / `_usable`: importing the producer's own
    helper here would make the check transcribe its answer, which is the vacuity this whole file exists
    to avoid. The re-derivation is three lines, and it is the part under test.
    """
    per = {}
    for arm in (I2, J2, K2):
        usable = [r for r in rows if r["mechanism"] == arm and r["status"] == "已运行"
                  and r.get("valid_for_ai_claim", True)]
        per[arm] = (sum(1 for r in usable if r["solver_status"] == "OPTIMAL"), len(usable))
    return "、".join(f"{arm} {per[arm][0]}/{per[arm][1]}" for arm in (I2, J2, K2))


def _convergence_disclosure(rows_for) -> list:
    """§6's solver-boundary sentence has to print measured per-arm counts, not the old assumption.

    v1 asserted that hitting the wall clock was identically-distributed noise across the three arms and
    never measured it; the running batch contradicted it (I2 8/8, J2 6/8, K2 1/8 OPTIMAL — README §8).
    The replacement sentence only carries information if the numbers it prints are the rows' own.
    """
    def line(rows: list) -> str:
        text = build(rows, rows_for(range(1, 7)), CONFIG)
        found = [l for l in text.splitlines() if MARK in l]
        return found[0] if found else ""

    base = rows_for(range(7, 27))
    text = line(base)
    want = _expected_counts(base)
    out = []
    if len({part.split()[1].split("/")[0] for part in want.split("、")}) == 1:
        out.append(f"FAIL：假行的三臂 OPTIMAL 行数变成同一个数（{want}），这条检查失去分辨力——"
                   "任何静态措辞都能对上。solver_status() 的不对称设计被改坏了。")
    if want not in text:
        out.append(f"FAIL：§6 披露的三臂行数与假行现算结果不一致（期望「{want}」），实际是："
                   f"{text[text.find('实测为'):][:100]}")
    if OLD_ASSERT in text:
        out.append(f"FAIL：§6 又回到未经实测的断言「{OLD_ASSERT}」：{text[:100]}")
    # 整臂被判不可用时分母应当显式归零，而不是让那一臂从句子中消失。
    dead = line([{**r, "valid_for_ai_claim": False} if r["mechanism"] == J2 else r for r in base])
    if f"{J2} 0/0" not in dead:
        out.append(f"FAIL：J2 整臂不可用时披露没有印出「{J2} 0/0」（读者会以为该臂没被统计），实际是："
                   f"{dead[dead.find('实测为'):][:100]}")
    if out:
        return out
    return ["OK：§6 的收敛披露逐臂印出由落盘行现算的 OPTIMAL 计数（" + want + "），"
            "整臂回退时那一条臂显式写成 0/0，旧的「同分布」断言不再出现。"]


def _convergence_check_is_live(rows_for) -> list:
    """Two mutants prove the check above is not transcribing today's wording.

    Mutant 1 restores the unmeasured assertion the batch falsified; mutant 2 keeps the printed format but
    inflates every numerator to its denominator, i.e. "assume everything converged". Each must be caught by
    the named FAIL, not merely by a non-zero exit.
    """
    src = open(os.path.join(ROOT, "analyze_need_v2.py"), encoding="utf-8").read()
    mutants = [
        ("把未经实测的「同分布」断言放回披露句",
         '实测为 {_convergence_phrase(rows_test)}（',
         '实测为 三臂共用上限，所以这项噪声' + OLD_ASSERT + '，无需逐臂统计（',
         ("三臂行数与假行现算结果不一致", "又回到未经实测的断言")),
        ("披露格式不变但分子被当成全部收敛",
         'return sum(1 for r in sample if str(r.get("solver_status", "")).strip() == "OPTIMAL"), len(sample)',
         'return len(sample), len(sample)',
         ("三臂行数与假行现算结果不一致",)),
    ]
    out = []
    for name, old, new, wants in mutants:
        if src.count(old) != 1:
            out.append(f"FAIL：变异「{name}」的锚点在 analyze_need_v2.py 里出现 {src.count(old)} 次"
                       "（应为 1 次）——生产者改了措辞，这条变异检查已经没有落点。")
            continue
        namespace = {"__name__": "analyze_need_v2_mutant", "__file__": "analyze_need_v2.py"}
        try:
            exec(compile(src.replace(old, new, 1), "<mutant>", "exec"), namespace)
            report = namespace["build"](rows_for(range(7, 27)), rows_for(range(1, 7)), CONFIG)
            found = [l for l in report.splitlines() if MARK in l]
            text = found[0] if found else ""
        except Exception as exc:  # noqa: BLE001
            out.append(f"FAIL：变异「{name}」没跑起来（{type(exc).__name__}: {str(exc)[:60]}），"
                       "不能拿一个报错的变异冒充「抓到了」。")
            continue
        want_line = _expected_counts(rows_for(range(7, 27)))
        problems = []
        if want_line not in text:
            problems.append(f"FAIL：三臂行数与假行现算结果不一致：{text[:80]}")
        if OLD_ASSERT in text:
            problems.append(f"FAIL：又回到未经实测的断言：{text[:80]}")
        missed = [want for want in wants if not any(want in p and p.startswith("FAIL")
                                                    for p in problems)]
        if missed:
            out.append(f"FAIL：变异「{name}」没有被指名抓到（缺 {missed}）。实际输出："
                       + (" / ".join(problems)[:160] if problems else "全绿——这条检查是空的。"))
        else:
            out.append(f"OK：变异「{name}」被指名抓到：{problems[0][:58]}……")
    return out


def main() -> int:
    header = ("# 合成样本（SELF-TEST）：本文件的数字由 `analyze_need_v2.build()` 在伪造行上生成，"
              "不是实验结果，不得引用\n\n")
    body = build(rows_for(range(7, 27)), rows_for(range(1, 7)), CONFIG)
    report = header + body
    with open(OUT, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(report + "\n")
    picked = [line for line in report.splitlines()
              if line.startswith(("- 判据（", "- 幅度：", "- 差距：", "- 低分位：", "- 踩线检查：", "| 指标（"))]
    print(f"已写出合成判定：{OUT}")
    print(f"搬运链会抓到的行数：{len(picked)}")
    for line in picked[:8]:
        print("   " + line[:110])
    failures = []
    out_of_range = _pct_cells_out_of_range(report)
    if out_of_range:
        failures.append("百分比列越界（多半是把各种子的百分比加总或平均错了对象）：" + "；".join(out_of_range))
    if not any("J2 − I2 =" in line for line in picked):
        failures.append("build() 生成的主判据行里没有 v2 臂的字面量，守卫与拼接的判据串需要跟着改。")

    saved = analyze_need_v2._pooled_pct
    analyze_need_v2._pooled_pct = lambda rows, mech, pct_key, n_key: \
        analyze_need_v2._total(rows, mech, pct_key)
    try:
        mutated = build(rows_for(range(7, 27)), rows_for(range(1, 7)), CONFIG)
    finally:
        analyze_need_v2._pooled_pct = saved
    if mutated == body:
        failures.append("变异「百分比被当成计数加总」没有改动任何文本，这条自检是空的。")
    caught = _pct_cells_out_of_range(mutated)
    if not caught:
        failures.append("变异「百分比被当成计数加总」没被越界检查抓到，那条检查形同虚设。")
    else:
        print(f"OK：变异「百分比被当成计数加总」被抓到：{caught[0]}")

    for problem in _column_guard_variants(rows_for):
        if problem.startswith("FAIL"):
            failures.append(problem)
        else:
            print(problem)
    for problem in _cache_branch_states(rows_for):
        if problem.startswith("FAIL"):
            failures.append(problem)
        else:
            print(problem)
    for problem in _cache_states_check_is_live(rows_for):
        if problem.startswith("FAIL"):
            failures.append(problem)
        else:
            print(problem)
    for problem in _unavailable_row_states(rows_for):
        if problem.startswith("FAIL"):
            failures.append(problem)
        else:
            print(problem)
    for problem in _convergence_disclosure(rows_for):
        if problem.startswith("FAIL"):
            failures.append(problem)
        else:
            print(problem)
    for problem in _convergence_check_is_live(rows_for):
        if problem.startswith("FAIL"):
            failures.append(problem)
        else:
            print(problem)

    for message in failures:
        print("FAIL：" + message)
    if failures:
        return 1
    print("OK：analyze_need_v2 的实际输出格式与搬运链找的标记一致。")
    print("OK：全部 (%) 列的单元格都在 0..100 内，百分比没有被当成计数加总。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
