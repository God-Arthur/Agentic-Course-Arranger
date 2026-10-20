import json
import os
import unittest
from unittest import mock

import llm_controller
import planner_core
from llm_controller import select_queries_with_llm
from planner_core import allocate_optimized, build_query_candidates, evaluate_assignments, generate_world

PROMPT_KEYS = ("query_id", "student_id", "bottleneck_course", "alternative_course", "category",
               "motivation", "unfilled_target_seats", "topic_similarity", "seat_slack",
               "demand_pressure", "public_priority_score", "question")


class _SteppingClock:
    """Fake monotonic clock: sleeping advances it, so nothing ever really blocks."""

    def __init__(self):
        self.now = 0.0
        self.slept = []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds


_clock = _SteppingClock()
_throttle_off = None
_time_patcher = None


def setUpModule():
    """Most of this suite asserts behaviour, not pacing; keep real waiting out of it."""
    global _throttle_off, _time_patcher
    _throttle_off = mock.patch.dict(os.environ, {"LLM_REQUESTS_PER_MINUTE": "0"})
    _throttle_off.start()
    _time_patcher = mock.patch.object(llm_controller, "time", _clock)
    _time_patcher.start()


def tearDownModule():
    _time_patcher.stop()
    _throttle_off.stop()


class _Response:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def _payload(query_ids, note="选择这些查询。", action="ask"):
    return {"choices": [{"message": {"content": json.dumps({
        "action": action, "query_ids": query_ids, "selection_note": note}, ensure_ascii=False)}}],
        "usage": {"prompt_tokens": 1234, "completion_tokens": 56}}


def _candidates(n_students=48, seed=5):
    world = generate_world(n_students=n_students, seed=seed)
    visible = {sid: dict(st.initial_scores) for sid, st in world.students.items()}
    assignments = allocate_optimized(world, visible)
    return world, visible, build_query_candidates(world, assignments, visible, set())


def _user_message(http_body):
    """The controller packs its structured state into the second chat message as JSON."""
    return json.loads(http_body["messages"][1]["content"])


class PromptIsolationTests(unittest.TestCase):
    """The controller may only ever see observable state, never latent utilities."""

    def test_payload_contains_no_hidden_scores(self):
        world, visible, candidates = _candidates()
        with mock.patch.object(llm_controller, "requests") as stub:
            captured = {}

            def post(url, headers=None, json=None, timeout=None):
                captured["payload"] = json
                captured["headers"] = headers or {}
                return _Response(_payload([candidates[0]["query_id"]]))

            stub.post = post
            select_queries_with_llm(candidates, 3, {"api_key": "test-key"}, {"round": 1})
            sent = _user_message(captured["payload"])

        blob = json.dumps(sent, ensure_ascii=False)
        self.assertNotIn("true_scores", blob, "提示词不得包含隐藏真值字段")
        self.assertNotIn("true_score", blob)
        for student in world.students.values():
            self.assertNotIn(json.dumps(student.true_scores, ensure_ascii=False), blob,
                             "学生的完整隐藏效用表出现在提示词中")
        for record in sent["candidate_questions"]:
            self.assertEqual(set(record.keys()), set(PROMPT_KEYS),
                             "候选记录只能包含白名单内的公开字段")
        self.assertNotIn("test-key", blob, "提示词正文不得混入密钥")
        self.assertEqual(captured["headers"].get("Authorization"), "Bearer test-key",
                         "密钥只出现在鉴权头中")

    def test_payload_carries_structured_global_state(self):
        world, visible, candidates = _candidates()
        with mock.patch.object(llm_controller, "requests") as stub:
            captured = {}

            def post(url, headers=None, json=None, timeout=None):
                captured["payload"] = json
                return _Response(_payload([]))

            stub.post = post
            state = {"round": 2, "questions_left_total": 8, "unfilled_target_course_places": 15}
            select_queries_with_llm(candidates, 3, {"api_key": "test-key"}, state)
            payload = _user_message(captured["payload"])
        self.assertEqual(payload["observable_planning_state"], state)
        self.assertEqual(payload["remaining_question_budget_this_round"], 3)
        self.assertIn("output_schema", payload)


