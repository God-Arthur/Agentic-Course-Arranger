"""Tests for the v2 need model: every new claim is checked against the student's own words."""
from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from unittest import mock

from planner_core import generate_world
from semantic_needs import (STUDENTS_PER_CALL, hard_requirements, narrative_skill_files,
                            predicted_visible, truth_need_labels)
from semantic_needs_v2 import (ALTERNATIVE_MARKER, GOAL_LINE_PREFIX, NEED_CLASS_EXPLICIT,
                               NEED_CLASS_PREFERENCE, NEED_CLASSES, NEED_MODEL_SCHEMA,
                               PARSER_VERSION_V2, build_need_model_v2, score_need_model,
                               truth_accepts_alternatives, truth_first_choice)
from tests.prompt_isolation import (CATALOGUE_KEYS, assert_wire_payload_isolated,
                                    hide_truth_behind_sentinels, run_and_capture)

SETTINGS = {"api_key": "test-key", "base_url": "http://localhost:1", "model": "test-model"}

# The task forbids handing the hidden preferences to the AI, but the tests below in this file stub
# ``_chat_call_v2`` wholesale — so nothing inspected what J2 actually puts on the wire. The shared
# instrument lives in ``tests/prompt_isolation.py``; ``tests/_selftest_prompt_isolation.py`` proves
# it can fail.


def capture_j2_wire(world, skills, eligible, settings):
    """Run the real J2 path with only the HTTP-level chat stubbed, and return what it would send."""
    def reply(user):
        batch = {r["student_id"] for r in user["student_records"]}
        subset = {sid: text for sid, text in skills.items() if sid in batch}
        records, usage = _fabricated_reply(subset, eligible, world)
        return {"records": records}, usage

    calls = run_and_capture(lambda: build_need_model_v2(world, parser="llm", llm_settings=settings,
                                                        skills=skills, cache_dir=tempfile.mkdtemp()),
                            reply=reply)
    if not calls:
        raise AssertionError("J2 臂一次请求都没发——本捕获核对的不是生产代码")
    return calls


def _fabricated_reply(skills, eligible, world):
    """One reply per student that mixes a real quote with four kinds of over-claim."""
    records = []
    for sid, text in skills.items():
        goal_line = next(line for line in text.splitlines() if line.startswith(GOAL_LINE_PREFIX))
        marker_line = next((line for line in text.splitlines() if ALTERNATIVE_MARKER in line), "")
        choices = eligible[sid]
        cited = next((c.course_id for c in world.courses.values() if c.name in text), "")
        uncited = next(c.course_id for c in world.courses.values()
                       if c.name not in text and c.course_id != cited)
        records.append({
            "student_id": sid,
            "goals": [{"quote": goal_line, "restatement": "希望补齐数据与建模方面的能力"},
                      {"quote": "我在记录里从没写过的一句话", "restatement": "伪造"}],
            "explicit_request": {"course_id": uncited, "quote": "第一志愿是《没点名的课》"},
            # Claims openness for everybody, including the students who never wrote that sentence.
            "accepts_alternatives": {"value": "true", "quote": marker_line},
            "course_relevance": [
                {"course_id": choices[0], "relevance": 0.8, "reason": "服务于该目标",
                 "evidence": [goal_line], "need_class": NEED_CLASS_EXPLICIT, "uncertainty": 0.2},
                {"course_id": "C99", "relevance": 0.9, "reason": "不存在的课",
                 "evidence": [goal_line], "need_class": NEED_CLASS_PREFERENCE, "uncertainty": 0.1},
                {"course_id": choices[1] if len(choices) > 1 else choices[0], "relevance": 5.0,
                 "reason": "越界相关度", "evidence": [goal_line],
                 "need_class": NEED_CLASS_PREFERENCE, "uncertainty": 0.1},
                {"course_id": choices[-1], "relevance": 0.6, "reason": "引用造假",
                 "evidence": ["这句原文不存在"], "need_class": NEED_CLASS_PREFERENCE,
                 "uncertainty": 0.3},
            ],
            "open_questions": ["这些课是否真的符合我的目标"],
        })
    return records, {"prompt_tokens": 100, "completion_tokens": 20}


def hostile_hard_requirements(st, world):
    """What §一 forbids, written by the model itself: delete one real 必修, invent another."""
    rows = hard_requirements(st)
    invented = next(c.course_id for c in world.courses.values()
                    if c.course_id not in st.required_courses and c.course_id not in st.initial_scores)
    return [{"course_id": invented, "evidence": ["培养方案"], "confidence": 1.0, "source": "培养方案"}] \
        + rows[1:]


