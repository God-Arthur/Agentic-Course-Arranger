"""供给侧扩容表：专用座位不串专业，建议增座等于缺口向上取整。"""
import json
import math
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "tools"))

import supply_table as supply  # noqa: E402
from planner.models import load_world  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def test_dedicated_seats_stay_with_their_major_and_extra_seats_ceil_the_gap():
    data = {
        "courses": [
            {"course_id": "PA", "name": "甲专业课", "category": "专业课", "major": "甲"},
            {"course_id": "PB", "name": "乙专业课", "category": "专业课", "major": "乙"},
            {"course_id": "PS", "name": "未分专业课", "category": "专业课"},
            {"course_id": "PE", "name": "体育", "category": "体育课"},
        ],
        "sections": [
            {"section_id": "PA1", "course_id": "PA", "capacity": 1, "slots": ["Mon-1"]},
            {"section_id": "PB1", "course_id": "PB", "capacity": 5, "slots": ["Mon-2"]},
            {"section_id": "PS1", "course_id": "PS", "capacity": 3, "slots": ["Mon-3"]},
            {"section_id": "PE1", "course_id": "PE", "capacity": 4, "slots": ["Thu-11"]},
        ],
        "plan": {
            "majors": ["甲", "乙"],
            "recommended_by_year": {
                "甲": {"一": {"专业课": 2}},
                "乙": {"一": {"专业课": 1}},
            },
            "need_rules": {"pe_courses_per_student": 1, "language_share_per_student": 0.0},
        },
    }
    world = load_world(data)
    table = supply.expansion_table(world, [("甲", "一"), ("甲", "一"), ("乙", "一"), ("乙", "一")])
    rows = {(row["major"], row["category"]): row for row in table["rows"]}
    jia = rows[("甲", "专业课")]
    yi = rows[("乙", "专业课")]
    assert jia["demand_slots"] == 4
    assert jia["dedicated_seats"] == 1
    assert yi["dedicated_seats"] == 5
    assert jia["dedicated_seats"] != yi["dedicated_seats"]
    # 共享 3 座按需求 4:2 分，甲分到 2，供给 3，缺口 1，增座 1。
    assert jia["fair_shared_seats"] == 2
    assert jia["extra_seats"] == math.ceil(jia["gap_slots"] - 1e-9)
    assert yi["extra_seats"] == 0
    pe = rows[("甲", "通识与体育类")]
    assert pe["demand_slots"] == 2  # 两名甲专业学生各加 1 门体育
    assert pe["extra_seats"] == 0   # 4 个体育座位按两人需求对半，每人分到 2


def test_language_quota_uses_largest_remainder_and_sums_to_the_rounded_total():
    counts = {("甲", "一"): 2, ("乙", "一"): 1}
    extras = supply.language_extras(counts, 0.5)
    assert sum(extras.values()) == round(3 * 0.5)


@pytest.mark.skipif(not (ROOT / "data" / "courses_k10.json").exists()
                     or not (ROOT / "data" / "major_sizes.json").exists(),
                     reason="缩尺目录或人数表不在这个环境里")
def test_k10_expansion_table_keeps_professional_seats_inside_the_major():
    sys.path.insert(0, str(ROOT / "tools"))
    import run_experiment as rx

    world = load_world(json.loads((ROOT / "data" / "courses_k10.json").read_text(encoding="utf-8")))
    sizes = json.loads((ROOT / "data" / "major_sizes.json").read_text(encoding="utf-8"))
    allocation = rx.allocate_population(1800, world, {k: int(v) for k, v in sizes["per_grade"].items()})
    table = supply.expansion_table(world, allocation)
    assert table["students"] == 1800
    assert table["positive_gap_cells"] > 0
    for row in table["rows"]:
        if row["gap_slots"] <= 0:
            assert row["extra_seats"] == 0
        else:
            assert row["extra_seats"] == math.ceil(row["gap_slots"] - 1e-9)
    cs = next(row for row in table["rows"]
              if row["major"] == "计算机科学与技术" and row["category"] == "专业课")
    other = next(row for row in table["rows"]
                 if row["major"] != "计算机科学与技术" and row["category"] == "专业课")
    assert cs["dedicated_seats"] != other["dedicated_seats"] or cs["dedicated_seats"] == 0
