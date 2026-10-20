import copy
import sys
import unittest
from unittest import mock

from planner_core import (
    Course, Student, World, _allocate_greedy_fallback, allocate_full_information,
    allocate_optimized, allocate_serial_lottery, build_query_candidates,
    evaluate_assignments, generate_world, heuristic_select_queries, run_planning,
)


def _assert_no_hard_violations(test, world, assignments):
    metrics = evaluate_assignments(world, assignments)
    test.assertEqual(metrics["capacity_violations"], 0, "课位容量违规必须为零")
    test.assertEqual(metrics["time_conflict_violations"], 0, "最终课表时间冲突违规必须为零")
    test.assertEqual(metrics["eligibility_violations"], 0, "选课资格违规必须为零")
    for sid, chosen in assignments.items():
        test.assertLessEqual(len(chosen), world.students[sid].target_courses)
    return metrics


class EngineInvariantsTests(unittest.TestCase):
    """Hard constraints are owned by the engine, never by a controller."""

    def test_every_mechanism_produces_zero_hard_violations(self):
        world = generate_world(n_students=45, seed=21)
        visible = {sid: dict(st.initial_scores) for sid, st in world.students.items()}
        planned = run_planning(world, question_budget=8, questions_per_round=2)
        arms = {
            "A_现行机制基线": planned.baseline_assignments,
            "B0_一次性优化无交互": allocate_optimized(world, visible),
            "B_规则式集体规划": planned.planned_assignments,
            "D_完整信息参照": allocate_full_information(world),
        }
        for name, assignments in arms.items():
            with self.subTest(arm=name):
                _assert_no_hard_violations(self, world, assignments)

    def test_assignments_respect_capacity_and_time(self):
        world = generate_world(n_students=40, seed=11)
        result = run_planning(world, question_budget=8, questions_per_round=2)
        for assignments in (result.baseline_assignments, result.planned_assignments):
            per_course = {cid: 0 for cid in world.courses}
            for sid, chosen in assignments.items():
                self.assertLessEqual(len(chosen), world.students[sid].target_courses)
                slots = [world.courses[cid].time_slot for cid in chosen]
                self.assertEqual(len(slots), len(set(slots)))
                for cid in chosen:
                    per_course[cid] += 1
            for cid, count in per_course.items():
                self.assertLessEqual(count, world.courses[cid].capacity)


class RuleIndependenceTests(unittest.TestCase):
    """培养要求、志愿候选规则与最终分配规则是三个彼此独立的层。"""

    def setUp(self):
        self.courses = {
            "S1": Course("S1", "同时段甲课", "专业选修", 1, 1, ["算法"]),
            "S2": Course("S2", "同时段乙课", "专业选修", 1, 5, ["算法"]),
            "R3": Course("R3", "需先修甲课", "专业选修", 2, 5, ["算法"], prerequisites=["S1"]),
            "H4": Course("H4", "未申报课程", "通识选修", 3, 5, ["算法"]),
        }
        self.student = Student(
            student_id="T1", year=3, priority_group="其他学生", priority_tier=1,
            interests=["算法"], target_courses=3, required_courses=["H4"],
            declared_courses=["S1", "S2"], completed_courses=[],
            true_scores={"S1": 90, "S2": 10, "R3": 95, "H4": 99},
            # 志愿分数只覆盖已申报课程：H4 与 R3 对规划系统不可见。
            initial_scores={"S1": 90, "S2": 85},
        )
        self.world = World(seed=3, courses=self.courses, students={"T1": self.student})

    def test_unregistered_course_is_never_assigned_even_if_required(self):
        assignments = allocate_optimized(self.world, {"T1": dict(self.student.initial_scores)})
        self.assertNotIn("H4", assignments["T1"], "未申报（分数不可见）的课程不得被分配")
        self.assertNotIn("R3", assignments["T1"])

    def test_ineligible_required_course_is_never_assigned(self):
        # R3 需要 S1 作为先修课；把分数披露出来也不应绕过资格规则。
        visible = {"T1": {"S1": 10, "S2": 1, "R3": 100, "H4": 100}}
        for assignments in (allocate_optimized(self.world, visible),
                            allocate_serial_lottery(self.world, visible, seed=1)):
            self.assertNotIn("R3", assignments["T1"], "先修课未完成的课程属于资格违规")

    def test_timetable_conflict_blocks_one_of_two_same_slot_choices(self):
        assignments = allocate_optimized(self.world, {"T1": dict(self.student.initial_scores)})
        slots = [self.courses[cid].time_slot for cid in assignments["T1"]]
        self.assertEqual(len(slots), len(set(slots)), "同类型志愿允许冲突，最终课表不允许")
        self.assertLessEqual(assignments["T1"].count("S1") + assignments["T1"].count("S2"), 1)

    def test_capacity_is_enforced_across_students(self):
        st2 = copy.deepcopy(self.student)
        st2.student_id = "T2"
        st2.required_courses = []
        world = World(seed=3, courses=self.courses, students={"T1": self.student, "T2": st2})
        visible = {"T1": {"S1": 90}, "T2": {"S1": 95}}
        for assignments in (allocate_optimized(world, visible),
                            allocate_serial_lottery(world, visible, seed=7)):
            self.assertEqual(assignments["T1"].count("S1") + assignments["T2"].count("S1"), 1,
                             "容量为 1 的课程只能分配给一人")


