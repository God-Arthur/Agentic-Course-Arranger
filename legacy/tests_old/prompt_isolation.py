"""Shared instrument for the "hidden truth never reaches the AI" claim (README §3).

Both need-model arms hand the model the same three things — the official course catalogue, each
student's own ``.skill`` prose, and an output template — so both are checked with one instrument.
It is deliberately two-sided:

* positive: every student's prose must appear verbatim and exactly once, the catalogue must be
  non-empty, and the field sets must equal the published contract;
* negative: each latent utility is replaced by a globally unique sentinel number first, so any
  leak of it — as a field name, as a whole score table, or as bare digits in free text — is a
  substring hit on what was actually sent.

The positive half matters: a checker that only searched for forbidden content would also pass on an
empty payload, which proves nothing. ``tests/_selftest_prompt_isolation.py`` runs mutants over both
arms to show this checker can fail.
"""
from __future__ import annotations

import json
import os
import tempfile
from unittest import mock

SENTINEL_BASE = 918273
WIRE_KEYS = {"course_catalogue", "student_records", "output_schema"}
PUBLIC_STUDENT_KEYS = {"student_id", "skill_text"}
CATALOGUE_KEYS = {"course_id", "name", "category", "topic_labels", "prerequisites", "capacity",
                  "official_description"}
# Both prompts must keep saying these out loud; deleting either line is a contract change.
PROMPT_ASSERTIONS = ("真实偏好分数", "培养方案的硬性要求由规则系统提供")


def hide_truth_behind_sentinels(world):
    """Give every (student, course) utility a number that occurs nowhere else in the payload."""
    offset = 0
    for sid, st in sorted(world.students.items()):
        st.true_scores = {cid: SENTINEL_BASE + offset + i
                          for i, cid in enumerate(sorted(st.true_scores))}
        offset += len(st.true_scores)
    return sorted({v for st in world.students.values() for v in st.true_scores.values()})


def run_and_capture(run, reply=None):
    """Run ``run`` with only ``llm_controller._chat`` stubbed; return the (system, user) it sent."""
    calls = []

    def fake_chat(api_key, base_url, model, system, user, temperature=0.1):
        calls.append((system, user))
        if reply is None:
            return {"records": []}, {"prompt_tokens": 1, "completion_tokens": 1}
        return reply(user)

    with mock.patch("llm_controller._chat", side_effect=fake_chat):
        run()
    return calls


def capture_v1_wire(world, skills, settings):
    """The arm behind the published §7B verdict: v1's real batching, cache and payload builder."""
    from semantic_needs import ParseCache, llm_relevance
    with tempfile.TemporaryDirectory() as tmp:
        cache = ParseCache(os.path.join(tmp, "unit-test-v1.jsonl"))
        return run_and_capture(lambda: llm_relevance(world, skills, settings, cache))


def assert_wire_payload_isolated(case, calls, world, skills, api_key):
    case.assertTrue(calls, "一个请求都没抓到，本测试什么都没证明")
    sentinels = sorted({v for st in world.students.values() for v in st.true_scores.values()})
    case.assertEqual(len(sentinels), sum(len(st.true_scores) for st in world.students.values()),
                     "哨兵分数之间有重复，下面的数字查找不再可靠")
    case.assertTrue(all(str(v) not in " ".join(skills.values()) for v in sentinels),
                    "学生原文里本来就出现了哨兵数字，换一个基数再测")
    seen = {}
    for system, user in calls:
        blob = system + json.dumps(user, ensure_ascii=False, sort_keys=True)
        case.assertEqual(set(user), WIRE_KEYS,
                         "送给模型的顶层字段集变了：改了生产代码就要同时改这里的正式契约，不能只放宽断言")
        case.assertTrue(user["course_catalogue"], "课程清单为空，正向核对形同虚设")
        for course in user["course_catalogue"]:
            case.assertEqual(set(course), CATALOGUE_KEYS,
                             f"课程条目 {course.get('course_id')} 的字段集不等于公开契约")
        case.assertTrue(user["student_records"], "本次请求没有任何学生")
        for record in user["student_records"]:
            case.assertEqual(set(record), PUBLIC_STUDENT_KEYS,
                             "学生记录只能包含公开字段，多一个字段就得先证明它不是真值")
            sid = record["student_id"]
            case.assertNotIn(sid, seen, "同一名学生的原文在一次运行里送到了模型两次")
            seen[sid] = record["skill_text"]
        for needle in ("true_scores", "true_score", api_key) + tuple(str(v) for v in sentinels):
            case.assertNotIn(needle, blob, f"提示词里出现了 {needle}")
        for st in world.students.values():
            case.assertNotIn(json.dumps(st.true_scores, ensure_ascii=False, sort_keys=True), blob,
                             "某名学生的完整隐藏效用表出现在提示词中")
        for phrase in PROMPT_ASSERTIONS:
            case.assertIn(phrase, system, f"提示词里那句「{phrase}」被删掉了")
    case.assertEqual(seen, skills, "每名学生的原文必须逐字送到模型手里，一条不多一条不少")
