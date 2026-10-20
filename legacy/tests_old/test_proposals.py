"""Semantic need model, proposal validator and hidden-information isolation tests."""
from __future__ import annotations

import inspect
import os
import random
import unittest
from unittest import mock

import llm_controller
import needs as needs_module
import proposals as proposals_module
from institution import SelectionPolicy
from llm_controller import propose_coordination_with_llm
from needs import (
    build_need_model, generate_skill_files, merge_llm_needs, parse_skill, rule_need_model,
    validate_need_model,
)
from planner_core import _is_eligible, generate_world, solve_assignment
from proposals import (
    PROPOSAL_COLUMNS, PROPOSAL_KINDS, Proposal, ProposalRunResult, admissible_courses,
    execute_proposals, generate_rule_proposals, global_state, prune_proposal,
    proposal_metrics_rows, run_proposal_planning, seats_left, validate_proposal,
)


class Forbidden(dict):
    """Any read from the planner side raises, so leakage cannot pass silently."""

    def _blocked(self, *_args, **_kwargs):
        raise AssertionError("规划层读取了隐藏真实偏好")

    __getitem__ = _blocked
    get = _blocked
    keys = _blocked
    values = _blocked
    items = _blocked
    copy = _blocked
    __iter__ = _blocked
    __contains__ = _blocked
    __len__ = _blocked


class SkillRecordTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.world = generate_world(n_students=24, seed=21)
        cls.skills = generate_skill_files(cls.world)

    def test_skill_record_keeps_declared_text_without_numeric_preferences(self):
        for text in self.skills.values():
            self.assertIn("## 学习目标", text)
            self.assertIn("## 硬性要求", text)
            digits = sum(char.isdigit() for char in text.split("## 尚不确定")[0])
            self.assertGreater(digits, 0, "年级与课程编号是文本的一部分")
            self.assertNotIn("true_scores", text)
            self.assertNotIn("隐藏", text)

    def test_parse_skill_splits_declared_sections(self):
        sid = next(iter(self.skills))
        sections = parse_skill(self.skills[sid])
        self.assertIn("学习目标", sections)
        self.assertTrue(sections["硬性要求"])

    def test_rule_need_model_uses_only_visible_fields_and_is_valid(self):
        records = rule_need_model(self.world, self.skills)
        self.assertEqual(len(records), len(self.world.students))
        self.assertEqual(validate_need_model(records, self.world), [])
        for record in records:
            st = self.world.students[record["student_id"]]
            self.assertEqual({item["course_id"] for item in record["hard_requirements"]},
                             set(st.required_courses))
            for item in record["acceptable_alternatives"]:
                self.assertTrue(item["needs_confirmation"])
                self.assertTrue(item["evidence"])
                self.assertLessEqual(item["confidence"], needs_module.CONFIDENCE_CAP)


class NeedModelValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=12, seed=31)
        self.skills = generate_skill_files(self.world)
        self.records = rule_need_model(self.world, self.skills)

    def test_an_inference_can_never_become_a_hard_requirement(self):
        sid = next(iter(self.world.students))
        st = self.world.students[sid]
        extra = next(cid for cid in self.world.courses if cid not in st.required_courses)
        self.records[0]["hard_requirements"].append(
            {"course_id": extra, "evidence": ["原文"], "confidence": 0.9, "source": "LLM 语义推断"})
        problems = validate_need_model(self.records, self.world)
        self.assertTrue(any("不能作为硬性要求" in problem or "来源必须是培养方案" in problem
                            for problem in problems))

    def test_an_unconfirmed_alternative_is_rejected(self):
        self.records[0]["acceptable_alternatives"] and self.records[0]["acceptable_alternatives"][0].update(
            {"needs_confirmation": False})
        self.assertTrue(any("未经学生确认" in problem for problem in validate_need_model(self.records, self.world)))

    def test_llm_alternatives_are_dropped_when_unsupported(self):
        sid = next(iter(self.world.students))
        st = self.world.students[sid]
        record = next(r for r in self.records if r["student_id"] == sid)
        blocked = next(cid for cid, course in self.world.courses.items()
                       if set(course.prerequisites) - set(st.completed_courses))
        covered = {cid for cid in st.declared_courses} | set(st.required_courses) | {
            item["course_id"] for item in record["acceptable_alternatives"]}
        fresh = next(cid for cid in self.world.courses
                     if cid not in covered and not set(self.world.courses[cid].prerequisites) - set(st.completed_courses))
        invented = {"student_id": sid, "acceptable_alternatives": [
            {"course_id": "C99", "reason": "听起来相关", "evidence": ["原文"], "confidence": 0.5},
            {"course_id": blocked, "reason": "先修未满足", "evidence": ["原文"], "confidence": 0.5},
            {"course_id": st.declared_courses[0], "reason": "已经表达过", "evidence": ["原文"], "confidence": 0.5},
            {"course_id": fresh, "reason": "没有引用原文", "evidence": [], "confidence": 0.5},
        ], "open_questions": [{"text": "是否可以替代？", "course_id": ""}]}
        merged, rejected = merge_llm_needs(self.records, [invented], self.world)
        self.assertEqual(len(rejected), 4)
        target = next(r for r in merged if r["student_id"] == sid)
        self.assertNotIn(fresh, [item["course_id"] for item in target["acceptable_alternatives"]])
        self.assertIn("是否可以替代？", [q["text"] for q in target["open_questions"]])


class HiddenInformationIsolationTests(unittest.TestCase):
    def test_planner_modules_do_not_reference_the_hidden_score_field(self):
        for module in (needs_module, proposals_module):
            source = inspect.getsource(module)
            self.assertNotIn("true_scores", source, f"{module.__name__} 不应读取隐藏偏好")
        self.assertNotIn("true_scores", inspect.getsource(propose_coordination_with_llm))

    def test_planning_path_runs_with_hidden_truth_unreadable(self):
        world = generate_world(n_students=24, seed=22)
        for student in world.students.values():
            student.true_scores = Forbidden(student.true_scores)
        policy = SelectionPolicy()
        model = build_need_model(world)
        self.assertEqual(model["problems"], [])
        needs_by_student = {record["student_id"]: record for record in model["records"]}
        visible = {sid: dict(st.initial_scores) for sid, st in world.students.items()}
        assignments, _ = solve_assignment(world, visible)
        state = global_state(world, assignments, visible, needs_by_student, policy, 1, 12, [])
        proposals = generate_rule_proposals(world, state, policy, visible, assignments, set())
        self.assertTrue(proposals)
        for proposal in proposals:
            validate_proposal(proposal, world, policy, visible, assignments, set())


class ProposalSchemaTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=24, seed=23)
        self.policy = SelectionPolicy()
        self.visible = {sid: dict(st.initial_scores) for sid, st in self.world.students.items()}
        self.assignments, _ = solve_assignment(self.world, self.visible)

    def _proposal(self, **overrides) -> Proposal:
        sid = next(iter(self.world.students))
        free = next(cid for cid, course in self.world.courses.items()
                    if course.capacity - sum(cid in chosen for chosen in self.assignments.values()) > 0)
        base = dict(
            proposal_id="P-1", kind="substitute", observed_conflict="热门课程超额",
            candidate_action="向 S 确认替代课程", affected_students=[sid], courses_involved=[free],
            hypotheses_to_verify=["S 愿意接受该替代"],
            verification_actions=[{"student_id": sid, "course_id": free,
                                   "question": "是否愿意修读？", "reason": "有空位"}],
            stop_condition="学生拒绝则放弃",
        )
        base.update(overrides)
        return Proposal(**base)

    def test_only_declared_proposal_kinds_are_allowed(self):
        problems = validate_proposal(self._proposal(kind="assign_seat"), self.world, self.policy,
                                     self.visible, self.assignments, set())
        self.assertTrue(any("不在允许集合内" in problem for problem in problems))
        self.assertTrue(set(PROPOSAL_KINDS))

    def test_export_columns_track_the_rows_the_pipeline_actually_produces(self):
        """app.py exports an empty proposal list with these names; they must match real rows."""
        import io
        import pandas as pd
        used, _, rows, _ = execute_proposals(self.world, [self._proposal()], self.visible,
                                             set(), 0, 8, random.Random(1))
        self.assertEqual(len(rows), 1, "提案没有被执行就比对不了导出列名")
        metrics = proposal_metrics_rows(ProposalRunResult(
            preset={}, visible=self.visible, outcome=None,
            proposals=[{**row, "round": 1, "generator": "规则提案生成器"} for row in rows],
            rejected=[], logs=[], cost={}, stop_reason=""))
        self.assertEqual(list(metrics[0].keys()), PROPOSAL_COLUMNS)
        # 规则规划一层不产生提案：导出必须仍是带表头的可解析 CSV，而不是一个空文件。
        header_only = pd.DataFrame(columns=PROPOSAL_COLUMNS).to_csv(index=False).encode("utf-8-sig")
        self.assertEqual(list(pd.read_csv(io.StringIO(header_only.decode("utf-8-sig"))).columns),
                         PROPOSAL_COLUMNS)

    def test_invented_records_are_rejected(self):
        problems = validate_proposal(
            self._proposal(affected_students=["S999"], courses_involved=["C999"],
                           verification_actions=[{"student_id": "S999", "course_id": "C999",
                                                  "question": "？", "reason": ""}]),
            self.world, self.policy, self.visible, self.assignments, set())
        self.assertTrue(any("不存在的学生" in problem for problem in problems))
        self.assertTrue(any("不存在的课程" in problem for problem in problems))

    def test_a_required_course_can_never_be_proposed_as_a_substitute(self):
        sid = next(s for s, st in self.world.students.items() if st.required_courses)
        required = self.world.students[sid].required_courses[0]
        problems = validate_proposal(
            self._proposal(affected_students=[sid], courses_involved=[required],
                           verification_actions=[{"student_id": sid, "course_id": required,
                                                  "question": "是否接受替代？", "reason": ""}]),
            self.world, self.policy, self.visible, self.assignments, set())
        self.assertTrue(any("必修课程" in problem for problem in problems))

    def test_a_course_that_clashes_with_the_existing_timetable_is_rejected(self):
        sid = next(s for s, chosen in self.assignments.items() if chosen)
        held = self.assignments[sid][0]
        clash = next(cid for cid, course in self.world.courses.items()
                     if course.time_slot == self.world.courses[held].time_slot and cid != held
                     and cid not in self.visible.get(sid, {}))
        problems = validate_proposal(
            self._proposal(affected_students=[sid], courses_involved=[clash],
                           verification_actions=[{"student_id": sid, "course_id": clash,
                                                  "question": "？", "reason": ""}]),
            self.world, self.policy, self.visible, self.assignments, set())
        self.assertTrue(any("时间冲突" in problem or "无需再问" in problem for problem in problems))

    def test_a_payload_that_claims_seats_or_results_is_not_parsed(self):
        self.assertIsNone(Proposal.from_dict({"assigned_seats": {"S001": ["C01"]}}))
        self.assertIsNone(Proposal.from_dict({"realized_utility": 900}))
        self.assertIsNone(Proposal.from_dict({"proposal_id": "P", "kind": "substitute"}))


