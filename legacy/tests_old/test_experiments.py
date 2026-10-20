"""Reporting-honesty tests: invalid LLM runs must never leak into means or other arms."""
from __future__ import annotations

import os
import unittest

from experiments import (
    MECHANISMS, _paired_stats, _sign_p_two_sided, _sign_threshold, build_report,
    load_raw_rows, significance_caveat, summarize,
)

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RAW_CSV = os.path.join(ROOT, "results", "raw_rows.csv")


def _row(seed, mechanism, utility, status="已运行", valid=True, note=""):
    return {
        "seed": seed, "mechanism": mechanism, "status": status, "note": note,
        "valid_for_ai_claim": valid, "total_preference_utility": utility,
        "average_student_utility": utility / 72.0, "hard_violations": 0,
        "questions": 0, "informative_questions": 0, "rounds": 0,
    }


class SignThreshold(unittest.TestCase):
    """The report must state the win count a sample can actually reach, not a remembered number."""

    def test_known_thresholds(self):
        self.assertEqual(_sign_threshold(8), 8)          # only a clean sweep is significant at n=8
        self.assertEqual(_sign_threshold(30), 21)
        self.assertEqual(_sign_threshold(40), 27)

    def test_threshold_agrees_with_the_p_it_implies(self):
        for m in (8, 30, 40):
            k = _sign_threshold(m)
            if k <= m:
                self.assertLess(_sign_p_two_sided(m, k), 0.05)
                self.assertGreaterEqual(_sign_p_two_sided(m, k - 1), 0.05)

    def test_unreachable_sample_reports_full_sweep_as_ceiling(self):
        self.assertEqual(_sign_threshold(5), 6)          # 5/5 still gives p = 0.0625
        self.assertEqual(_sign_p_two_sided(5, 5), 0.0625)

    def test_paired_stats_expose_the_non_tie_denominator(self):
        stats = _paired_stats([1.0, -1.0, 0.0, 2.0])
        self.assertEqual((stats["sign_test_pairs"], stats["sign_test_wins"]), (3, 2))


    def test_caveat_follows_the_sample_not_a_remembered_n(self):
        note8 = significance_caveat({"X - Y": {"n": 8, "sign_test_pairs": 8}})
        note30 = significance_caveat({"X - Y": {"n": 30, "sign_test_pairs": 30}})
        note5 = significance_caveat({"X - Y": {"n": 5, "sign_test_pairs": 5}})
        self.assertIn("至少要 8 胜", note8)
        self.assertIn("至少要 21 胜", note30)
        self.assertNotIn("8 胜", note30)
        self.assertIn("即使 5 战全胜 p 也只到 0.0625", note5)  # n=5 cannot certify even a sweep
        self.assertEqual(significance_caveat({}), "本轮没有非平局的配对场景，符号检验无从谈起。")


class SummarizeExclusions(unittest.TestCase):
    def setUp(self):
        # seed 1: one clean run of every arm. seeds 2-3: the LLM arm falls back.
        self.rows = [_row(1, mech, 1000.0 + i) for i, mech in enumerate(MECHANISMS)]
        self.rows += [
            _row(2, "A_现行机制基线", 1500.0),
            _row(2, "C_AI集体规划", 2000.0, valid=False, note="LLM 回退"),
            _row(3, "A_现行机制基线", 1600.0),
            _row(3, "C_AI集体规划", 3000.0, valid=False, note="LLM 回退"),
            _row(4, "A_现行机制基线", 1700.0),
            _row(4, "C_AI集体规划", 4000.0),
            _row(5, "B_规则式集体规划", 5000.0, status="求解失败", note="无可行解"),
        ]
        self.summary = summarize(self.rows)

    def test_only_valid_llm_rows_reach_the_ai_mean(self):
        arm_c = self.summary["per_mechanism"]["C_AI集体规划"]
        self.assertEqual(arm_c["runs"], 2)
        self.assertEqual(arm_c["llm_fallback_runs"], 2)
        # seeds 1 and 4 are valid; the 2000/3000 fallback rows must not be averaged in.
        utility = arm_c["metrics"]["total_preference_utility"]
        self.assertEqual((utility["n"], utility["min"], utility["max"]), (2, 1003.0, 4000.0))

    def test_every_dropped_row_is_listed(self):
        dropped = {(e["seed"], e["mechanism"]) for e in self.summary["excluded_rows"]}
        self.assertEqual(dropped, {(2, "C_AI集体规划"), (3, "C_AI集体规划"), (5, "B_规则式集体规划")})

    def test_later_arms_do_not_inherit_llm_fallback_counts(self):
        for mech in ("A_现行机制基线", "B0_一次性优化无交互", "B_规则式集体规划", "D_完整信息参照"):
            self.assertEqual(self.summary["per_mechanism"][mech]["llm_fallback_runs"], 0)

    def test_contrasts_skip_pairs_whose_target_row_is_invalid(self):
        contrast = self.summary["paired_contrasts"]["C_AI集体规划 - A_现行机制基线"]
        self.assertEqual(contrast["n"], 2)