class ActionValidationTests(unittest.TestCase):
    def test_stop_action_returns_no_queries(self):
        _, _, candidates = _candidates()
        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = lambda url, headers=None, json=None, timeout=None: _Response(
                _payload([candidates[0]["query_id"]], action="stop", note="信息已足够。"))
            chosen, note, requested_stop, usage = select_queries_with_llm(
                candidates, 3, {"api_key": "test-key"}, {})
        self.assertEqual(chosen, [])
        self.assertTrue(requested_stop)
        self.assertEqual(usage["prompt_tokens"], 1234)

    def test_invented_query_ids_are_rejected(self):
        _, _, candidates = _candidates()
        real = candidates[0]["query_id"]
        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = lambda url, headers=None, json=None, timeout=None: _Response(
                _payload(["Q-FAKE-999", real, "not-a-query"]))
            chosen, _, _, _ = select_queries_with_llm(candidates, 3, {"api_key": "test-key"}, {})
        self.assertEqual([q["query_id"] for q in chosen], [real])

    def test_budget_and_duplicate_students_are_enforced(self):
        _, _, candidates = _candidates()
        distinct = []
        seen = set()
        for query in candidates:
            if query["student_id"] not in seen:
                seen.add(query["student_id"])
                distinct.append(query["query_id"])
        # 故意超预算，并把第一名学生重复提问。
        overstated = (distinct[:4] + [distinct[0]]) if len(distinct) >= 2 else distinct
        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = lambda url, headers=None, json=None, timeout=None: _Response(_payload(overstated))
            chosen, _, _, _ = select_queries_with_llm(candidates, 3, {"api_key": "test-key"}, {})
        self.assertLessEqual(len(chosen), 3)
        self.assertEqual(len({q["student_id"] for q in chosen}), len(chosen))
        self.assertEqual(len(chosen), min(3, len(distinct)))

    def test_missing_api_key_fails_before_network(self):
        _, _, candidates = _candidates()
        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = lambda *a, **k: self.fail("缺少密钥时不应发起请求")
            with self.assertRaises(ValueError):
                select_queries_with_llm(candidates, 3, {"api_key": ""}, {})


class RateLimitTests(unittest.TestCase):
    """上游配额是 20 次/分钟；超配额会让整轮实验退化成规则回退。"""

    def setUp(self):
        self.saved = list(llm_controller._REQUEST_TIMES)
        llm_controller._REQUEST_TIMES.clear()
        self.clock = _SteppingClock()
        patchers = [mock.patch.object(llm_controller, "time", self.clock),
                    mock.patch.dict(os.environ, {"LLM_REQUESTS_PER_MINUTE": "3"})]
        for p in patchers:
            p.start()
        self.addCleanup(self._restore, patchers)

    def _restore(self, patchers):
        for p in reversed(patchers):
            p.stop()
        llm_controller._REQUEST_TIMES.clear()
        llm_controller._REQUEST_TIMES.extend(self.saved)

    def test_window_paces_requests_to_the_quota(self):
        for _ in range(3):
            llm_controller._wait_for_slot()
        self.assertEqual(self.clock.slept, [], "配额内不应等待")
        llm_controller._wait_for_slot()
        self.assertEqual(self.clock.slept, [60.05], "第 4 次请求必须等窗口滑走")
        self.assertEqual(len(llm_controller._REQUEST_TIMES), 1)

    def test_quota_zero_disables_throttling(self):
        with mock.patch.dict(os.environ, {"LLM_REQUESTS_PER_MINUTE": "0"}):
            for _ in range(50):
                llm_controller._wait_for_slot()
        self.assertEqual(self.clock.slept, [])
        self.assertEqual(len(llm_controller._REQUEST_TIMES), 0)

    def test_malformed_quota_is_reported_instead_of_silently_ignored(self):
        with mock.patch.dict(os.environ, {"LLM_REQUESTS_PER_MINUTE": "20 rpm"}):
            with self.assertRaises(ValueError):
                llm_controller.requests_per_minute()

    def test_backoff_prefers_retry_after_then_grows_exponentially(self):
        busy = mock.Mock()
        busy.response = mock.Mock(headers={"Retry-After": "12"})
        self.assertEqual(llm_controller._retry_delay(busy, 0), 12.0)
        plain = RuntimeError("boom")
        self.assertEqual([llm_controller._retry_delay(plain, a) for a in (0, 1, 2, 9)],
                         [1.0, 2.0, 4.0, 60.0])

    def test_rate_limited_reply_is_retried_and_still_returns_selections(self):
        _, _, candidates = _candidates(n_students=36, seed=41)
        self.assertTrue(candidates, "该世界应能生成候选问题")
        qid = candidates[0]["query_id"]
        reply = mock.Mock(headers={"Retry-After": "20"})
        too_many = llm_controller.requests.exceptions.HTTPError("429 Too Many Requests", response=reply)
        limited = mock.Mock()
        limited.raise_for_status = mock.Mock(side_effect=too_many)
        limited.response = reply
        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = mock.Mock(side_effect=[limited, _Response(_payload([qid]))])
            with mock.patch.dict(os.environ, {"LLM_REQUESTS_PER_MINUTE": "20"}):
                chosen, _, requested_stop, _ = select_queries_with_llm(
                    candidates, 3, {"api_key": "test-key"}, {})
        self.assertEqual([q["query_id"] for q in chosen], [qid])
        self.assertFalse(requested_stop)
        self.assertIn(20.0, self.clock.slept, "429 必须按 Retry-After 退避后重试")
        self.assertEqual(stub.post.call_count, 2)


