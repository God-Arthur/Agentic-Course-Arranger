"""名额簿记与物理校验层的用例。

谁中签由 AI 筛选器决定，这些用例只验证「模型给出的授予里，物理不成立的那些会被拒」——
测的是代码正确性，不是任何排课方案的质量，不能拿来当模拟结果的验收。
"""
import pytest

from planner.models import Student, load_world
from planner.selection import RoundPlanEntry, SelectionState, run_two_rounds

from fixtures import tiny_world


def make_world():
    world = load_world(tiny_world())
    world.students = {
        "G1": Student("G1", "计算机科学与技术", graduating=True),
        "T1": Student("T1", "计算机科学与技术", transfer=True),
        "N1": Student("N1", "计算机科学与技术"),
        "N2": Student("N2", "数学与应用数学"),
    }
    return world


def stats():
    return {"calls": 0, "failures": 0, "sanitized": 0}


def no_plans(round_index, view):
    return {}


def builder_of(world, plans):
    """同一个学生在两轮里不会重复申报已到手课程的志愿（真实行为层也会这样剔除）。"""
    def build(round_index, view):
        out = {}
        for student_id, entry in plans.items():
            held_courses = {world.sections[h].course_id for h in view["assignments"][student_id]}
            prefs = {s: r for s, r in entry.prefs.items()
                     if s not in view["assignments"][student_id]
                     and world.sections[s].course_id not in held_courses}
            out[student_id] = RoundPlanEntry(drops=entry.drops, prefs=prefs)
        return out
    return build


def grant_all(round_index, prefs, view):
    """把每张志愿都当作获批，交给校验层逐条拒绝——测试里不引入任何取舍规则。"""
    return [(sid, section_id) for sid, student_prefs in prefs.items() for section_id in student_prefs]


def grant(section_id, student_id, rounds=(1, 2)):
    return lambda round_index, prefs, view: (
        [(student_id, section_id)] if round_index in rounds else [])


def check_invariants(world, result):
    for sid, sections in result.assignments.items():
        courses = [world.sections[s].course_id for s in sections]
        assert len(courses) == len(set(courses)), f"{sid} 持有同一课程的两个教学班"
        slots = [slot for s in sections for slot in world.sections[s].slots]
        assert len(slots) == len(set(slots)), f"{sid} 最终课表有时间冲突"
    for sec_id, left in result.remaining.items():
        assert 0 <= left <= world.sections[sec_id].capacity
    for sec_id in world.sections:
        granted = sum(1 for secs in result.assignments.values() if sec_id in secs)
        assert granted == world.sections[sec_id].capacity - result.remaining[sec_id]


def test_preset_occupies_a_seat_before_round1():
    world = make_world()
    result = run_two_rounds(world, {"G1": ["A1"]}, no_plans, grant_all, stats())
    assert result.remaining["A1"] == 0
    check_invariants(world, result)


def test_the_model_decides_who_wins_when_seats_are_short():
    world = make_world()
    plans = {"N1": RoundPlanEntry(prefs={"B1": 1}), "N2": RoundPlanEntry(prefs={"B1": 1})}
    audit = stats()
    result = run_two_rounds(world, {}, builder_of(world, plans),
                            grant("B1", "N2", rounds=(1,)), audit)
    assert "B1" in result.assignments["N2"]
    assert "B1" not in result.assignments["N1"]
    assert audit["sanitized"] == 0
    check_invariants(world, result)


def test_a_grant_that_over_books_a_section_is_rejected():
    world = make_world()
    plans = {"N1": RoundPlanEntry(prefs={"B1": 1}), "N2": RoundPlanEntry(prefs={"B1": 1})}
    audit = stats()
    result = run_two_rounds(world, {}, builder_of(world, plans), grant_all, audit)
    assert audit["sanitized"] == 2          # 第一轮 N2 超授，第二轮 N2 再试一次仍超授
    assert result.remaining["B1"] == 0
    assert {e["reason"] for e in result.events if e["phase"] == "拒发"} == {"教学班已无空位"}
    check_invariants(world, result)