class RuleReaderFillsEveryField(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=12, seed=3)
        self.skills = narrative_skill_files(self.world)
        self.model = build_need_model_v2(self.world, parser="rule", skills=self.skills)
        self.records = {r["student_id"]: r for r in self.model["records"]}

    def test_all_five_fields_present_and_classes_legal(self) -> None:
        for record in self.model["records"]:
            self.assertTrue(record["goals"], "规则臂也要交付学习目标")
            self.assertIn("explicit_request", record)
            self.assertIn("acceptable_alternatives", record)
            self.assertTrue(record["open_questions"])
            for row in record["course_relevance"]:
                self.assertIn(row["need_class"], NEED_CLASSES)
                if row["need_class"] == NEED_CLASS_EXPLICIT:
                    self.assertEqual(row["course_id"], record["explicit_request"]["course_id"])

    def test_rule_recovers_the_named_course_and_the_openness_clause(self) -> None:
        named = 0
        for sid, record in self.records.items():
            truth_alt = truth_accepts_alternatives(self.skills[sid])
            self.assertEqual(record["acceptable_alternatives"]["verified"], truth_alt)
            self.assertEqual(truth_alt, ALTERNATIVE_MARKER in self.skills[sid])
            if truth_alt:
                self.assertIn(ALTERNATIVE_MARKER, record["acceptable_alternatives"]["quote"])
            wanted = truth_first_choice(self.world.students[sid], self.world)
            if wanted:
                named += 1
                self.assertEqual(record["explicit_request"]["course_id"], wanted)
        self.assertGreater(named, 0, "这份种子没有任何学生点名课程，测试失去意义")
        self.assertLess(named, len(self.records), "所有学生都点名，测不出漏识别")
        scores = score_need_model(self.model["records"], self.world,
                                  truth_need_labels(self.world), self.skills)
        self.assertEqual(scores["class_overclaims_corrected"], 0)
        self.assertEqual(scores["alternative_accuracy_pct"], 100.0)
        self.assertEqual(scores["explicit_request_accuracy_pct"], 100.0)

    def test_goal_quotes_are_verbatim(self) -> None:
        for record in self.model["records"]:
            for goal in record["goals"]:
                text = self.skills[record["student_id"]].replace(" ", "")
                self.assertIn(goal["text"].replace(" ", ""), text)
                self.assertEqual(goal["restatement"], "", "规则臂没有重述能力，不得假装有")


