import random

import pytest

from planner.llm import LLMError
from planner.models import load_world
from planner.skills import (_normalize_category, format_skill, parse_skill_llm,
                            skill_student_id, synthesize_students)

from fixtures import demo_world


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.failures = 0

    def chat_json(self, system, user):
        self.calls += 1
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def test_normalize_category_maps_labels_and_keeps_merged_key():
    assert _normalize_category("思政类") == "思政类"
    assert _normalize_category("体育课") == "通识与体育类"
    assert _normalize_category("通识核心课") == "通识与体育类"
    assert _normalize_category("通识与体育类") == "通识与体育类"


def test_normalize_category_rejects_unknown_category():
    with pytest.raises(ValueError):
        _normalize_category("魔法类")


def test_skill_student_id_reads_only_the_header():
    assert skill_student_id("@SKILL S001\n专业：计算机科学与技术") == "S001"
    with pytest.raises(ValueError, match="@SKILL"):
        skill_student_id("专业：计算机科学与技术")


def test_synthesize_students_deterministic_and_shaped():
    world = load_world(demo_world(scale=30))
    students_a, texts_a = synthesize_students(12, world, random.Random(7))
    students_b, texts_b = synthesize_students(12, world, random.Random(7))
    assert set(students_a) == {f"S{i:03d}" for i in range(1, 13)}
    assert texts_a == texts_b
    catalog_keys = set(world.category_keys)
    for sid, text in texts_a.items():
        assert skill_student_id(text) == sid
        lines = [line for line in text.splitlines() if line.startswith("本学期修读目标：")]
        assert len(lines) == 1, f"{sid} 的修读目标行不唯一"
        parts = [p for p in lines[0].split("：", 1)[1].split("；") if p.strip()]
        assert parts, f"{sid} 没有修读目标"
        for part in parts:
            key, _, count = part.rpartition(" ")
            assert key in catalog_keys, f"{sid} 的目标类别 {key} 不在开课目录里"
            assert int(count) >= 1
    assert any(s.graduating for s in students_a.values()), "合成学生里必须有毕业年级样本"
    assert any(s.transfer for s in students_a.values())
    assert any(s.campus_move for s in students_a.values())


def test_format_skill_carries_profile_fields():
    from planner.models import Student
    student = Student("S009", "计算机科学与技术", graduating=True,
                      completed=("已修课程1",))
    text = format_skill(student, {"思政类": 1}, ["Mon-1"], "没有特别偏好。")
    assert skill_student_id(text) == "S009"
    assert "年级：毕业年级" in text
    assert "本学期修读目标：思政类 1" in text
    assert "尽量避开：Mon-1" in text


def test_parse_skill_llm_accepts_valid_output():
    client = FakeClient([{"required": {"思政类": 1}, "elective": {"体育课": 1},
                          "avoid_slots": ["Mon-1"], "notes": "x"}])
    text = "@SKILL S001\n专业：计算机科学与技术\n本学期修读目标：思政类 1\n尽量避开：无\n备注：无"
    need = parse_skill_llm(text, client)
    assert need.student_id == "S001"
    assert need.source == "llm"
    assert need.required == {"思政类": 1}
    assert need.elective == {"通识与体育类": 1}
    assert need.avoid_slots == (("Mon", 1),)


def test_parse_skill_llm_raises_when_the_call_fails():
    # 需求解析没有规则替代通路：调用失败必须把整轮跑废，而不是悄悄换成规则需求。
    client = FakeClient([LLMError("端点超时")])
    text = "@SKILL S002\n本学期修读目标：思政类 2\n尽量避开：无\n备注：无"
    with pytest.raises(LLMError):
        parse_skill_llm(text, client)


def test_parse_skill_llm_drops_categories_that_do_not_exist():
    # 分类名不在九类里 = 指向世界上不存在的字段，剔掉并计数；
    # 1800 次解析里出现一次不能让几小时的长跑整轮作废（2026-10-20 实测 Wed-14 打崩过一次 n=100）。
    client = FakeClient([{"required": {"魔法类": 1, "思政类": 2}}])
    stats = {}
    text = "@SKILL S003\n本学期修读目标：思政类 1\n尽量避开：无\n备注：无"
    need = parse_skill_llm(text, client, stats)
    assert need.required == {"思政类": 2}
    assert stats["sanitized"] == 1


def test_parse_skill_llm_drops_slots_outside_the_grid():
    client = FakeClient([{"required": {"思政类": 1}, "avoid_slots": ["Mon-1", "Wed-14", None]}])
    stats = {}
    text = "@SKILL S004\n本学期修读目标：思政类 1\n尽量避开：无\n备注：无"
    need = parse_skill_llm(text, client, stats)
    assert need.avoid_slots == (("Mon", 1),)
    assert stats["sanitized"] == 2
