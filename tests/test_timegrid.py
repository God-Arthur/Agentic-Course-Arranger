import pytest

from planner.timegrid import (all_slots, has_conflict, parse_slot, period_label,
                              slot_key, slot_label)


def test_parse_slot_accepts_english_and_chinese():
    assert parse_slot("Mon-3") == ("Mon", 3)
    assert parse_slot("周三-5") == ("Wed", 5)
    assert parse_slot("fri-13") == ("Fri", 13)


def test_parse_slot_rejects_weekend():
    # 发起人 2026-10-19 定：不考虑周末。周末班在转换阶段就被剔除，网格也不接受，
    # 这样万一有周末时间漏进来会立刻报错，而不是静默画不到课表上。
    with pytest.raises(ValueError):
        parse_slot("Sat-1")
    with pytest.raises(ValueError):
        parse_slot("周日-13")


def test_parse_slot_rejects_bad_input():
    with pytest.raises(ValueError):
        parse_slot("Mo-1")
    with pytest.raises(ValueError):
        parse_slot("Mon-14")
    with pytest.raises(ValueError):
        parse_slot("Mon")
    with pytest.raises(ValueError):
        parse_slot("Mon-0")


def test_period_labels():
    assert period_label(1) == "上午1"
    assert period_label(5) == "上午5"
    assert period_label(6) == "下午1"
    assert period_label(10) == "下午5"
    assert period_label(11) == "晚上1"
    assert period_label(13) == "晚上3"
    with pytest.raises(ValueError):
        period_label(14)


def test_conflicts():
    assert has_conflict([("Mon", 1)], [("Mon", 1), ("Tue", 2)])
    assert not has_conflict([("Mon", 1)], [("Mon", 2)])


def test_slot_key_and_label():
    slot = ("Tue", 11)
    assert slot_label(slot) == "周二晚上1"
    assert slot_key(slot) == "Tue-11"
    assert parse_slot(slot_key(slot)) == slot
    assert parse_slot("fri-13") == ("Fri", 13)


def test_all_slots_shape():
    slots = all_slots()
    assert len(slots) == 5 * 13