class ModelClaimsAreNotTakenOnTrust(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=12, seed=3)
        self.skills = narrative_skill_files(self.world)
        self.eligible = {sid: labels["eligible"] for sid, labels in truth_need_labels(self.world).items()}
        patcher = mock.patch("semantic_needs_v2._chat_call_v2",
                             side_effect=lambda *a, **k: _fabricated_reply(self.skills, self.eligible,
                                                                           self.world))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.tmp = tempfile.mkdtemp()
        self.model = build_need_model_v2(self.world, parser="llm", llm_settings=SETTINGS,
                                         skills=self.skills, cache_dir=self.tmp)

    def test_every_overclaim_is_rejected_and_named(self) -> None:
        joined = " || ".join(self.model["problems"])
        self.assertIn("引用了不存在的课程 C99", joined)
        self.assertIn("相关度超出 0 到 1", joined)
        self.assertIn("引用的原文在学生记录里找不到", joined)
        self.assertIn("学习目标引用不到学生原文", joined)
        self.assertIn("但原文里没有这门课", joined)
        self.assertIn("已降为一般偏好", joined)
        self.assertIn("声称学生愿意接受替代课程，但原文里找不到这句话", joined)

    def test_kept_rows_are_clean_and_downgraded(self) -> None:
        for record in self.model["records"]:
            sid = record["student_id"]
            audit = record["need_audit"]
            self.assertEqual(audit["goals_kept"], 1)
            self.assertEqual(audit["goals_offered"], 2)
            self.assertEqual(audit["classes_corrected"], 1)
            self.assertFalse(record["explicit_request"]["verified"])
            self.assertEqual(record["explicit_request"]["course_id"], "")
            truth_alt = truth_accepts_alternatives(self.skills[sid])
            self.assertEqual(record["acceptable_alternatives"]["verified"], truth_alt)
            self.assertEqual(record["open_questions"][0]["text"], "这些课是否真的符合我的目标")
            ids = {row["course_id"] for row in record["course_relevance"]}
            self.assertNotIn("C99", ids)
            for row in record["course_relevance"]:
                self.assertTrue(0.0 <= row["relevance"] <= 1.0)
                self.assertNotEqual(row["need_class"], NEED_CLASS_EXPLICIT)
                self.assertIn(row["course_id"], self.eligible[sid])

    def test_shared_bridge_still_consumes_the_records(self) -> None:
        visible, stats = predicted_visible(self.world, self.model["records"])
        self.assertGreater(stats["pairs_added"], 0)
        self.assertEqual(stats["pairs_rejected_unadopted"], 0)
        sid = self.model["records"][0]["student_id"]
        row = self.model["records"][0]["course_relevance"][0]
        self.assertEqual(visible[sid][row["course_id"]], 40 + round(55 * 0.8))

    def test_metrics_are_computed_by_code_not_by_the_reader(self) -> None:
        scores = score_need_model(self.model["records"], self.world, truth_need_labels(self.world),
                                  self.skills)
        self.assertEqual(scores["explicit_request_accuracy_pct"], 0.0)
        self.assertEqual(scores["goal_topic_coverage_pct"], 100.0)
        self.assertEqual(scores["goal_quote_reject_pct"], 50.0)
        self.assertGreater(scores["alternative_missed"] + scores["alternative_hallucinated"], 0)
        self.assertEqual(scores["alternative_accuracy_pct"],
                         round(sum(1 for text in self.skills.values()
                                   if truth_accepts_alternatives(text)) / len(self.skills) * 100, 1))
        self.assertGreater(scores["class_overclaim_rate_pct"], 0.0)
        self.assertEqual(sum(v["rows"] for v in scores["need_class_distribution"].values()),
                         sum(len(r["course_relevance"]) for r in self.model["records"]))


class CacheIsVersionedAndShared(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=8, seed=5)
        self.skills = narrative_skill_files(self.world)
        self.eligible = {sid: labels["eligible"] for sid, labels in truth_need_labels(self.world).items()}
        self.tmp = tempfile.mkdtemp()
        self.calls = 0

        def reply(api_key, base_url, model, batch, catalogue):
            self.calls += 1
            subset = {sid: text for sid, text in self.skills.items() if sid in dict(batch)}
            return _fabricated_reply(subset, self.eligible, self.world)

        patcher = mock.patch("semantic_needs_v2._chat_call_v2", side_effect=reply)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_second_run_pays_nothing_and_stays_out_of_the_v1_file(self) -> None:
        first = build_need_model_v2(self.world, parser="llm", llm_settings=SETTINGS,
                                    skills=self.skills, cache_dir=self.tmp)
        expected_calls = math.ceil(len(self.skills) / 6)
        self.assertEqual(first["cost"]["llm_calls"], expected_calls)
        self.assertEqual(self.calls, expected_calls)
        self.assertEqual(first["cost"]["cache_misses"], len(self.skills))
        second = build_need_model_v2(self.world, parser="llm", llm_settings=SETTINGS,
                                     skills=self.skills, cache_dir=self.tmp)
        self.assertEqual(second["cost"]["llm_calls"], 0)
        self.assertEqual(second["cost"]["cache_hits"], len(self.skills))
        self.assertEqual(self.calls, expected_calls)
        self.assertEqual(second["records"], first["records"])
        path = os.path.join(self.tmp, "semantic_cache", "llm_relevance_v2.jsonl")
        with open(path, encoding="utf-8") as handle:
            versions = {json.loads(line)["version"] for line in handle if line.strip()}
        self.assertEqual(versions, {PARSER_VERSION_V2})

    def test_failure_falls_back_to_rules_and_says_so(self) -> None:
        with mock.patch("semantic_needs_v2.BATCH_RETRY_SECONDS", 0.0), \
                mock.patch("semantic_needs_v2._chat_call_v2", side_effect=RuntimeError("gateway down")):
            model = build_need_model_v2(self.world, parser="llm", llm_settings=SETTINGS,
                                        skills=self.skills, cache_dir=self.tmp)
        self.assertTrue(model["cost"]["llm_fallback"])
        self.assertEqual(model["parser"], "规则解析")
        self.assertTrue(any("回退" in item for item in model["problems"]))
        rule_model = build_need_model_v2(self.world, parser="rule", skills=self.skills)
        self.assertEqual(model["records"], rule_model["records"],
                         "回退行必须逐字段等于规则行，否则报表里的“回退”只是标签")


