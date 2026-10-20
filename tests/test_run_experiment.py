"""tools/run_experiment.py 的离线端到端测试：假客户端 + 临时目录，不花任何配额。

重点验三件事：长跑能跑完、检查点续跑不再调用模型、最终课表没有超载或时间冲突。
"""
import json
import sys
from collections import Counter
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import run_experiment as rx                                  # noqa: E402
from planner.models import World, load_world                 # noqa: E402
from planner.timegrid import has_conflict                    # noqa: E402


class ScriptedClient:
    """按 system 提示词判场景给回复，顺便统计调用次数（不联网）。"""

    calls = 0
    failures = 0
    explode = False
    fail_on_review = False
    fail_on_alloc = False
    plan_round1 = 0

    def __init__(self, base_url=None, model=None, api_key=None, **kwargs):
        pass

    def perf(self):
        return {"avg_latency_s": 0.0, "max_latency_s": 0.0, "call_span_s": 0.0,
                "effective_rpm": None}

    def chat_json(self, system, user):
        if self.explode:
            raise AssertionError("续跑时不该再调用模型")
        if self.fail_on_review and "【上一版预置课表】" in user:
            raise AssertionError("模拟端点在复盘改写阶段断线")
        if self.fail_on_alloc and system.startswith("你是教务系统的选课筛选器"):
            raise AssertionError("模拟端点在筛选器阶段断线")
        type(self).calls += 1
        if system.startswith("你扮演一名中国大学生") and "【第 1 轮】" in user:
            type(self).plan_round1 += 1
        if system.startswith("你是教务系统的需求解析器"):
            return {"required": {"思政类": 1}, "elective": {}, "avoid_slots": [], "notes": "无"}
        if system.startswith("你是教务系统的预置课表规划器"):
            return {"sections": ["A1", "PE1"], "why": "先必修"}
        if system.startswith("你扮演一名中国大学生"):
            return {"drops": [], "preferences": [{"section_id": "B1", "rank": 1}]}
        if system.startswith("你是教务系统的选课筛选器"):
            return {"grants": [{"student_id": "S001", "section_id": "B1"}]}
        if system.startswith("你是选课满意度评估器"):
            return {"satisfaction": 70, "reason": "还行"}
        raise AssertionError(f"未预期的提示词：{system[:20]}")


@pytest.fixture(autouse=True)
def reset_client():
    ScriptedClient.calls = 0
    ScriptedClient.failures = 0
    ScriptedClient.explode = False
    ScriptedClient.fail_on_review = False
    ScriptedClient.fail_on_alloc = False
    ScriptedClient.plan_round1 = 0
    yield


@pytest.fixture
def dataset(tmp_path):
    courses = {
        "courses": [
            {"course_id": "A", "name": "思政A", "category": "思政类"},
            {"course_id": "B", "name": "外语B", "category": "外语类"},
            {"course_id": "PE", "name": "体育", "category": "体育课"},
        ],
        "sections": [
            {"section_id": "A1", "course_id": "A", "capacity": 2, "slots": ["Mon-1"]},
            {"section_id": "B1", "course_id": "B", "capacity": 1, "slots": ["Tue-2"]},
            {"section_id": "PE1", "course_id": "PE", "capacity": 3, "slots": ["Thu-11"]},
        ],
        "plan": {
            "majors": ["计算机科学与技术"],
            "recommended": {"计算机科学与技术": {"思政类": 1}},
            "recommended_by_year": {"计算机科学与技术": {
                "一": {"思政类": 1}, "二": {"外语类": 1}}},
            "need_rules": {"language_share_per_student": 0.0, "pe_courses_per_student": 1,
                           "exclude_grades": ["四"]},
        },
        "_meta": {"capacity_divisor": 1},
    }
    (tmp_path / "courses.json").write_text(json.dumps(courses, ensure_ascii=False),
                                           encoding="utf-8")
    (tmp_path / "sizes.json").write_text(json.dumps(
        {"per_grade": {"计算机科学与技术": 40}}), encoding="utf-8")
    return tmp_path


def _argv(dataset, out, iterations="1"):
    return ["--courses", str(dataset / "courses.json"), "--sizes", str(dataset / "sizes.json"),
            "--n", "3", "--seed", "5", "--iterations", iterations, "--workers", "2",
            "--out", str(out)]


def test_allocate_population_weights_by_enrolment(dataset):
    world = load_world(json.loads((dataset / "courses.json").read_text(encoding="utf-8")))
    allocation = rx.allocate_population(9, world, {"计算机科学与技术": 40})
    assert len(allocation) == 9                        # 最大余数法：总数不多不少
    assert Counter(allocation) == Counter({("计算机科学与技术", "一"): 5,
                                           ("计算机科学与技术", "二"): 4})
    # 人数权重为 0 的专业不能白占格子
    assert rx.allocate_population(4, world, {}) == [("计算机科学与技术", "一")] * 2 + \
        [("计算机科学与技术", "二")] * 2


