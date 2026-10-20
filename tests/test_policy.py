import pytest

from planner.models import load_world
from planner.policy import tier_of, validate_preferences

from fixtures import tiny_world


def _students():
    from planner.models import Student
    return {
        "G1": Student("G1", "计算机科学与技术", graduating=True),
        "T1": Student("T1", "计算机科学与技术", transfer=True),
        "M1": Student("M1", "计算机科学与技术", campus_move=True),
        "N1": Student("N1", "计算机科学与技术"),
        "N2": Student("N2", "数学与应用数学"),
    }


def test_category_merge_key():
    world = load_world(tiny_world())
    assert world.course_of_section("C1").category == "专业课"
    from planner.models import category_key
    assert category_key("通识核心课") == "通识与体育类"
    assert category_key("体育课") == "通识与体育类"
    assert category_key("思政类") == "思政类"


def test_tier_order_matches_spec():
    world = load_world(tiny_world())
    students = _students()
    specialty = world.courses["C"]
    foreign = world.courses["B"]
    assert tier_of(students["G1"], specialty) == 0
    assert tier_of(students["T1"], specialty) == 1
    assert tier_of(students["M1"], specialty) == 2
    # 本专业学生选专业课是第 4 优先级；外专业学生与普通学生在非专业课上同级。
    assert tier_of(students["N1"], specialty) == 3
    assert tier_of(students["N2"], specialty) == 4
    assert tier_of(students["N1"], foreign) == 4
    assert tier_of(students["G1"], foreign) == 0


def test_validate_ok_prefs():
    world = load_world(tiny_world())
    world.students = _students()
    validate_preferences(world, {"N1": {"B1": 1, "B2": 2, "A1": 1}})


def test_validate_rejects_gap_ranks():
    world = load_world(tiny_world())
    world.students = _students()
    with pytest.raises(ValueError):
        validate_preferences(world, {"N1": {"B2": 2}})


def test_validate_rejects_fourth_pref_in_merged_category():
    world = load_world(tiny_world())
    world.students = _students()
    prefs = {"N1": {"PE1": 1, "PE2": 2, "GE1": 1, "GE2": 2}}
    with pytest.raises(ValueError, match="通识与体育类"):
        validate_preferences(world, prefs)


def test_validate_rejects_bad_rank():
    world = load_world(tiny_world())
    world.students = _students()
    with pytest.raises(ValueError):
        validate_preferences(world, {"N1": {"B1": 4}})


def test_validate_rejects_unknown_section_or_student():
    world = load_world(tiny_world())
    world.students = _students()
    with pytest.raises(ValueError):
        validate_preferences(world, {"N1": {"ZZ": 1}})
    with pytest.raises(ValueError):
        validate_preferences(world, {"XX": {"B1": 1}})


def test_load_world_rejects_bad_category():
    data = tiny_world()
    data["courses"][0]["category"] = "魔法类"
    with pytest.raises(ValueError):
        load_world(data)