class TruthArmStaysMarked(unittest.TestCase):
    def test_reference_arm_is_labelled_and_not_deployable(self) -> None:
        world = generate_world(n_students=6, seed=11)
        skills = narrative_skill_files(world)
        model = build_need_model_v2(world, parser="truth", skills=skills)
        self.assertTrue(all(r["hidden_utility_used"] for r in model["records"]))
        self.assertEqual(model["cost"]["llm_calls"], 0)
        scores = score_need_model(model["records"], world, truth_need_labels(world), skills)
        self.assertEqual(scores["explicit_request_accuracy_pct"], 100.0)
        self.assertEqual(scores["alternative_accuracy_pct"], 100.0)
        # 真值臂没有自然语言目标：这三项必须是"不适用"，不得写成 0 分让读者以为它更差。
        self.assertFalse(scores["goal_metrics_applicable"])
        for key in ("goals_per_student", "goal_quote_reject_pct", "goal_topic_coverage_pct"):
            self.assertIsNone(scores[key])

    def test_row_level_explicit_class_is_structurally_unreachable(self) -> None:
        world = generate_world(n_students=6, seed=11)
        skills = narrative_skill_files(world)
        model = build_need_model_v2(world, parser="rule", skills=skills)
        scores = score_need_model(model["records"], world, truth_need_labels(world), skills)
        labels = {row["need_class"] for record in model["records"]
                  for row in record["course_relevance"]}
        # 唯一被学生逐字点名的课是自申报的专业选修，它必被相关性校验器拒绝，所以行级"明确要求"
        # 对三臂都是结构性空集；真明确要求只能由 explicit_request 字段承载。
        self.assertNotIn(NEED_CLASS_EXPLICIT, labels)
        self.assertEqual(scores["need_class_distribution"][NEED_CLASS_EXPLICIT]["rows"], 0)
        named = [r for r in model["records"] if r["explicit_request"]["verified"]]
        self.assertTrue(named, "规则臂应至少有一名学生点过课，否则这条断言是空的")
        for record in named:
            self.assertIn(record["explicit_request"]["course_id"],
                          world.students[record["student_id"]].declared_courses)


class J2PromptCarriesNoHiddenTruth(unittest.TestCase):
    """任务书规定隐藏真值不得喂给 AI；本文件其它用例整体替掉了批次调用，只有这里核对真实请求体。"""

    def setUp(self) -> None:
        self.world = generate_world(n_students=12, seed=3)
        self.skills = narrative_skill_files(self.world)
        self.eligible = {sid: labels["eligible"]
                         for sid, labels in truth_need_labels(self.world).items()}
        self.sentinels = hide_truth_behind_sentinels(self.world)

    def test_batch_size_and_sentinels_are_usable(self) -> None:
        """The instrument itself: 12 students must be more than one call, and the sentinels unique."""
        self.assertGreater(len(self.world.students), STUDENTS_PER_CALL,
                           "学生数不超过单批上限，就测不出跨批次泄漏")
        self.assertEqual(len(self.sentinels), len(set(self.sentinels)))

    def test_every_wire_request_isolates_the_hidden_utility(self) -> None:
        calls = capture_j2_wire(self.world, self.skills, self.eligible, SETTINGS)
        self.assertEqual(len(calls), math.ceil(len(self.skills) / STUDENTS_PER_CALL))
        assert_wire_payload_isolated(self, calls, self.world, self.skills, SETTINGS["api_key"])

    def test_catalogue_and_schema_do_not_smuggle_state(self) -> None:
        """Only the public course fields and the output template may accompany the prose."""
        calls = capture_j2_wire(self.world, self.skills, self.eligible, SETTINGS)
        for _, user in calls:
            for course in user["course_catalogue"]:
                self.assertEqual(set(course), CATALOGUE_KEYS)
            self.assertEqual(user["output_schema"], NEED_MODEL_SCHEMA)
            self.assertNotIn("hard_requirements", json.dumps(user["output_schema"], ensure_ascii=False),
                             "硬约束由规则系统填写，不得出现在要求模型输出的模板里")


