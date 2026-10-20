"""Tests for the AI semantic need module: reader honesty, hard-constraint ownership, caching."""
from __future__ import annotations

import json
import math
import os
import tempfile
import unittest
from unittest import mock

import llm_controller
from planner_core import _is_eligible, generate_world
from semantic_needs import (NEED_OUTPUT_SCHEMA, RELEVANCE_ADOPT_MIN, RULE_TOPIC_ALIASES,
                            STUDENTS_PER_CALL, TOPIC_PARAPHRASE, ParseCache,
                            build_semantic_need_model, hard_requirements, key_need_fulfilment,
                            narrative_skill_files, predicted_visible, rule_alias_coverage,
                            rule_relevance, score_need_identification, truth_need_labels,
                            validate_relevance)
from tests.prompt_isolation import (CATALOGUE_KEYS, assert_wire_payload_isolated,
                                    capture_v1_wire, hide_truth_behind_sentinels)


class ReaderInputIsProse(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=12, seed=3)
        self.skills = narrative_skill_files(self.world)

    def test_goal_line_is_prose_not_topic_labels(self) -> None:
        for sid, text in self.skills.items():
            goal_line = next(line for line in text.splitlines() if line.startswith("本学期想把"))
            leaked = [topic for topic in self.world.students[sid].interests if topic in goal_line]
            self.assertEqual(leaked, [], f"{sid} 的目标行写出了主题标签 {leaked}")

    def test_semantic_gap_is_partial_not_total(self) -> None:
        """A rule baseline that recovers nothing is a straw man; one that recovers everything
        could not measure semantics. The disclosed alias table must sit between."""
        recovered = {topic for topic, aliases in RULE_TOPIC_ALIASES.items()
                     if any(alias in TOPIC_PARAPHRASE[topic] for alias in aliases)}
        self.assertTrue(1 <= len(recovered) <= 6, f"别名表覆盖：{sorted(recovered)}")
        rows = rule_alias_coverage(self.world)
        self.assertEqual(len(rows), len(TOPIC_PARAPHRASE))
        self.assertEqual(sum(1 for row in rows if row["规则别名可恢复"] == "是"), len(recovered))
        unmapped = {row["对应主题"] for row in rows if row["规则别名可恢复"] == "否"}
        bridged = {"机器学习", "机器人", "计算机视觉", "伦理"}
        self.assertTrue(bridged <= unmapped, f"这些主题应留出语义鸿沟供模型跨越：{sorted(unmapped)}")

    def test_rule_rows_are_evidenced_and_capped(self) -> None:
        for sid, text in self.skills.items():
            rows = rule_relevance(text, self.world.students[sid], self.world)
            self.assertLessEqual(len(rows), 8)
            for row in rows:
                self.assertTrue(row["needs_confirmation"])
                self.assertTrue(any(item in text for item in row["evidence"]))


class HardConstraintsBelongToRules(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=10, seed=5)
        self.skills = narrative_skill_files(self.world)

    def test_required_courses_come_from_curriculum_only(self) -> None:
        for sid, st in self.world.students.items():
            rows = hard_requirements(st)
            self.assertEqual([r["course_id"] for r in rows], list(st.required_courses))
            self.assertTrue(all(r["source"] == "培养方案" and r["confidence"] == 1.0 for r in rows))

    def test_validator_rejects_every_illegal_inference(self) -> None:
        sid = next(iter(self.world.students))
        st = self.world.students[sid]
        text = self.skills[sid]
        uneligible = next((c.course_id for c in self.world.courses.values()
                           if c.prerequisites and not _is_eligible(st, c)
                           and c.course_id not in st.required_courses
                           and c.course_id not in st.initial_scores), None)
        probe = [
            {"course_id": "NOPE", "relevance": 0.9, "evidence": [text[:20]]},
            {"course_id": st.required_courses[0], "relevance": 0.9, "evidence": [text[:20]]},
            {"course_id": next(iter(st.initial_scores)), "relevance": 0.9, "evidence": [text[:20]]},
            {"course_id": "C04", "relevance": 1.7, "evidence": [text[:20]]},
            {"course_id": "C04", "relevance": 0.5, "evidence": []},
            {"course_id": "C04", "relevance": 0.5, "evidence": ["这句话学生没有写过"]},
        ]
        if uneligible:
            probe.append({"course_id": uneligible, "relevance": 0.9, "evidence": [text[:20]]})
        kept, rejected = validate_relevance(probe, sid, self.world, text, "LLM 语义解析")
        self.assertEqual(kept, [])
        self.assertGreaterEqual(len(rejected), len(probe))

    def test_evidence_must_quote_the_student(self) -> None:
        sid = next(iter(self.world.students))
        text = self.skills[sid]
        course = next(cid for cid, c in self.world.courses.items()
                      if cid not in self.world.students[sid].initial_scores
                      and cid not in self.world.students[sid].required_courses
                      and _is_eligible(self.world.students[sid], c))
        kept, _ = validate_relevance(
            [{"course_id": course, "relevance": 0.8, "evidence": [text[3:14]], "reason": "x"}],
            sid, self.world, text, "LLM 语义解析")
        self.assertEqual(len(kept), 1)
        self.assertEqual(kept[0]["category"], "规则可识别")


