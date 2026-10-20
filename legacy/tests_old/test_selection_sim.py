"""Two-round selection simulation and solver-decoupling tests."""
from __future__ import annotations

import unittest

from institution import SelectionPolicy
from planner_core import generate_world, solve_assignment
from selection_sim import (
    BehaviorModel, baseline_preset, build_preference_lists, check_final_constraints,
    simulate_selection,
)


class SelectionPolicyTests(unittest.TestCase):
    def test_unknown_rule_key_is_rejected_instead_of_silently_ignored(self):
        with self.assertRaises(ValueError):
            SelectionPolicy.from_config({"max_preferences_per_cours": 3})

    def test_final_timetable_conflict_cannot_be_switched_off(self):
        with self.assertRaises(ValueError):
            SelectionPolicy.from_config({"final_timetable_conflict_allowed": True})

    def test_every_rule_declares_its_provenance(self):
        rows = SelectionPolicy().rules_rows()
        self.assertEqual(len(rows), len(SelectionPolicy.KNOWN_FIELDS))
        self.assertTrue(all(row["依据"] and row["说明"] for row in rows))
        self.assertTrue(all(row["依据"] != "已核实" for row in rows),
                        "本项目没有官方核实过的选课规则，任何规则都必须标注为假设或待核实")


class TwoRoundSimulationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.world = generate_world(n_students=36, seed=5)
        cls.policy = SelectionPolicy()

    def _preset_from_optimizer(self):
        visible = {sid: dict(st.initial_scores) for sid, st in self.world.students.items()}
        assignments, _ = solve_assignment(self.world, visible)
        return assignments

    def test_final_timetable_respects_every_hard_constraint(self):
        for preset in (baseline_preset(self.world, self.policy), self._preset_from_optimizer()):
            outcome = simulate_selection(self.world, preset, self.policy, seed=91)
            for cid, chosen in outcome.final_assignments.items():
                self.assertLessEqual(len(chosen), self.policy.course_limit_for(self.world.students[cid].target_courses))
            metrics = outcome.metrics
            self.assertEqual(metrics["capacity_violations"], 0)
            self.assertEqual(metrics["time_conflict_violations"], 0)
            self.assertEqual(metrics["eligibility_violations"], 0)
            self.assertEqual(metrics["load_violations"], 0)
            self.assertEqual(outcome.round_stats["final_violations"], 0)
            for cid, course in self.world.courses.items():
                taken = sum(cid in chosen for chosen in outcome.final_assignments.values())
                self.assertLessEqual(taken, course.capacity)

    def test_preset_that_does_not_occupy_seats_still_competes_for_capacity(self):
        preset = self._preset_from_optimizer()
        outcome = simulate_selection(self.world, preset, self.policy, BehaviorModel(), seed=13)
        held = sum(len(v) for v in outcome.final_assignments.values())
        self.assertLessEqual(held, sum(c.capacity for c in self.world.courses.values()))
        self.assertGreaterEqual(outcome.preset_stats["preset_keep_pct"] or 0, 0)

    def test_preset_occupying_seats_reserves_the_recommended_course(self):
        policy = SelectionPolicy.from_config({"preset_occupies_seat": True})
        preset = self._preset_from_optimizer()
        outcome = simulate_selection(self.world, preset, policy, BehaviorModel(), seed=13)
        for sid, chosen in preset.items():
            kept = sum(1 for cid in chosen if cid in outcome.final_assignments[sid])
            self.assertGreaterEqual(kept, 1, "占位型预置至少应保留一个课位")
        self.assertLessEqual(outcome.round_stats["round2_grants"],
                             sum(len(v) for v in outcome.final_assignments.values()))

    def test_students_may_drop_a_preset_course_they_do_not_value(self):
        behavior = BehaviorModel(preset_off_cutoff_keep_rate=0.0)
        worst = min(self.world.students, key=lambda sid: sum(self.world.students[sid].true_scores.values()))
        st = self.world.students[worst]
        low = sorted(st.true_scores, key=lambda cid: st.true_scores[cid])[0]
        if low in st.required_courses:
            self.skipTest("该生的最低分课程恰好是必修，不会被撤销")
        outcome = simulate_selection(self.world, {worst: [low]}, self.policy, behavior, seed=7)
        self.assertNotIn(low, outcome.final_assignments[worst])
        self.assertEqual(outcome.preset_stats["preset_adjustment_pct"],
                         round(100 / len(self.world.students), 1))

    def test_round_two_can_only_improve_the_final_outcome(self):
        preset = self._preset_from_optimizer()
        with_round2 = simulate_selection(self.world, preset, self.policy, BehaviorModel(), seed=21)
        policy = SelectionPolicy.from_config({"round2_enabled": False})
        without_round2 = simulate_selection(self.world, preset, policy, BehaviorModel(), seed=21)
        self.assertGreaterEqual(with_round2.metrics["total_preference_utility"],
                                without_round2.metrics["total_preference_utility"])

    def test_required_course_is_listed_before_conflicting_elective(self):
        st = next(student for student in self.world.students.values()
                  if student.required_courses and student.year >= 3)
        required = st.required_courses[0]
        scores = {cid: 99 for cid in st.true_scores}
        prefs = build_preference_lists(self.world, st.student_id, [required], scores, self.policy)
        self.assertIn(required, prefs)
        required_index = prefs.index(required)
        conflicting = [cid for cid, course in self.world.courses.items()
                       if course.time_slot == self.world.courses[required].time_slot
                       and course.category != self.world.courses[required].category]
        self.assertTrue(all(prefs.index(cid) > required_index or cid not in prefs
                            for cid in conflicting if cid in prefs))

    def test_same_seed_reproduces_the_same_final_timetable(self):
        preset = self._preset_from_optimizer()
        first = simulate_selection(self.world, preset, self.policy, BehaviorModel(), seed=777)
        second = simulate_selection(self.world, preset, self.policy, BehaviorModel(), seed=777)
        self.assertEqual(first.final_assignments, second.final_assignments)
        self.assertEqual(first.metrics["total_preference_utility"], second.metrics["total_preference_utility"])

    def test_check_final_constraints_detects_a_broken_timetable(self):
        """The checker must actually fail a bad timetable: the old assertion sum==sum was always true."""
        by_slot: dict = {}
        for course in self.world.courses.values():
            by_slot.setdefault(course.time_slot, []).append(course.course_id)
        clash = next((ids for ids in by_slot.values() if len(ids) >= 2), None)
        if clash is not None:
            broken = {sid: clash[:2] for sid in self.world.students}
            expected_key = "time_conflict_violations"
        else:
            smallest = min(self.world.courses.values(), key=lambda c: c.capacity)
            broken = {sid: [smallest.course_id] for sid in self.world.students}
            expected_key = "capacity_violations"
        violations = check_final_constraints(self.world, broken, self.policy)
        self.assertGreater(violations[expected_key], 0,
                           f"刻意构造的违规课表没有被 {expected_key} 捕获：{violations}")


class SolverDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.world = generate_world(n_students=30, seed=4)
        self.visible = {sid: dict(st.initial_scores) for sid, st in self.world.students.items()}

    def test_solver_reports_status_objective_and_bound(self):
        _, diagnostics = solve_assignment(self.world, self.visible)
        self.assertIn(diagnostics["status"], {"OPTIMAL", "FEASIBLE", "MODEL_INVALID", "NO_ORTOOLS"})
        self.assertEqual(diagnostics["workers"], 1)
        if diagnostics["engine"] == "cp-sat":
            self.assertIsNotNone(diagnostics["objective_value"])
            self.assertIsNotNone(diagnostics["best_bound"])
            self.assertLessEqual(diagnostics["objective_value"], diagnostics["best_bound"] + 1e-6)

    def test_single_worker_solver_is_reproducible(self):
        first, _ = solve_assignment(self.world, self.visible)
        second, _ = solve_assignment(self.world, self.visible)
        self.assertEqual(first, second)

    def test_proposal_bonus_never_relaxes_a_hard_constraint(self):
        pairs = {(sid, cid): 30 for sid in list(self.world.students)[:6]
                 for cid in list(self.world.courses)[:3]}
        assignments, _ = solve_assignment(self.world, self.visible, pairs)
        violations = check_final_constraints(self.world, assignments, SelectionPolicy())
        self.assertEqual(sum(violations.values()), 0)
        for sid, chosen in assignments.items():
            self.assertLessEqual(len(chosen), self.world.students[sid].target_courses)


if __name__ == "__main__":
    unittest.main()