class GreedyFallbackTests(unittest.TestCase):
    """OR-Tools 缺失或求解失败时的贪心回退必须遵守与 CP-SAT 相同的硬约束。"""

    def setUp(self):
        self.courses = {
            "F1": Course("F1", "热门课甲", "专业选修", 1, 1, ["算法"]),
            "F2": Course("F2", "热门课乙", "专业选修", 1, 5, ["算法"]),
            "F3": Course("F3", "需先修甲", "专业选修", 2, 5, ["算法"], prerequisites=["F1"]),
            "F4": Course("F4", "通识课", "通识选修", 3, 5, ["数据结构"]),
        }
        self.students = {
            "T1": Student(
                student_id="T1", year=3, priority_group="毕业年级", priority_tier=1,
                interests=["算法"], target_courses=2, required_courses=["F4"],
                completed_courses=[], declared_courses=["F1", "F2", "F3", "F4"],
                true_scores={"F1": 95, "F2": 20, "F3": 90, "F4": 80},
                initial_scores={"F1": 95, "F2": 60, "F3": 99, "F4": 80}),
            "T2": Student(
                student_id="T2", year=2, priority_group="其他学生", priority_tier=2,
                interests=["算法"], target_courses=3, required_courses=[],
                completed_courses=[], declared_courses=["F1", "F2", "F3"],
                true_scores={"F1": 90, "F2": 85, "F3": 70, "F4": 10},
                initial_scores={"F1": 90, "F2": 85, "F3": 70}),
        }
        self.world = World(seed=4, courses=self.courses, students=self.students)
        self.visible = {sid: dict(st.initial_scores) for sid, st in self.students.items()}

    def test_fallback_keeps_capacity_time_eligibility_and_load(self):
        assignments = _allocate_greedy_fallback(self.world, self.visible)
        _assert_no_hard_violations(self, self.world, assignments)
        self.assertEqual(assignments["T1"].count("F1") + assignments["T2"].count("F1"), 1,
                         "容量为 1 的课程在回退路径上同样只能给一人")
        self.assertNotIn("F3", assignments["T1"], "先修课未完成的课程即使分数最高也不可分配")
        self.assertNotIn("F3", assignments["T2"], "先修课未完成的课程即使分数最高也不可分配")
        self.assertNotIn("F4", assignments["T2"], "未披露分数的课程不得进入分配")

    def test_engine_routes_to_fallback_when_ortools_is_absent(self):
        blocked = {"ortools": None, "ortools.sat": None, "ortools.sat.python": None}
        with mock.patch.dict(sys.modules, blocked):
            assignments = allocate_optimized(self.world, self.visible)
        self.assertEqual(assignments, _allocate_greedy_fallback(self.world, self.visible))
        _assert_no_hard_violations(self, self.world, assignments)

    def test_fallback_is_deterministic_and_blind_to_hidden_truth(self):
        world = generate_world(n_students=45, seed=21)
        visible = {sid: dict(st.initial_scores) for sid, st in world.students.items()}
        first = _allocate_greedy_fallback(world, visible)
        _assert_no_hard_violations(self, world, first)
        self.assertEqual(first, _allocate_greedy_fallback(world, visible), "回退算法必须是确定性的")
        scrambled = copy.deepcopy(world)
        for st in scrambled.students.values():
            st.true_scores = {cid: 100 - score for cid, score in st.true_scores.items()}
        self.assertEqual(first, _allocate_greedy_fallback(scrambled, visible),
                         "隐藏真值变化却改变了回退结果，说明引擎读取了私有信息")