def test_run_experiment_end_to_end_with_scripted_llm(dataset):
    ScriptedClient.explode = False
    ScriptedClient.calls = 0
    rx.LLMClient = ScriptedClient
    rx.resolve_llm = lambda user: ({"base_url": "http://x", "model": "m", "api_key": "k",
                                    "min_interval": 0}, {})
    out = dataset / "result.json"
    assert rx.main(_argv(dataset, out)) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["students"] == 3
    assert payload["_meta"]["need_rules"]["pe_courses_per_student"] == 1
    assert len(payload["history"]) == 2                 # 初始一轮 + 复盘改写一轮
    assert payload["llm_stats"]["calls"] == ScriptedClient.calls > 0
    ckpt = dataset / "result.json.ckpt"
    for name in ("needs.json", "presets_0.json", "presets_1.json",
                 "iter0_round1.json", "iter0_round2.json", "iter0_scores.json"):
        assert (ckpt / name).exists(), name
    # 模拟真的跑过：曾因 simulate_rounds 的 start_round 初始化错而静默跳过两轮，
    # 那时 round1 检查点根本不存在、grant_events 为 0，而下面的续跑测试照样"通过"。
    assert payload["summary"]["grant_events"] >= 1
    round1 = json.loads((ckpt / "iter0_round1.json").read_text(encoding="utf-8"))
    assert any(e["phase"] == "中签" for e in round1["events"])

    # 事实校验：任何教学班都不许超座，任何人都不许时间冲突
    world = load_world(json.loads((dataset / "courses.json").read_text(encoding="utf-8")))
    booked = {}
    for row in payload["students"]:
        sections = [item["section_id"] for item in row["final"]]
        for section_id in sections:
            booked[section_id] = booked.get(section_id, 0) + 1
        slots = []
        for section_id in sections:
            slots.extend(world.sections[section_id].slots)
        assert not has_conflict(set(slots), set()), row["student_id"]
    for section_id, count in booked.items():
        assert count <= world.sections[section_id].capacity, section_id


def test_rerun_resumes_from_checkpoints_without_any_llm_call(dataset):
    _run_once(dataset)
    ScriptedClient.calls = 0
    ScriptedClient.explode = True            # 再调用一次就 AssertionError
    out = dataset / "result.json"
    assert rx.main(_argv(dataset, out)) == 0
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["summary"]["students"] == 3
    assert payload["summary"]["grant_events"] >= 1        # 续跑是从检查点重建的同一份结果
    assert ScriptedClient.calls == 0


def _run_once(dataset, iterations="1"):
    ScriptedClient.explode = False
    rx.LLMClient = ScriptedClient
    rx.resolve_llm = lambda user: ({"base_url": "http://x", "model": "m", "api_key": "k",
                                    "min_interval": 0}, {})
    assert rx.main(_argv(dataset, dataset / "result.json", iterations)) == 0


def test_dry_run_makes_no_calls(dataset, capsys):
    rx.LLMClient = ScriptedClient
    assert rx.main(_argv(dataset, dataset / "unused.json") + ["--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert '"students": 3' in printed
    assert ScriptedClient.calls == 0


def test_partial_result_is_reviewable_when_the_run_dies_midway(dataset):
    """1800 人档要跑几小时：中途断线时 --out 必须已经是上一轮的完整结果，不能只剩日志。"""
    _run_once(dataset, iterations="0")
    out = dataset / "result.json"
    assert json.loads(out.read_text(encoding="utf-8"))["_meta"]["status"] == "完成"

    ScriptedClient.fail_on_review = True          # 第 0 轮全部走检查点，第 1 轮一进复盘就断线
    with pytest.raises(AssertionError, match="复盘改写阶段断线"):
        rx.main(_argv(dataset, out, iterations="1"))
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["_meta"]["status"] == "第 1 轮已完成，仍在运行"
    assert payload["_meta"]["iterations_completed"] == 1
    assert payload["_meta"]["iterations"] == 1
    assert payload["summary"]["students"] == 3
    assert len(payload["history"]) == 1


def test_allocator_crash_does_not_rebill_the_round_one_plans(dataset):
    """正式跑在筛选器阶段断过线：整轮没落盘时，1800 次学生志愿调用不能跟着一起重付。"""
    _run_once(dataset, iterations="0")
    out = dataset / "result.json"
    ckpt = Path(str(out) + ".ckpt")
    for path in ckpt.glob("iter0_*"):                 # 只留 needs.json 与 presets_0.json
        path.unlink()

    ScriptedClient.fail_on_alloc = True
    with pytest.raises(AssertionError, match="筛选器阶段断线"):
        rx.main(_argv(dataset, out, iterations="0"))
    assert ScriptedClient.plan_round1 >= 1            # 崩之前确实为志愿付过费
    journal = json.loads((ckpt / "iter0_round1_plans.json").read_text(encoding="utf-8"))
    assert len(journal["plans"]) == 3

    ScriptedClient.calls = 0
    ScriptedClient.plan_round1 = 0
    ScriptedClient.fail_on_alloc = False
    assert rx.main(_argv(dataset, out, iterations="0")) == 0
    assert ScriptedClient.plan_round1 == 0            # 重启后一个志愿调用都没再花
    assert json.loads(out.read_text(encoding="utf-8"))["summary"]["grant_events"] >= 1