class PlanningLoopWithLLMTests(unittest.TestCase):
    """AI actions run through the same validator and solver, so they cannot break rules."""

    def test_adversarial_llm_choices_still_yield_zero_violations(self):
        world, visible, _ = _candidates(n_students=40, seed=31)
        pool = build_query_candidates(world, allocate_optimized(world, visible), visible, set())
        ids = [q["query_id"] for q in pool]

        def post(url, headers=None, json=None, timeout=None):
            # 恶意顺序：反向挑选，尽量越界超额返回。
            return _Response(_payload(ids[::-1] + ["Q-NOPE"] * 5))

        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = post
            result = planner_core.run_planning(
                world, question_budget=6, questions_per_round=3,
                controller="LLM 规划控制器", llm_settings={"api_key": "test-key"})
        metrics = evaluate_assignments(world, result.planned_assignments)
        self.assertEqual(metrics["capacity_violations"], 0)
        self.assertEqual(metrics["time_conflict_violations"], 0)
        self.assertEqual(metrics["eligibility_violations"], 0)
        self.assertLessEqual(result.cost["questions"], 6)
        self.assertGreater(result.cost["llm_calls"], 0)
        self.assertFalse(result.cost["llm_fallback"])
        self.assertEqual(result.cost["llm_prompt_tokens"], 1234 * result.cost["llm_calls"])

    def test_api_error_falls_back_to_rule_policy_and_is_flagged(self):
        world, _, _ = _candidates(n_students=36, seed=33)
        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = mock.Mock(side_effect=RuntimeError("连接超时"))
            result = planner_core.run_planning(
                world, question_budget=4, questions_per_round=2,
                controller="LLM 规划控制器", llm_settings={"api_key": "test-key"})
        self.assertTrue(result.cost["llm_fallback"], "回退必须被记录，否则无法区分规则与 AI 输出")
        self.assertEqual(evaluate_assignments(world, result.planned_assignments)["capacity_violations"], 0)
        self.assertTrue(any("回退" in str(row.get("controller_note", "")) for row in result.logs))

    def test_llm_stop_decision_ends_the_loop(self):
        world, _, _ = _candidates(n_students=36, seed=35)
        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = lambda url, headers=None, json=None, timeout=None: _Response(
                _payload([], action="stop", note="继续问询的预期收益不足。"))
            result = planner_core.run_planning(
                world, question_budget=8, questions_per_round=4,
                controller="LLM 规划控制器", llm_settings={"api_key": "test-key"})
        self.assertEqual(result.cost["questions"], 0)
        self.assertEqual(result.cost["rounds"], 1)
        self.assertIn("停止", result.stop_reason + json.dumps(result.logs, ensure_ascii=False))

    def test_no_key_configured_still_runs_with_rule_controller(self):
        world, _, _ = _candidates(n_students=30, seed=37)
        with mock.patch.object(llm_controller, "requests") as stub:
            stub.post = lambda *a, **k: self.fail("未配置密钥时不应调用外部 API")
            result = planner_core.run_planning(
                world, question_budget=4, questions_per_round=2,
                controller="LLM 规划控制器", llm_settings={})
        self.assertTrue(result.cost["llm_fallback"])
        self.assertEqual(evaluate_assignments(world, result.planned_assignments)["time_conflict_violations"], 0)


if __name__ == "__main__":
    unittest.main()
