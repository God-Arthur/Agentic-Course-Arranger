"""End-to-end headless render of app.py's v2 expander, without the solver and without the LLM.

``app.py`` gates the whole results area behind 「运行集体规划仿真」 (``if "run" not in
st.session_state: … st.stop()``), and that button's handler calls ``solve_assignment`` /
``run_planning`` — CP-SAT with a 6 s wall clock. Running it while a v2 batch is in flight steals
wall clock from the batch and changes later seeds' solutions, so this file does **not** click it.
Instead it injects the same payload the handler stores, built by the same production functions
(``generate_world``, ``SelectionPolicy``, ``BehaviorModel``, ``baseline_preset``,
``simulate_selection``, ``build_need_model``) with the planning layer's preset replaced by
``selection_sim.baseline_preset``. What that costs in coverage is stated plainly: the click path
itself is exercised by ``tests/test_app_semantic_tab.py`` (which does run the solver), and nothing
here proves the optimizer's preset. What it buys is that the v2 expander's own code path — the
radio, the 重算 button, ``build_need_model_v2``, the three arms, the disclaimers — is executed and
asserted instead of merely existing.

"不进求解器 / 零 API 成本" is machine-checked, not taken from the caption: the planning entry points
and ``semantic_needs_v2.llm_need_models`` are patched to raise, and no new ``ortools`` module may
appear in ``sys.modules``.
"""
from __future__ import annotations

import os
import sys
import unittest
from unittest import mock

from streamlit.testing.v1 import AppTest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# ``results_need_v2/_selftest_v2_ui_mutants.py`` points this at a mutated *copy* of app.py, so the
# probe never rewrites the real file while the batch or a browser session is using it. The copy is
# written at the repo root, so ``HERE``-relative paths inside app.py resolve identically.
APP = os.environ.get("V2_UI_APP", os.path.join(ROOT, "app.py"))
VERDICT = os.path.join(ROOT, "results_need_v2", "FROZEN_VERDICT_V2.md")
RULE_ARM = "I2 规则解析（离线，无 API 成本）"
LLM_ARM = "J2 AI 解析（调用 LLM）"
TRUTH_ARM = "K2 隐藏真实需求标签（不可部署的上限参照）"
# build_need_model_v2 reports the reader's display name, not the radio's internal key.
RULE_PARSER = "规则解析"
TRUTH_PARSER = "隐藏真实需求标签"
GOAL_KEYS = ("goals_per_student", "goal_quote_reject_pct", "goal_topic_coverage_pct")
METRIC_COL = "指标（由仿真器真值判分，不由读者自评）"
NEED_KEYS = ("goals", "explicit_request", "acceptable_alternatives", "course_relevance",
             "open_questions")
NEED_CLASSES = {"明确要求", "一般偏好", "仍有歧义"}


def _exceptions(at: AppTest) -> list:
    return [str(e.value) for e in at.exception]


def _texts(at: AppTest) -> str:
    """Every text-bearing element the app can emit, joined for substring checks."""
    parts = []
    for attr in ("markdown", "caption", "text", "info", "warning", "error", "success"):
        for item in getattr(at, attr, []) or []:
            parts.append(str(getattr(item, "value", "")))
    return "\n".join(parts)


def _metric_frame(at: AppTest):
    """The v2 判分表 exactly as the browser receives it — values already Arrow-round-tripped."""
    for element in at.dataframe:
        frame = element.value
        frame = frame.to_pandas() if hasattr(frame, "to_pandas") else frame
        if METRIC_COL in getattr(frame, "columns", []):
            return frame
    return None


def _cell(frame, key: str):
    hit = frame.loc[frame[METRIC_COL] == key, "值"]
    return None if hit.empty else str(hit.iloc[0])


def _forbidden(name: str):
    def _raise(*_args, **_kwargs):
        raise AssertionError(f"{name} 被调用：本文件承诺不进求解器、不发 API 请求")
    return _raise