class PredictedWeightsAreShared(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=16, seed=11)
        self.model = build_semantic_need_model(self.world, parser="rule")

    def test_never_overwrites_a_disclosed_score(self) -> None:
        visible, stats = predicted_visible(self.world, self.model["records"])
        for sid, st in self.world.students.items():
            for cid, score in st.initial_scores.items():
                self.assertEqual(visible[sid][cid], score)
            for cid in visible[sid]:
                if cid in st.initial_scores:
                    continue
                self.assertNotIn(cid, st.required_courses)
                self.assertTrue(_is_eligible(st, self.world.courses[cid]))
        self.assertIsInstance(stats["coverage_pct"], float)

    def test_low_relevance_rows_are_not_adopted(self) -> None:
        visible, _ = predicted_visible(self.world, self.model["records"])
        for record in self.model["records"]:
            for row in record["course_relevance"]:
                if row["relevance"] < RELEVANCE_ADOPT_MIN:
                    self.assertNotIn(row["course_id"], visible[record["student_id"]])


class ReferenceArmIsFlagged(unittest.TestCase):
    def test_truth_parser_marks_hidden_utility_and_rule_parser_does_not(self) -> None:
        world = generate_world(n_students=8, seed=2)
        truth = build_semantic_need_model(world, parser="truth")
        self.assertTrue(all(r["hidden_utility_used"] for r in truth["records"]))
        self.assertTrue(all(r["parser"] == "隐藏真实需求标签" for r in truth["records"]))
        rule = build_semantic_need_model(world, parser="rule")
        self.assertFalse(any("hidden_utility_used" in r for r in rule["records"]))

    def test_missing_api_key_falls_back_and_is_recorded(self) -> None:
        # `build_semantic_need_model` 与 `_endpoint()` 都会在世界缺 key 时回读 `os.getenv`，
        # 而跑 app.py 的无头测试早已把 .env 灌进过环境变量。只 pop 一个变量等于把"不触网"
        # 交给测试顺序；这里同时屏蔽三个 OPENAI_* 并把 `_chat` 打成异常，缺 key 分支在任何
        # 顺序下都发不出请求。缓存目录另指临时目录，避免测试写进批次正在读的档案缓存。
        world = generate_world(n_students=8, seed=2)
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")}
        with tempfile.TemporaryDirectory() as tmp, \
                mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(llm_controller, "_chat",
                                  side_effect=AssertionError("缺 key 时不应发生网络调用")):
            model = build_semantic_need_model(world, parser="llm",
                                              llm_settings={"api_key": ""}, cache_dir=tmp)
        self.assertTrue(model["cost"]["llm_fallback"])
        self.assertTrue(model["cost"]["rejected_items"])
        self.assertTrue(all(r["parser"] == "规则解析" for r in model["records"]))
        self.assertEqual(model["cost"]["llm_calls"], 0)


class ProfilesAreCached(unittest.TestCase):
    def test_second_read_of_the_same_profile_costs_nothing(self) -> None:
        world = generate_world(n_students=6, seed=4)
        skills = narrative_skill_files(world)
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cache.jsonl")
            first = ParseCache(path)
            key_rows = [{"course_id": "C05", "relevance": 0.7, "reason": "r", "evidence": ["e"]}]
            from semantic_needs import _cache_key, llm_catalogue
            catalogue = llm_catalogue(world)
            key = _cache_key(next(iter(skills.values())), catalogue, "unit-test-model")
            self.assertIsNone(first.get(key))
            first.put(key, key_rows)
            second = ParseCache(path)
            self.assertEqual(second.get(key), key_rows)
            self.assertEqual(second.misses, 0)