class CandidateGeneratorPublicOnlyTests(unittest.TestCase):
    """候选问题只能由公开信息推导，且必须是真正可执行的选项。"""

    def setUp(self):
        self.world = generate_world(n_students=48, seed=5)
        self.visible = {sid: dict(st.initial_scores) for sid, st in self.world.students.items()}
        self.assignments = allocate_optimized(self.world, self.visible)
        self.candidates = build_query_candidates(self.world, self.assignments, self.visible, set())

    def test_no_candidates_without_a_public_rationale(self):
        self.assertTrue(self.candidates, "扩宽后的候选生成器应能找到公开可解释的问询机会")

    def test_candidates_ignore_hidden_utilities(self):
        scrambled = copy.deepcopy(self.world)
        for st in scrambled.students.values():
            st.true_scores = {cid: 100 - score for cid, score in st.true_scores.items()}
        other = build_query_candidates(scrambled, self.assignments, self.visible, set())
        self.assertEqual([q["query_id"] for q in self.candidates], [q["query_id"] for q in other],
                         "隐藏真值发生变化却改变了候选，说明控制器读取了私有信息")

    def test_candidates_only_offer_feasible_unseen_alternatives(self):
        remaining = {cid: c.capacity for cid, c in self.world.courses.items()}
        for chosen in self.assignments.values():
            for cid in chosen:
                remaining[cid] -= 1
        for query in self.candidates:
            sid, alt_id = query["student_id"], query["alternative_course_id"]
            st = self.world.students[sid]
            with self.subTest(query=query["query_id"]):
                self.assertNotIn(alt_id, self.visible[sid], "不应重复询问已知偏好的课程")
                self.assertNotIn(alt_id, self.assignments[sid], "不应询问已分配的课程")
                self.assertNotIn(alt_id, st.required_courses, "培养要求不需要通过问询发现")
                self.assertGreater(remaining[alt_id], 0, "无空位的课程不构成可行选项")
                self.assertTrue(set(self.world.courses[alt_id].prerequisites) <= set(st.completed_courses))
                used_slots = {
                    self.world.courses[cid].time_slot for cid in self.assignments[sid]
                }
                self.assertNotIn(self.world.courses[alt_id].time_slot, used_slots,
                                 "与现有课表时间冲突的替代方案不可执行")

    def test_asked_pairs_are_not_regenerated(self):
        first = self.candidates[0]
        asked = {(first["student_id"], first["alternative_course_id"])}
        again = build_query_candidates(self.world, self.assignments, self.visible, asked)
        self.assertNotIn(first["query_id"], [q["query_id"] for q in again])


class DeterminismTests(unittest.TestCase):
    def test_deterministic_seed(self):
        w1 = generate_world(n_students=32, seed=5)
        w2 = generate_world(n_students=32, seed=5)
        r1 = run_planning(w1, question_budget=6, questions_per_round=2)
        r2 = run_planning(w2, question_budget=6, questions_per_round=2)
        self.assertEqual(r1.baseline_assignments, r2.baseline_assignments)
        self.assertEqual(r1.planned_assignments, r2.planned_assignments)
        self.assertEqual(r1.baseline_metrics, r2.baseline_metrics)

    def test_candidate_order_is_stable(self):
        world = generate_world(n_students=30, seed=9)
        visible = {sid: dict(st.initial_scores) for sid, st in world.students.items()}
        assignments = allocate_optimized(world, visible)
        first = build_query_candidates(world, assignments, visible, set())
        second = build_query_candidates(world, assignments, visible, set())
        self.assertEqual([q["query_id"] for q in first], [q["query_id"] for q in second])


class LoopAccountingTests(unittest.TestCase):
    def test_budget_and_informative_counting(self):
        world = generate_world(n_students=40, seed=13)
        result = run_planning(world, question_budget=7, questions_per_round=3)
        self.assertLessEqual(result.cost["questions"], 7)
        self.assertLessEqual(result.cost["informative_questions"], result.cost["questions"])
        self.assertGreaterEqual(result.cost["rounds"], 1)
        self.assertLessEqual(result.cost["rounds"], 7)

    def test_every_asked_question_is_logged(self):
        world = generate_world(n_students=36, seed=17)
        result = run_planning(world, question_budget=6, questions_per_round=2)
        asked_logs = [row for row in result.logs if row.get("action") == "向学生确认替代方案"]
        self.assertEqual(len(asked_logs), result.cost["questions"], "问答日志必须可追溯")
        for row in asked_logs:
            self.assertTrue(row["question"] and row["student_id"])
            self.assertIn(row["accepted"], ("是", "否"))

    def test_round_effects_report_actual_utility_delta(self):
        world = generate_world(n_students=40, seed=19)
        result = run_planning(world, question_budget=8, questions_per_round=4)
        # rounds 统计进入过的轮次；round_effects 只记录重新求解过的轮次。
        self.assertLessEqual(len(result.round_effects), result.cost["rounds"])
        self.assertGreaterEqual(len(result.round_effects), 1)
        for effect in result.round_effects:
            for key in ("delta_total_utility", "changed_students", "informative_this_round",
                        "candidates_available"):
                self.assertIn(key, effect)

    def test_no_opportunity_stops_cleanly(self):
        world = World(
            seed=1,
            courses={"S1": Course("S1", "唯一课程", "专业选修", 1, 5, ["算法"])},
            students={
                "T1": Student("T1", 3, "其他学生", 1, ["算法"], 1, [], ["S1"], [],
                              {"S1": 90}, {"S1": 90}),
            },
        )
        result = run_planning(world, question_budget=5, questions_per_round=2)
        self.assertEqual(result.cost["questions"], 0)
        self.assertTrue(any("没有可用查询候选" in str(row.get("note", "")) for row in result.logs))

    def test_heuristic_selector_respects_budget_and_dedups_students(self):
        world = generate_world(n_students=48, seed=23)
        visible = {sid: dict(st.initial_scores) for sid, st in world.students.items()}
        candidates = build_query_candidates(
            world, allocate_optimized(world, visible), visible, set())
        picked = heuristic_select_queries(candidates, 4)
        self.assertLessEqual(len(picked), 4)
        self.assertEqual(len({q["student_id"] for q in picked}), len(picked))


if __name__ == "__main__":
    unittest.main()