def _injected_payload():
    """The dict app.py:130-141 stores, with every value produced by a production function."""
    from institution import SelectionPolicy
    from needs import build_need_model
    from planner_core import generate_world
    from selection_sim import BehaviorModel, baseline_preset, simulate_selection

    world = generate_world(n_students=30, seed=7)
    policy = SelectionPolicy()
    behavior = BehaviorModel()
    preset = baseline_preset(world, policy)
    first_seed = world.seed * 1000 + 17
    return world, {
        "mode": "注入负载：未经过规划层",
        "preset": preset,
        "visible": {sid: student.initial_scores for sid, student in world.students.items()},
        "cost": {"questions": 0, "informative_questions": 0, "solver_status": None},
        "stop_reason": "测试注入的负载：未运行规划层，也未运行求解器",
        "proposal_rows": [], "proposal_logs": [], "rejected": [],
        "needs": build_need_model(world, use_llm=False),
        "policy": policy,
        "behavior": behavior,
        "outcomes": [simulate_selection(world, preset, policy, behavior, seed=first_seed)],
        "baseline_outcome": simulate_selection(world, preset, policy, behavior, seed=first_seed),
        "config": {"kind": "合成生成", "n_students": len(world.students), "seed": world.seed,
                   "question_budget": 0, "questions_per_round": 1, "mode": "注入负载",
                   "sim_replicates": 1, "policy": policy.to_dict(), "behavior_reported": True},
    }