def test_a_grant_for_a_pref_that_was_never_submitted_is_rejected():
    world = make_world()
    plans = {"N1": RoundPlanEntry(prefs={"B1": 1})}
    audit = stats()
    result = run_two_rounds(world, {}, builder_of(world, plans), grant("C1", "N1"), audit)
    assert audit["sanitized"] == 2
    assert "C1" not in result.assignments["N1"]
    assert "B1" not in result.assignments["N1"]  # 筛选器没批的志愿不会自己生效


def test_a_second_section_of_the_same_course_is_rejected():
    world = make_world()
    plans = {"N1": RoundPlanEntry(prefs={"B1": 1, "B2": 2})}
    audit = stats()
    result = run_two_rounds(world, {}, builder_of(world, plans), grant_all, audit)
    assert result.assignments["N1"] == {"B1"}
    assert audit["sanitized"] == 1
    check_invariants(world, result)


def test_a_grant_that_clashes_with_the_preset_timetable_is_rejected():
    world = make_world()
    plans = {"N1": RoundPlanEntry(prefs={"B1": 1})}   # B1 与预置 A1 同为 Mon-1
    audit = stats()
    result = run_two_rounds(world, {"N1": ["A1"]}, builder_of(world, plans), grant_all, audit)
    assert result.assignments["N1"] == {"A1"}
    assert {e["reason"] for e in result.events if e["phase"] == "拒发"} == {"与已选课程时间冲突"}


def test_a_drop_in_round2_releases_the_seat_for_someone_else():
    world = make_world()

    def build(round_index, view):
        if round_index == 1:
            return {"G1": RoundPlanEntry(), "N1": RoundPlanEntry(prefs={"A1": 1})}
        return {"G1": RoundPlanEntry(drops=("A1",)), "N1": RoundPlanEntry(prefs={"A1": 1})}

    result = run_two_rounds(world, {"G1": ["A1"]}, build, grant_all, stats())
    assert "A1" in result.assignments["N1"]
    assert "A1" not in result.assignments["G1"]
    drops = [e for e in result.events if e["phase"] == "退课"]
    assert drops and drops[0]["round"] == 2
    check_invariants(world, result)


def test_a_winning_grant_records_the_rank_it_was_filled_by():
    world = make_world()
    plans = {"N1": RoundPlanEntry(prefs={"B1": 1, "B2": 2})}

    def build(round_index, view):
        return plans if round_index == 1 else {}

    result = run_two_rounds(world, {}, build, grant("B2", "N1", rounds=(1,)), stats())
    wins = [e for e in result.events if e["phase"] == "中签"]
    assert wins == [{"round": 1, "phase": "中签", "rank": 2,
                     "student_id": "N1", "section_id": "B2"}]
    assert result.remaining["B2"] == 0


def test_a_pref_for_an_already_held_course_is_rejected_before_allocation():
    world = make_world()
    plans = {"N1": RoundPlanEntry(prefs={"A1": 1})}
    with pytest.raises(ValueError, match="已持有"):
        run_two_rounds(world, {"N1": ["A1"]}, lambda r, v: plans, grant_all, stats())


def test_an_unknown_student_cannot_submit_a_plan():
    world = make_world()
    plans = {"ZZ": RoundPlanEntry(prefs={"A1": 1})}
    with pytest.raises(ValueError, match="未知学生"):
        run_two_rounds(world, {}, lambda r, v: plans, grant_all, stats())


def test_preset_validation_covers_capacity_conflict_and_duplicates():
    world = make_world()
    state = SelectionState(world, {"N1": ["A1"]})
    assert state.remaining["A1"] == 0 and state.assignments["N1"] == {"A1"}
    with pytest.raises(ValueError, match="超过教学班容量"):
        SelectionState(world, {"N1": ["A1"], "N2": ["A1"]})   # A1 只有 1 个座位
    with pytest.raises(ValueError, match="时间冲突"):
        SelectionState(world, {"N1": ["A1", "B1"]})
    with pytest.raises(ValueError, match="同一课程"):
        SelectionState(world, {"N1": ["B1", "B2"]})
    with pytest.raises(ValueError, match="未知教学班"):
        SelectionState(world, {"N1": ["XXX"]})
    with pytest.raises(ValueError, match="未知学生"):
        SelectionState(world, {"ZZ": ["A1"]})
