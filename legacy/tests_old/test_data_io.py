"""The CSV contract is the only bridge between a real table and the engine."""
import csv
import json
import os
import tempfile
import unittest

import data_io
from data_io import DataContractError, load_world, save_world
from planner_core import generate_world


class RoundTripTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.world = generate_world(n_students=12, seed=7)
        save_world(self.world, self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_saved_tables_reload_identically(self):
        reloaded = load_world(self.tmp.name)
        self.assertEqual(reloaded.seed, self.world.seed)
        self.assertEqual(set(reloaded.courses), set(self.world.courses))
        self.assertEqual(set(reloaded.students), set(self.world.students))
        for sid, st in self.world.students.items():
            other = reloaded.students[sid]
            self.assertEqual(other.true_scores, st.true_scores)
            self.assertEqual(other.initial_scores, st.initial_scores)
            self.assertEqual(other.declared_courses, st.declared_courses)
            self.assertEqual(other.required_courses, st.required_courses)
            self.assertEqual(other.completed_courses, st.completed_courses)
            self.assertEqual(other.priority_tier, st.priority_tier)
            self.assertEqual(other.target_courses, st.target_courses)

    def test_reload_is_deterministic_end_to_end(self):
        from planner_core import allocate_optimized
        visible = {sid: dict(st.initial_scores) for sid, st in self.world.students.items()}
        reloaded = load_world(self.tmp.name)
        visible2 = {sid: dict(st.initial_scores) for sid, st in reloaded.students.items()}
        self.assertEqual(allocate_optimized(self.world, visible),
                         allocate_optimized(reloaded, visible2))

    def test_manifest_declares_synthetic_provenance(self):
        with open(os.path.join(self.tmp.name, "manifest.json"), encoding="utf-8") as handle:
            manifest = json.load(handle)
        self.assertEqual(manifest["seed"], self.world.seed)
        self.assertIn("not real", manifest["provenance"].lower())

    def test_truth_file_carries_a_quarantine_warning(self):
        with open(os.path.join(self.tmp.name, "simulation_truth.csv"), encoding="utf-8-sig") as handle:
            head = handle.read()
        self.assertIn(data_io.TRUTH_WARNING, head)
        self.assertNotIn("#READ_ONLY", load_world(self.tmp.name).students)


class ContractViolationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        save_world(generate_world(n_students=6, seed=3), self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _rewrite(self, filename, mutate):
        path = os.path.join(self.tmp.name, filename)
        with open(path, newline="", encoding="utf-8-sig") as handle:
            rows = list(csv.DictReader(handle))
        rows = mutate(rows)
        with open(path, "w", newline="", encoding="utf-8-sig") as handle:
            writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
            writer.writeheader()
            writer.writerows(rows)

    def test_missing_file(self):
        with self.assertRaises(DataContractError):
            load_world(os.path.join(self.tmp.name, "does-not-exist"))

    def test_missing_column(self):
        path = os.path.join(self.tmp.name, "courses.csv")
        with open(path, "w", encoding="utf-8-sig") as handle:
            handle.write("course_id,course_name\nC1,甲\n")
        with self.assertRaises(DataContractError) as ctx:
            load_world(self.tmp.name)
        self.assertIn("缺少必需列", str(ctx.exception))

    def test_duplicate_course_id(self):
        def mutate(rows):
            rows.append(dict(rows[0]))
            return rows
        self._rewrite("courses.csv", mutate)
        with self.assertRaises(DataContractError):
            load_world(self.tmp.name)

    def test_non_integer_capacity(self):
        self._rewrite("courses.csv", lambda rows: [
            {**row, "capacity": "很多"} if row["course_id"] == rows[0]["course_id"] else row
            for row in rows])
        with self.assertRaises(DataContractError):
            load_world(self.tmp.name)

    def test_unknown_course_reference(self):
        self._rewrite("students.csv", lambda rows: [
            {**rows[0], "declared_courses": rows[0]["declared_courses"] + "|GHOST-999"}, *rows[1:]])
        with self.assertRaises(DataContractError):
            load_world(self.tmp.name)

    def test_declared_course_without_visible_score(self):
        def mutate(rows):
            row = rows[0]
            scores = json.loads(row["initial_scores"])
            first = row["declared_courses"].split("|")[0]
            scores.pop(first, None)
            row["initial_scores"] = json.dumps(scores, ensure_ascii=False)
            return rows
        self._rewrite("students.csv", mutate)
        with self.assertRaises(DataContractError):
            load_world(self.tmp.name)

    def test_student_missing_hidden_truth(self):
        def mutate(rows):
            return [row for row in rows if row["student_id"] != "#READ_ONLY"][1:]
        self._rewrite("simulation_truth.csv", mutate)
        with self.assertRaises(DataContractError):
            load_world(self.tmp.name)

    def test_malformed_preference_json(self):
        self._rewrite("students.csv", lambda rows: [{**rows[0], "initial_scores": "{不是 JSON"}, *rows[1:]])
        with self.assertRaises(DataContractError):
            load_world(self.tmp.name)

    def test_non_integer_year_is_a_contract_error(self):
        self._rewrite("students.csv", lambda rows: [{**rows[0], "year": "大三"}, *rows[1:]])
        with self.assertRaises(DataContractError):
            load_world(self.tmp.name)


if __name__ == "__main__":
    unittest.main()