class V2NeedTabInApp(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls._env = dict(os.environ)
        cls._ortools_before = {name for name in sys.modules if name.startswith("ortools")}
        cls._patches = [
            mock.patch("planner_core.solve_assignment", _forbidden("planner_core.solve_assignment")),
            mock.patch("planner_core.run_planning", _forbidden("planner_core.run_planning")),
            mock.patch("proposals.run_proposal_planning",
                       _forbidden("proposals.run_proposal_planning")),
            mock.patch("semantic_needs_v2.llm_need_models",
                       _forbidden("semantic_needs_v2.llm_need_models")),
        ]
        for patcher in cls._patches:
            patcher.start()
        cls.world, payload = _injected_payload()
        cls.at = AppTest.from_file(APP, default_timeout=600)
        cls.at.session_state["world"] = cls.world
        cls.at.session_state["run"] = payload
        cls.at.run()
        failures = _exceptions(cls.at)
        if failures:
            raise AssertionError(failures)
        # Captured before any test method can move the radio: 重算 defaulting to the LLM arm would
        # spend the batch's shared 20-requests/minute quota silently.
        cls.default_arm = cls.at.get_by_key("v2_arm").value
        cls.initial_texts = _texts(cls.at)

    @classmethod
    def tearDownClass(cls) -> None:
        for patcher in cls._patches:
            patcher.stop()
        leaked = {name for name in sys.modules if name.startswith("ortools")} - cls._ortools_before
        os.environ.clear()
        os.environ.update(cls._env)
        if leaked:
            raise AssertionError(f"本文件把求解器加载进了进程：{sorted(leaked)}")

    def _run_arm(self, label: str) -> dict:
        self.at.get_by_key("v2_arm").set_value(label).run()
        self.assertEqual(_exceptions(self.at), [])
        self.at.get_by_key("run_v2").click().run()
        self.assertEqual(_exceptions(self.at), [])
        return self.at.session_state["v2"]

    def test_default_selection_is_the_offline_rule_arm(self) -> None:
        self.assertEqual(self.default_arm, RULE_ARM)

    def test_expander_declares_that_it_does_not_re_solve(self) -> None:
        # Rendered before any 重算 click: the button label and the standing caption are the app's
        # own promise that this section never touches the optimizer.
        self.assertIn("不进求解器", str(self.at.get_by_key("run_v2").label))
        self.assertIn("本区刻意不重新求解预置课表", self.initial_texts)

    def test_rule_arm_yields_a_v2_shaped_model_without_calling_the_llm(self) -> None:
        model = self._run_arm(RULE_ARM)
        self.assertEqual(model["parser"], RULE_PARSER)
        self.assertEqual(model["need_version"], "semantic-need-v2")
        self.assertEqual(model["cost"]["llm_calls"], 0)
        self.assertEqual(model["cost"]["llm_prompt_tokens"], 0)
        self.assertFalse(model["cost"]["llm_fallback"])
        self.assertEqual(len(model["records"]), len(model["skills"]))
        for rec in model["records"]:
            for key in NEED_KEYS:
                self.assertIn(key, rec, f"学生 {rec['student_id']} 的 v2 记录缺字段 {key}")
            for row in rec["course_relevance"]:
                self.assertIn(row["need_class"], NEED_CLASSES)
        with_rows = [r for r in model["records"] if r["course_relevance"]]
        self.assertTrue(with_rows, "每个学生的 v2 相关度表都是空的：本区等于没接出任何东西。")
        # The rule arm's 学习目标/待确认问题 are template-derived; if that disclaimer ever goes
        # quiet, this page reads regex output as model output (task #7's defect, v2 side).
        self.assertIn("都不是模型输出", _texts(self.at))
        frame = _metric_frame(self.at)
        self.assertIsNotNone(frame, "v2 判分表没有渲染出来：本区等于没接出任何东西。")
        self.assertTrue(all(isinstance(value, str) for value in frame["值"]),
                        f"值列类型混杂，Arrow 会改写它：{list(frame['值'])}")
        self.assertEqual(_cell(frame, "goal_metrics_applicable"), "是")
        for key in GOAL_KEYS:
            self.assertRegex(_cell(frame, key) or "", r"^-?[0-9.]+$",
                             f"{key} 对规则臂是可算的，表里却不是数字")

    def test_truth_arm_has_no_natural_language_goals_by_contract(self) -> None:
        # PREREG §2's 2026-10-17 addendum: K2 has no learning goals, and printing 0 for them would
        # read as "the truth reference does worse than AI on goals". The app must say so out loud —
        # in the table, not only in the 提示 above it.
        model = self._run_arm(TRUTH_ARM)
        self.assertEqual(model["parser"], TRUTH_PARSER)
        self.assertEqual(model["cost"]["llm_calls"], 0)
        self.assertTrue(all(not rec["goals"] for rec in model["records"]),
                        "K2 出现了学习目标，与预注册 §2 的「真值臂按接口没有自然语言目标」冲突。")
        text = _texts(self.at)
        self.assertIn("学习目标", text)
        self.assertIn("不适用", text)
        frame = _metric_frame(self.at)
        self.assertIsNotNone(frame, "v2 判分表没有渲染出来：这条检查就是空的。")
        for key in GOAL_KEYS:
            self.assertEqual(_cell(frame, key), "不适用",
                             f"K2 的 {key} 印成了 {_cell(frame, key)}：读者会当成真值臂在这项上得了这个分")
        self.assertEqual(_cell(frame, "goal_metrics_applicable"), "否")
        for value in frame["值"]:
            self.assertNotIn(str(value), {"None", "nan", "False", "True", ""},
                             "判分表里出现了裸 None/布尔值：混进百分数同一列时会被 Arrow 改写")

    def test_llm_arm_warns_about_shared_quota_and_is_never_clicked_here(self) -> None:
        self.at.get_by_key("v2_arm").set_value(LLM_ARM).run()
        self.assertEqual(_exceptions(self.at), [])
        text = _texts(self.at)
        self.assertIn("20 请求/分钟", text)
        self.assertIn("批次", text)

    def test_solver_guard_bites_through_the_app_import_path(self) -> None:
        # Proof this file's central promise is enforced rather than asserted: clicking the v1 tab's
        # 用该解析器重跑 button goes to solve_assignment, and the patch raises before CP-SAT loads.
        # If this ever stops failing, the guard is gone and the click would steal the batch's wall
        # clock — exactly the accident this file exists to prevent. Kept on its own AppTest so the
        # aborted script run cannot leak into the shared tree the other checks read.
        scratch = AppTest.from_file(APP, default_timeout=600)
        scratch_world, scratch_payload = _injected_payload()
        scratch.session_state["world"] = scratch_world
        scratch.session_state["run"] = scratch_payload
        scratch.run()
        scratch.get_by_key("run_sem").click().run()
        trapped = [text for text in _exceptions(scratch) if "solve_assignment" in text]
        self.assertEqual(len(trapped), 1,
                         f"点击 v1 重跑按钮没有被求解器守卫拦住：{_exceptions(scratch)}")

    def test_llm_guard_is_armed_on_the_module_the_app_calls(self) -> None:
        import semantic_needs_v2

        with self.assertRaises(AssertionError):
            semantic_needs_v2.llm_need_models(None, None, None, None)

    def test_verdict_is_either_shown_or_declared_missing_never_blurred(self) -> None:
        if os.path.exists(VERDICT):
            self.assertIn("判据（独立测试种子", self.initial_texts,
                          "判定文件已存在，但界面没有把主判据行原样显示出来。")
            self.assertNotIn("还不存在", self.initial_texts)
        else:
            self.assertIn("还不存在", self.initial_texts,
                          "判定未落盘时界面必须明说本区不能用来宣称第二处理更好——"
                          "采不到这段说明，这条检查就是空的。")


if __name__ == "__main__":
    unittest.main()