class PairedSignTest(unittest.TestCase):
    """A win/loss record is not an effect: the exact sign test must say how weak n=8 is."""

    def test_perfect_sweep_is_only_just_significant(self):
        stats = _paired_stats([10.0] * 8)
        self.assertEqual(stats["n"], 8)
        self.assertEqual(stats["sign_test_wins"], 8)
        self.assertAlmostEqual(stats["sign_test_p_two_sided"], 2 / 256, places=4)
        self.assertTrue(stats["significant_at_5pct"])

    def test_six_wins_two_losses_is_not_significant(self):
        stats = _paired_stats([40, 15, 40, -41, 39, 164, 69, -156])
        self.assertEqual((stats["sign_test_wins"], stats["n"]), (6, 8))
        self.assertAlmostEqual(stats["sign_test_p_two_sided"], 2 * 37 / 256, places=4)
        self.assertFalse(stats["significant_at_5pct"])
        self.assertGreater(stats["mean_delta_total_utility"], 0)

    def test_ties_leave_the_sign_test_denominator(self):
        stats = _paired_stats([5.0, -5.0, 0.0, 0.0])
        self.assertEqual(stats["n"], 4)
        self.assertEqual(stats["sign_test_wins"], 1)
        self.assertEqual(stats["sign_test_p_two_sided"], 1.0)

    def test_dispersion_is_reported_alongside_the_mean(self):
        stats = _paired_stats([2.0, 4.0])
        self.assertEqual(stats["mean_delta_total_utility"], 3.0)
        self.assertEqual((stats["sd_delta"], stats["se_delta"]), (1.41, 1.0))
        self.assertEqual(stats["t_paired"], 3.0)
        self.assertEqual(stats["sign_test_p_two_sided"], 0.5)
        self.assertEqual(_paired_stats([]), {"n": 0})

    def test_single_seed_contrast_cannot_be_significant(self):
        stats = _paired_stats([7.0])
        self.assertNotIn("sd_delta", stats)
        self.assertEqual(stats["sign_test_p_two_sided"], 1.0)
        self.assertFalse(stats["significant_at_5pct"])


class ReportToleratesNeverRunArms(unittest.TestCase):
    """A run without --include-llm leaves C/E contrasts with zero pairs. The report must still render.

    Regression: the paired table once indexed mean_delta_total_utility directly, so a
    keys-absent contrast raised KeyError *after* the arms had run — which, with the default
    --out, meant the cost had been paid and nothing was written.
    """

    def _rows(self):
        rows = []
        for seed in (1, 2, 3):
            for i, mech in enumerate(MECHANISMS):
                ran = mech not in ("C_AI集体规划", "E_LLM协调提案")
                rows.append(_row(seed, mech, 12000.0 + seed * 40 + i * 7,
                                 status="已运行" if ran else "未运行", valid=ran))
        return rows

    def test_zero_pair_contrast_is_a_dash_not_a_crash(self):
        rows = self._rows()
        summary = summarize(rows)
        core = summary["paired_contrasts"]["E_LLM协调提案 - F_规则协调提案"]
        self.assertEqual(core["n"], 0)
        self.assertNotIn("mean_delta_total_utility", core)

        report = build_report(rows, summary, {
            "n_students": 72, "seeds": [1, 2, 3], "question_budget": 30,
            "questions_per_round": 4, "sim_replicates": 3, "run_started_at": "test",
            "solver_workers": 1, "solver_max_seconds": 6,
        })
        line = next(l for l in report.splitlines()
                    if l.startswith("| E_LLM协调提案 - F_规则协调提案 |"))
        self.assertIn("—", line)
        self.assertNotIn("None", line)
        self.assertNotIn("| B_规则式集体规划 - A_现行机制基线 | 0 |", report)

    def test_llm_arms_are_absent_from_the_means_and_the_header(self):
        rows = self._rows()
        summary = summarize(rows)
        report = build_report(rows, summary, {"seeds": [1, 2, 3], "run_started_at": "test"})
        header = next(l for l in report.splitlines() if l.startswith("| 指标 | "))
        self.assertNotIn("AI 集体规划", header)
        self.assertNotIn("LLM协调提案", header)
        self.assertEqual([e["mechanism"] for e in summary["excluded_rows"]].count("C_AI集体规划"), 3)


@unittest.skipUnless(os.path.exists(RAW_CSV), "results/raw_rows.csv 尚未生成")
class RecordedResultsStillReportedHonestly(unittest.TestCase):
    def test_reload_of_recorded_raw_rows_reproduces_the_exclusions(self):
        rows = load_raw_rows(RAW_CSV)
        self.assertIsInstance(rows[0]["seed"], int)
        self.assertIsInstance(rows[0]["valid_for_ai_claim"], bool)
        self.assertIsNone(rows[0]["utility_per_informative_query"])

        summary = summarize(rows)
        c_seeds = {r["seed"] for r in rows if r["mechanism"] == "C_AI集体规划"}
        arm_c = summary["per_mechanism"]["C_AI集体规划"]
        dropped_c = [e for e in summary["excluded_rows"] if e["mechanism"] == "C_AI集体规划"]
        self.assertEqual(arm_c["runs"] + len(dropped_c), len(c_seeds))
        self.assertEqual(summary["per_mechanism"]["D_完整信息参照"]["llm_fallback_runs"], 0)
        self.assertEqual(summary["per_mechanism"]["D_完整信息参照"]["violations_total"], 0)

    def test_recorded_contrast_significance_flags_match_their_p_values(self):
        summary = summarize(load_raw_rows(RAW_CSV))
        self.assertTrue(summary["paired_contrasts"])
        for name, contrast in summary["paired_contrasts"].items():
            p = contrast.get("sign_test_p_two_sided")
            if not contrast.get("deltas"):
                # An arm pair that never co-ran in this dataset must stay silent: no p value,
                # no significance flag, no invented min/max.
                self.assertIsNone(p, name)
                self.assertNotIn("significant_at_5pct", contrast, name)
                self.assertIsNone(contrast["min_delta"], name)
                continue
            self.assertIsNotNone(p, name)
            self.assertEqual(contrast.get("significant_at_5pct"), p < 0.05, name)
            self.assertEqual(contrast["min_delta"], min(contrast["deltas"]), name)
            self.assertEqual(contrast["max_delta"], max(contrast["deltas"]), name)


if __name__ == "__main__":
    unittest.main()