class HardConstraintsStayOnTheRuleSide(unittest.TestCase):
    """任务书 §一：必修等硬约束由正式规则决定，LLM 不得推断或改变。

    v1 的保护是事后校验器（``needs.validate_need_model`` 会拒绝伪造的硬性要求），v2 的保护是结构性的：
    ``validate_need_model`` 一律用规则侧重写这个字段。本类钉的是后者——所以它必须能在模型主动上交一份
    伪造清单时仍然成立，而不是只测"模型没提"。
    """

    def setUp(self) -> None:
        self.world = generate_world(n_students=12, seed=3)
        self.skills = narrative_skill_files(self.world)
        self.eligible = {sid: labels["eligible"]
                         for sid, labels in truth_need_labels(self.world).items()}
        for st in self.world.students.values():
            self.assertTrue(st.required_courses, "没有必修可删，本类的两条断言都会空转")

        def reply(api_key, base_url, model, batch, catalogue):
            subset = {sid: text for sid, text in self.skills.items() if sid in dict(batch)}
            records, usage = _fabricated_reply(subset, self.eligible, self.world)
            for record in records:
                record["hard_requirements"] = hostile_hard_requirements(
                    self.world.students[record["student_id"]], self.world)
            return records, usage

        patcher = mock.patch("semantic_needs_v2._chat_call_v2", side_effect=reply)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.llm = build_need_model_v2(self.world, parser="llm", llm_settings=SETTINGS,
                                       skills=self.skills, cache_dir=tempfile.mkdtemp())

    def test_forged_list_neither_adds_nor_deletes_a_required_course(self) -> None:
        self.assertFalse(self.llm["cost"]["llm_fallback"], "回退到规则臂就等于没测到模型伪造的那一份")
        self.assertGreater(self.llm["cost"]["llm_calls"], 0)
        for record in self.llm["records"]:
            st = self.world.students[record["student_id"]]
            forged_rows = hostile_hard_requirements(st, self.world)
            forged = {row["course_id"] for row in forged_rows}
            ids = [row["course_id"] for row in record["hard_requirements"]]
            self.assertNotEqual(ids, [row["course_id"] for row in forged_rows],
                                "夹具与规则输出没有差别，这条断言无从区分伪造")
            self.assertEqual(record["hard_requirements"], hard_requirements(st),
                             "硬性要求必须逐字段等于培养方案的规则输出")
            self.assertEqual(set(ids), set(st.required_courses))
            for forged_cid in forged - set(st.required_courses):
                self.assertNotIn(forged_cid, ids, "模型编造的必修课进了记录")
            self.assertIn(st.required_courses[0], ids, "模型删掉的那门必修没有由规则补回来")

    def test_all_three_arms_publish_the_same_rule_rows(self) -> None:
        rule = build_need_model_v2(self.world, parser="rule", skills=self.skills)
        truth = build_need_model_v2(self.world, parser="truth", skills=self.skills)
        with mock.patch("semantic_needs_v2.BATCH_RETRY_SECONDS", 0.0), \
                mock.patch("semantic_needs_v2._chat_call_v2", side_effect=RuntimeError("gateway down")):
            fallback = build_need_model_v2(self.world, parser="llm", llm_settings=SETTINGS,
                                           skills=self.skills, cache_dir=tempfile.mkdtemp())
        self.assertTrue(fallback["cost"]["llm_fallback"])
        arms = {"I2": rule, "J2": self.llm, "K2": truth, "回退": fallback}
        by_student = {name: {r["student_id"]: r["hard_requirements"] for r in model["records"]}
                      for name, model in arms.items()}
        want = {sid: hard_requirements(st) for sid, st in self.world.students.items()}
        for name, got in by_student.items():
            self.assertEqual(set(got), set(want), f"{name} 臂缺学生")
            for sid, rows in want.items():
                self.assertEqual(got[sid], rows, f"{name} 臂的 {sid} 硬性要求不来自培养方案")

    def test_the_optimizer_bridge_never_reads_the_forged_field(self) -> None:
        """预置课表的可输入面只有相关性行：把字段换成模型伪造的版本，桥接结果一格都不该动。"""
        clean, _ = predicted_visible(self.world, self.llm["records"])
        tampered = [dict(record, hard_requirements=hostile_hard_requirements(
            self.world.students[record["student_id"]], self.world)) for record in self.llm["records"]]
        moved, _ = predicted_visible(self.world, tampered)
        self.assertEqual(clean, moved)


if __name__ == "__main__":
    unittest.main()