class ProposalPruningTests(unittest.TestCase):
    """A scheme survives when at least one of its verification actions is legal."""

    @classmethod
    def setUpClass(cls) -> None:
        cls.world = generate_world(n_students=24, seed=25)
        cls.policy = SelectionPolicy()

    def setUp(self) -> None:
        self.visible = {sid: dict(st.initial_scores) for sid, st in self.world.students.items()}
        self.assignments, _ = solve_assignment(self.world, self.visible)

    def _legal_pair(self):
        for sid, st in self.world.students.items():
            if not st.required_courses:
                continue
            held_slots = {self.world.courses[cid].time_slot for cid in self.assignments.get(sid, [])}
            for cid, course in self.world.courses.items():
                if cid in st.required_courses or cid in self.visible.get(sid, {}):
                    continue
                if not _is_eligible(st, course) or course.time_slot in held_slots:
                    continue
                if sum(cid in chosen for chosen in self.assignments.values()) >= course.capacity:
                    continue
                return sid, cid, st.required_courses[0]
        self.fail("这个实例里没有可用的合法/非法动作对，测试需要换一个种子")

    def _proposal(self, sid, actions) -> Proposal:
        return Proposal(
            proposal_id="P-prune", kind="bundle", observed_conflict="热门课程超额",
            candidate_action="同时确认两个方向", affected_students=[sid],
            courses_involved=sorted({a["course_id"] for a in actions}),
            hypotheses_to_verify=["学生愿意调整志愿"], verification_actions=actions,
            stop_condition="学生拒绝则放弃",
        )

    def test_illegal_action_is_dropped_and_the_rest_of_the_scheme_survives(self):
        sid, legal_cid, required_cid = self._legal_pair()
        proposal = self._proposal(sid, [
            {"student_id": sid, "course_id": legal_cid, "question": "是否愿意修读？", "reason": "有空位"},
            {"student_id": sid, "course_id": required_cid, "question": "能否替代必修？", "reason": "内容相近"},
        ])
        self.assertTrue(validate_proposal(proposal, self.world, self.policy, self.visible,
                                         self.assignments, set()), "必修替代必须被严格校验发现")
        kept, faults = prune_proposal(proposal, self.world, self.policy, self.visible,
                                      self.assignments, set())
        self.assertIsNotNone(kept)
        self.assertEqual([a["course_id"] for a in kept.verification_actions], [legal_cid])
        self.assertTrue(any("必修课程" in problem for problem in faults))

    def test_a_scheme_whose_actions_are_all_illegal_is_rejected(self):
        sid, _legal_cid, required_cid = self._legal_pair()
        proposal = self._proposal(sid, [
            {"student_id": sid, "course_id": required_cid, "question": "能否替代必修？", "reason": "内容相近"}])
        kept, faults = prune_proposal(proposal, self.world, self.policy, self.visible,
                                     self.assignments, set())
        self.assertIsNone(kept)
        self.assertTrue(faults)

    def test_structural_faults_cannot_be_repaired_by_pruning(self):
        sid, legal_cid, _ = self._legal_pair()
        proposal = self._proposal(sid, [
            {"student_id": sid, "course_id": legal_cid, "question": "是否愿意修读？", "reason": "有空位"}])
        proposal.kind = "assign_seat"
        kept, faults = prune_proposal(proposal, self.world, self.policy, self.visible,
                                     self.assignments, set())
        self.assertIsNone(kept)
        self.assertTrue(any("不在允许集合内" in problem for problem in faults))

    def test_state_shows_the_facts_the_validator_enforces(self):
        """Otherwise every LLM proposal would fail on information it was never given."""
        model = {record["student_id"]: record for record in build_need_model(self.world)["records"]}
        state = global_state(self.world, self.assignments, self.visible, model, self.policy, 1, 8, [],
                             asked=set())
        self.assertTrue(state["checks_applied_to_every_verification_action"])
        for course in state["courses_with_free_seats"]:
            self.assertIn("prerequisites", course)
            self.assertIn("time_slot", course)
        for record in state["students_with_unmet_needs"]:
            st = self.world.students[record["student_id"]]
            self.assertEqual(sorted(record["completed_course_ids"]), sorted(st.completed_courses))
            self.assertEqual(sorted(record["required_course_ids"]), sorted(st.required_courses))
            self.assertEqual(record["held_course_time_slots"],
                             sorted({self.world.courses[cid].time_slot for cid in record["held_courses"]}))
            self.assertEqual(record["already_disclosed_course_ids"],
                             sorted(self.visible.get(record["student_id"], {})))
            self.assertNotIn("true_scores", record)

    def test_admissible_lists_only_contain_pairs_the_validator_accepts(self):
        remaining = seats_left(self.world, self.assignments)
        checked = 0
        for sid in list(self.world.students)[:10]:
            admissible = admissible_courses(self.world, sid, self.visible, self.assignments, set(), remaining)
            pairs = [(cid, []) for cid in admissible["ask"]]
            pairs += [(item["course_id"], [item["releases_course_id"]]) for item in admissible["swap"]]
            for cid, releases in pairs:
                action = {"student_id": sid, "course_id": cid, "question": "是否愿意修读？", "reason": "有空位"}
                if releases:
                    action["releases_course_ids"] = releases
                kept, faults = prune_proposal(
                    self._proposal(sid, [action]), self.world, self.policy, self.visible,
                    self.assignments, set())
                self.assertIsNotNone(kept, f"{sid} -> {cid} 在可行动清单里却被校验拒绝：{faults}")
                self.assertEqual(faults, [])
                checked += 1
        self.assertGreater(checked, 10, "清单本身不能是空的，否则这一检验没有覆盖任何东西")


class ProposalPipelineTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.world = generate_world(n_students=30, seed=24)
        cls.model = build_need_model(cls.world)

    def test_rule_generator_produces_validated_proposals_without_calling_a_model(self):
        result = run_proposal_planning(self.world, question_budget=10, questions_per_round=4,
                                       generator="规则提案生成器", need_model=self.model)
        self.assertEqual(result.cost["llm_calls"], 0)
        self.assertFalse(result.cost["llm_fallback"])
        self.assertGreater(result.cost["proposals_generated"], 0)
        self.assertGreaterEqual(result.cost["proposals_valid"], result.cost["proposals_selected"])
        self.assertLessEqual(result.cost["questions"], 10)
        self.assertEqual(result.outcome.round_stats["final_violations"], 0)
        self.assertIn(result.cost["solver_status"], {"OPTIMAL", "FEASIBLE", "MODEL_INVALID", "NO_ORTOOLS"})
        self.assertTrue(result.stop_reason)

    def test_executed_proposals_change_the_visible_model_and_the_search(self):
        rng = random.Random(5)
        visible = {sid: dict(st.initial_scores) for sid, st in self.world.students.items()}
        assignments, _ = solve_assignment(self.world, visible)
        state = global_state(self.world, assignments, visible,
                             {r["student_id"]: r for r in self.model["records"]},
                             SelectionPolicy(), 1, 8, [])
        proposals = generate_rule_proposals(self.world, state, SelectionPolicy(), visible, assignments, set())
        legal = [p for p in proposals if not validate_proposal(p, self.world, SelectionPolicy(),
                                                               visible, assignments, set())]
        self.assertTrue(legal)
        asked = set()
        used, rows, execution, bonus = 0, [], [], {}
        used, _, rows, bonus = execute_proposals(self.world, legal[:1], visible, asked, used, 8, rng)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["queries"], len(legal[:1][0].verification_actions))
        for (sid, cid), value in bonus.items():
            self.assertIn(cid, visible[sid], "只有学生确认之后才允许写入可见偏好")
            after, _ = solve_assignment(self.world, visible, bonus)
            self.assertNotIn(cid, assignments[sid] or [])
            self.assertIsInstance(after[sid], list)

    def test_a_missing_llm_is_recorded_as_a_fallback_not_a_result(self):
        # `_endpoint()` 在 settings 缺 key 时会回退读 os.environ，而跑 app.py 的无头 UI 测试
        # 会把 .env 灌进环境变量。不屏蔽的话这条用例会真的发网络请求、花掉配额并"测不到"缺 key 分支。
        env = {k: v for k, v in os.environ.items()
               if k not in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")}
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(llm_controller, "_chat",
                                  side_effect=AssertionError("缺 key 时不应发生网络调用")):
            result = run_proposal_planning(self.world, question_budget=6, questions_per_round=3,
                                           generator="LLM 提案生成器",
                                           llm_settings={"api_key": "", "base_url": "", "model": ""},
                                           need_model=self.model)
        self.assertTrue(result.cost["llm_fallback"])
        self.assertTrue(result.cost["llm_failures"])
        self.assertEqual(result.cost["llm_calls"], 0)


if __name__ == "__main__":
    unittest.main()