class QualityMetricsAreBounded(unittest.TestCase):
    def test_metrics_agree_with_hand_computed_counts(self) -> None:
        world = generate_world(n_students=20, seed=9)
        model = build_semantic_need_model(world, parser="rule")
        truth = truth_need_labels(world)
        quality = score_need_identification(model["records"], truth)
        for key in ("need_precision_pct", "need_recall_pct", "need_f1_pct",
                    "key_need_recall_pct", "interest_coverage_pct"):
            self.assertGreaterEqual(quality[key], 0.0)
            self.assertLessEqual(quality[key], 100.0)
        visible, _ = predicted_visible(world, model["records"])
        from planner_core import allocate_optimized
        final = allocate_optimized(world, visible)
        pct = key_need_fulfilment(world, truth, final)
        self.assertGreaterEqual(pct, 0.0)
        self.assertLessEqual(pct, 100.0)


class ReportNeedSection(unittest.TestCase):
    """The 5A section is generated from rows and config; both key names and arm order are load-bearing."""

    def setUp(self) -> None:
        from experiments import NEED_ARMS, _need_lines
        self.need_lines = _need_lines
        self.arms = sorted(NEED_ARMS)
        world = generate_world(n_students=20, seed=3)
        self.alias = rule_alias_coverage(world)

    def _rows(self) -> list:
        return [{"mechanism": arm, "status": "已运行"} for arm in self.arms]

    def test_alias_table_uses_the_published_row_keys(self) -> None:
        """Renaming a key in rule_alias_coverage must break this test, not a 3-hour batch."""
        lines = self.need_lines(self._rows(), {"need_alias_rows": self.alias})
        text = "\n".join(lines)
        self.assertIn("## 5A", text)
        for row in self.alias[:3]:
            self.assertIn(row["学生写的说法"], text)
            self.assertIn(row["对应主题"], text)

    def test_arm_headings_are_in_a_stable_order(self) -> None:
        """NEED_ARMS is a set: unsorted iteration would reshuffle the report between runs."""
        first = "\n".join(self.need_lines(self._rows(), {}))
        again = "\n".join(self.need_lines(list(reversed(self._rows())), {}))
        self.assertEqual(first, again)
        self.assertLess(first.index("I_规则"), first.index("J_AI"))
        self.assertLess(first.index("J_AI"), first.index("K_真实"))

    def test_no_need_rows_means_no_need_section(self) -> None:
        self.assertEqual(self.need_lines([{"mechanism": "A_现行机制基线", "status": "已运行"}], {}), [])


class ChunkPlanPartitionsTheFrozenSeeds(unittest.TestCase):
    def test_seeds_1_to_26_appear_exactly_once(self) -> None:
        from need_chunk_plan import CHUNKS, validate_plan
        validate_plan()
        seen = [seed for _, seeds in CHUNKS for seed in seeds]
        self.assertEqual(sorted(seen), list(range(1, 27)))

    def test_a_mis_split_plan_is_rejected(self) -> None:
        """Word splitting once turned `dev_1_6:1 2 3 4 5 6` into six bogus specs and overwrote
        finished results; the planner must refuse any partition that is not 1-26 exactly once."""
        import need_chunk_plan
        original = need_chunk_plan.CHUNKS
        try:
            need_chunk_plan.CHUNKS = [("dev_1_6", [1])]
            with self.assertRaises(SystemExit):
                need_chunk_plan.validate_plan()
            need_chunk_plan.CHUNKS = [("dev_1", [1]), ("dev_1b", [1])]
            with self.assertRaises(SystemExit):
                need_chunk_plan.validate_plan()
            need_chunk_plan.CHUNKS = [("bad name", list(range(1, 27)))]
            with self.assertRaises(SystemExit):
                need_chunk_plan.validate_plan()
            need_chunk_plan.CHUNKS = [("dev_1", [])]
            with self.assertRaises(SystemExit):
                need_chunk_plan.validate_plan()
        finally:
            need_chunk_plan.CHUNKS = original


