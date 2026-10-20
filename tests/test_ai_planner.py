import pytest

from planner.ai_planner import ai_presets, ai_retune
from planner.llm import LLMError
from planner.models import NeedModel, Student, load_world

from fixtures import tiny_world


class FakeClient:
    """按脚本回复，并记下每次 (system, user)，用于检查交给模型的上下文。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.prompts = []
        self.calls = 0
        self.failures = 0

    def chat_json(self, system, user):
        self.calls += 1
        self.prompts.append((system, user))
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _world(*student_ids):
    world = load_world(tiny_world())
    world.students = {sid: Student(sid, "计算机科学与技术") for sid in student_ids}
    return world


def _needs(*student_ids, **kwargs):
    return {sid: NeedModel(student_id=sid, **kwargs) for sid in student_ids}


def test_ai_presets_drops_only_illegal_entries():
    world = _world("N1")
    needs = _needs("N1", required={"外语类": 1})
    client = FakeClient([{"sections": ["B1", "ZZZ", "B2", "A1"], "why": "先外语"}])
    llm_stats = {}
    presets = ai_presets(world, needs, {"N1": "@SKILL N1"}, client, llm_stats)
    # B2 与 B1 同一门课程 → 剔除；ZZZ 不存在 → 剔除；A1 与 B1 同为周一第1节 → 冲突剔除
    assert presets == {"N1": ["B1"]}
    assert llm_stats["sanitized"] == 3


def test_ai_presets_never_books_a_section_beyond_capacity():
    world = _world("N1", "N2")
    needs = dict(_needs("N1", required={"思政类": 1}), **_needs("N2", required={"思政类": 1}))
    client = FakeClient([{"sections": ["A1"]}, {"sections": ["A1"]}])  # A1 容量只有 1
    presets = ai_presets(world, needs, {"N1": "@SKILL N1", "N2": "@SKILL N2"}, client, {})
    assert presets["N1"] == ["A1"]
    assert presets["N2"] == []  # 后一名学生看到的余座已被占完


def test_ai_presets_prompt_carries_required_marks_and_candidates():
    world = _world("N1")
    needs = _needs("N1", required={"思政类": 1})
    client = FakeClient([{"sections": [], "why": "没有合适班"}])
    ai_presets(world, needs, {"N1": "@SKILL N1\n本学期修读目标：思政类 1"}, client, {})
    system, user = client.prompts[0]
    assert "预置课表规划器" in system
    assert "思政类（必修）：目标 1，已有 0" in user
    assert "A1|思政A" in user


def test_ai_presets_raises_when_the_call_fails():
    world = _world("N1")
    needs = _needs("N1", required={"思政类": 1})
    client = FakeClient([LLMError("端点挂了")])
    with pytest.raises(LLMError):
        ai_presets(world, needs, {"N1": "@SKILL N1"}, client, {})


def test_ai_retune_replans_only_the_worst_and_keeps_others_seats():
    world = _world("N1", "N2")
    needs = dict(_needs("N1", required={"外语类": 1}), **_needs("N2", required={"外语类": 1}))
    preset_map = {"N1": [], "N2": ["B2"]}
    # B2 容量 1，已被 N2 占走：最差学生 N1 即使想抢也抢不到，只能拿到 B1（Mon-1）
    client = FakeClient([{"sections": ["B2", "B1"], "why": "换个时间"}])
    refined = ai_retune(world, needs, {"N1": "@SKILL N1", "N2": "@SKILL N2"}, client, {},
                        preset_map, {"N1": 30, "N2": 90}, {"N1": "课太早", "N2": "满意"},
                        {"N1": set(), "N2": {"B2"}}, frac=0.5)
    assert refined == {"N1": ["B1"], "N2": ["B2"]}
    assert client.calls == 1  # 只重排最差的那一名
    assert "【上一轮满意度】30——课太早" in client.prompts[0][1]
    assert "【两轮选课后的最终课表】" in client.prompts[0][1]


def test_ai_presets_with_workers_never_books_beyond_capacity():
    """并发排课时余座由一把锁记账：8 名学生在同一节课抢 1 个座位，只能落地 1 次。"""
    ids = [f"N{i}" for i in range(8)]
    world = _world(*ids)
    needs = _needs(*ids, required={"思政类": 1})
    texts = {sid: f"@SKILL {sid}" for sid in ids}
    client = FakeClient([{"sections": ["A1"], "why": "同一节"} for _ in ids])
    stats = {}
    presets = ai_presets(world, needs, texts, client, stats, workers=4)
    booked = [section_id for sections in presets.values() for section_id in sections]
    assert booked == ["A1"]
    assert stats["sanitized"] == 7
    assert len(client.prompts) == 8


def test_ai_presets_sequential_and_parallel_agree_on_totals():
    ids = [f"N{i}" for i in range(4)]
    needs = _needs(*ids, required={"外语类": 1})
    texts = {sid: f"@SKILL {sid}" for sid in ids}
    replies = [{"sections": ["B1"], "why": "外语"}, {"sections": ["B2"], "why": "外语"},
               {"sections": ["B1"], "why": "外语"}, {"sections": ["B2"], "why": "外语"}]
    sequential = ai_presets(_world(*ids), needs, texts, FakeClient(list(replies)), {}, 1)
    parallel = ai_presets(_world(*ids), needs, texts, FakeClient(list(replies)), {}, 4)
    assert sum(len(v) for v in sequential.values()) == sum(len(v) for v in parallel.values())
