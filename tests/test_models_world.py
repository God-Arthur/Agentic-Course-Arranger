"""World 装载与索引：真实目录有 4000+ 教学班，查表索引必须与全量扫描口径一致。"""
import pytest

from planner.models import category_key, load_world

from fixtures import demo_world, tiny_world


def _brute_sections(data, course_id):
    return [s["section_id"] for s in data["sections"] if s["course_id"] == course_id]


def _brute_courses(data, key):
    return [c["course_id"] for c in data["courses"] if category_key(c["category"]) == key]


def test_world_indexes_agree_with_full_scan():
    for factory in (tiny_world, lambda: demo_world(30)):
        data = factory()
        world = load_world(data)
        for course in data["courses"]:
            assert world.sections_of(course["course_id"]) == _brute_sections(data, course["course_id"])
        for key in world.category_keys:
            assert [c.course_id for c in world.courses_of_key(key)] == _brute_courses(data, key)
        assert world.sections_of("不存在的课程") == []


def test_weekend_slots_are_rejected():
    """网格只有周一至周五（发起人 2026-10-19 定：不考虑周末），装载时必须挡住而不是静默丢弃。"""
    data = tiny_world()
    data["sections"].append({"section_id": "PE9", "course_id": "PE", "capacity": 30,
                             "slots": ["Sat-11", "Sun-12"]})
    with pytest.raises(ValueError, match="非法星期"):
        load_world(data)