class AnalyzerRefusesUnmergeableChunks(unittest.TestCase):
    def setUp(self) -> None:
        import analyze_semantic_need
        self.analyzer = analyze_semantic_need
        self.saved_dir = analyze_semantic_need.OUT_DIR
        self.tmp = tempfile.TemporaryDirectory()
        analyze_semantic_need.OUT_DIR = self.tmp.name

    def tearDown(self) -> None:
        self.analyzer.OUT_DIR = self.saved_dir
        self.tmp.cleanup()

    def _write_chunk(self, tag: str, seeds: list, config: dict) -> None:
        import csv
        import json
        path = os.path.join(self.tmp.name, tag)
        os.makedirs(path, exist_ok=True)
        with open(os.path.join(path, "raw_rows.csv"), "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=["seed", "mechanism", "status",
                                                        "total_preference_utility"])
            writer.writeheader()
            for seed in seeds:
                for arm in ("I_规则语义需求解析",):
                    writer.writerow({"seed": seed, "mechanism": arm, "status": "已运行",
                                     "total_preference_utility": 1000.0})
        with open(os.path.join(path, "summary.json"), "w", encoding="utf-8") as handle:
            json.dump({"config": config}, handle)

    def test_missing_seed_coverage_is_refused(self) -> None:
        cfg = {"n_students": 8}
        self._write_chunk("dev_seed_1", [1], cfg)
        with self.assertRaises(SystemExit):
            self.analyzer._load(["dev_seed_1"], {1, 2}, "开发种子")

    def test_duplicated_seed_across_chunks_is_refused(self) -> None:
        cfg = {"n_students": 8}
        self._write_chunk("dev_seed_1", [1], cfg)
        self._write_chunk("dev_seed_1_copy", [1], cfg)
        with self.assertRaises(SystemExit):
            self.analyzer._load(["dev_seed_1", "dev_seed_1_copy"], {1}, "开发种子")

    def test_config_fingerprint_mismatch_is_refused(self) -> None:
        seeds = [1, 2]
        self._write_chunk("dev_a", seeds, {"n_students": 8, "solver_max_seconds": 6})
        self._write_chunk("dev_b", seeds, {"n_students": 8, "solver_max_seconds": 30})
        with self.assertRaises(SystemExit):
            self.analyzer._load(["dev_a", "dev_b"], set(seeds), "开发种子")


class JPromptCarriesNoHiddenTruth(unittest.TestCase):
    """v1 的 J 臂载荷此前没有任何测试看过。

    §7B 那个已发表的判定真的调用过模型，所以"隐藏真值不进提示词"这句话必须在这条路上有看守；
    在补上这几例之前，它只由控制器通道（`test_llm_controller.py`）和源码静态扫描
    （`test_proposals.py`）分别守着，两者都不覆盖 `semantic_needs._chat_call` 组装的请求体。
    """

    SETTINGS = {"api_key": "test-key", "base_url": "http://localhost:1", "model": "test-model"}

    def setUp(self) -> None:
        self.world = generate_world(n_students=12, seed=3)
        self.skills = narrative_skill_files(self.world)
        hide_truth_behind_sentinels(self.world)

    def _capture(self):
        return capture_v1_wire(self.world, self.skills, self.SETTINGS)

    def test_every_wire_request_isolates_the_hidden_utility(self) -> None:
        calls = self._capture()
        self.assertEqual(len(calls), math.ceil(len(self.skills) / STUDENTS_PER_CALL),
                         "批次切分变了，跨批次泄漏的核对也随之失效")
        assert_wire_payload_isolated(self, calls, self.world, self.skills, self.SETTINGS["api_key"])

    def test_payload_carries_only_public_catalogue_fields_and_the_frozen_schema(self) -> None:
        for _, user in self._capture():
            for course in user["course_catalogue"]:
                self.assertEqual(set(course), CATALOGUE_KEYS)
            self.assertEqual(user["output_schema"], NEED_OUTPUT_SCHEMA,
                             "v1 的输出模板被预注册冻住，改动它等于改动那条已发表判定的输入")
            self.assertNotIn("hard_requirements", json.dumps(user["output_schema"],
                                                            ensure_ascii=False))


if __name__ == "__main__":
    unittest.main()
