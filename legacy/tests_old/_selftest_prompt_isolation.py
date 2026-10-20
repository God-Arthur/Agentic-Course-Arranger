#!/usr/bin/env python3
"""Prove the prompt-isolation checker is capable of failing — on both need-model arms.

``J2PromptCarriesNoHiddenTruth`` and ``JPromptCarriesNoHiddenTruth`` pass because the payloads the
production paths build are clean. That proves nothing about the *checker*: a checker that always
returned "clean" would also pass. So this script takes the real captured requests (captured by
patching only ``llm_controller._chat``, with the batching and payload assembly left alone) and
applies eight leak-shaped mutations, each of which must make ``assert_wire_payload_isolated`` raise
with the reason it names.

Scope, stated plainly: this tests the checker, not the producer. A green run here plus green unit
tests together mean "the payloads J and J2 send are clean, and it would be noticed if they
weren't".

    .venv/bin/python tests/_selftest_prompt_isolation.py
"""
from __future__ import annotations

import copy
import json
import math
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from planner_core import generate_world
from semantic_needs import STUDENTS_PER_CALL, narrative_skill_files, truth_need_labels
from tests.prompt_isolation import (assert_wire_payload_isolated, capture_v1_wire,
                                    hide_truth_behind_sentinels)
from tests.test_semantic_needs_v2 import capture_j2_wire

SETTINGS = {"api_key": "test-key", "base_url": "http://localhost:1", "model": "test-model"}


class _Case(unittest.TestCase):
    def runTest(self) -> None:
        pass


def same(a, b) -> bool:
    return json.dumps(a, ensure_ascii=False, sort_keys=True) == \
        json.dumps(b, ensure_ascii=False, sort_keys=True)


def _world_with_sentinels():
    world = generate_world(n_students=12, seed=3)
    skills = narrative_skill_files(world)
    hide_truth_behind_sentinels(world)
    return world, skills


def arm_v1():
    """The arm behind the published §7B verdict: v1's llm_relevance, batched and cached for real."""
    world, skills = _world_with_sentinels()
    return capture_v1_wire(world, skills, SETTINGS), world, skills


def arm_v2():
    world, skills = _world_with_sentinels()
    eligible = {sid: labels["eligible"] for sid, labels in truth_need_labels(world).items()}
    return capture_j2_wire(world, skills, eligible, SETTINGS), world, skills


def first_sentinel(world):
    return sorted({v for st in world.students.values() for v in st.true_scores.values()})[0]


def truth_table(world):
    sid, st = sorted(world.students.items())[0]
    return sid, dict(st.true_scores)


def m_true_field(calls, world, skills):
    """The obvious leak: the hidden table rides along as an extra field on the student record."""
    _, table = truth_table(world)
    calls[0][1]["student_records"][0]["true_scores"] = table


def m_public_extra_field(calls, world, skills):
    """Any unlisted field, even a legitimately visible one, must trip the whitelist."""
    sid = calls[0][1]["student_records"][0]["student_id"]
    calls[0][1]["student_records"][0]["initial_scores"] = dict(world.students[sid].initial_scores)


def m_truth_in_system_prompt(calls, world, skills):
    """Leak through the system message, where no field-name check applies — only the numbers give it away."""
    _, table = truth_table(world)
    calls[0] = (calls[0][0] + "\n真值表 " + json.dumps(table, ensure_ascii=False), calls[0][1])


def m_truth_in_prose(calls, world, skills):
    """Leak disguised as the one field that is allowed to be long free text."""
    sid, table = truth_table(world)
    for record in calls[0][1]["student_records"]:
        if record["student_id"] == sid:
            record["skill_text"] = json.dumps(table, ensure_ascii=False)


def m_catalogue_score(calls, world, skills):
    calls[0][1]["course_catalogue"][0]["mean_true_score"] = first_sentinel(world)


def m_empty_payload(calls, world, skills):
    """The vacuity mutant: a checker that only looks for forbidden content would wave this through."""
    calls[0][1]["course_catalogue"] = []
    calls[0][1]["student_records"] = []


def m_dropped_student(calls, world, skills):
    calls[-1][1]["student_records"].pop()


def m_duplicated_student(calls, world, skills):
    calls[1][1]["student_records"].append(copy.deepcopy(calls[0][1]["student_records"][0]))


MUTANTS = [
    ("记录里多带 true_scores 字段", "公开字段", m_true_field),
    ("记录里多带任何未登记字段", "公开字段", m_public_extra_field),
    ("真值表写进系统提示词", "提示词里出现了", m_truth_in_system_prompt),
    ("真值表伪装成学生原文", "提示词里出现了", m_truth_in_prose),
    ("课程清单里多一个分数字段", "字段集不等于公开契约", m_catalogue_score),
    ("请求体清空只留字段名", "正向核对", m_empty_payload),
    ("少送一名学生", "一条不多一条不少", m_dropped_student),
    ("同一名学生送两次", "送到了模型两次", m_duplicated_student),
]

ARMS = [("J（v1，已发表 §7B 判定用的那条）", arm_v1), ("J2（v2，正在跑的批次）", arm_v2)]


def check_arm(label, builder, case) -> int:
    base, world, skills = builder()
    expected_calls = math.ceil(len(skills) / STUDENTS_PER_CALL)
    print(f"\n[{label}] 捕获 {len(base)} 次请求（按 {expected_calls} 批切分），"
          f"课程清单 {len(base[0][1]['course_catalogue'])} 条，"
          f"哨兵分数 {len({v for st in world.students.values() for v in st.true_scores.values()})} 个。")
    if len(base) != expected_calls:
        print("FAIL：捕获的批次数与批次切分不符，这条捕获不可用。")
        return 1
    try:
        assert_wire_payload_isolated(case, copy.deepcopy(base), world, skills, SETTINGS["api_key"])
    except AssertionError as exc:
        print(f"FAIL：未变异的生产请求体本应通过，却报了「{exc}」——核对器本身坏了。")
        return 1
    print("OK：真实生产请求体通过核对（这是唯一一条关于生产代码形状的证据）。")
    escaped = 0
    for name, expected, mutate in MUTANTS:
        mutated = copy.deepcopy(base)
        mutate(mutated, world, skills)
        if same(mutated, base):
            print(f"FAIL：变异「{name}」没有改动任何内容，这条自检是空的。")
            escaped += 1
            continue
        fired = ""
        try:
            assert_wire_payload_isolated(case, mutated, world, skills, SETTINGS["api_key"])
        except AssertionError as exc:
            fired = str(exc)
        if not fired:
            print(f"FAIL：变异「{name}」没有被发现——隐藏真值可以通过这条通道泄漏。")
            escaped += 1
        elif expected not in fired:
            print(f"FAIL：变异「{name}」报警了，但理由「{fired[:80]}」不是预期的「{expected}」——"
                  "报警被别的检查抢先吃掉，这条通道仍然没测到。")
            escaped += 1
        else:
            print(f"OK：变异「{name}」被抓住：{fired[:64]}")
    return escaped


def main() -> int:
    case = _Case()
    bad = sum(check_arm(label, builder, case) for label, builder in ARMS)
    total = len(MUTANTS) * len(ARMS)
    if bad:
        print(f"\n问题 = {bad} 个变异躲过了核对。")
        return 1
    print(f"\n问题 = 无：两条臂各 {len(MUTANTS)} 个变异、共 {total} 个全部被预期的那条检查抓住。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
