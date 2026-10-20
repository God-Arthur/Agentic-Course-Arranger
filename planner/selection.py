"""两轮选课模拟：每轮的中签名单由 LLM 决定，代码只做名额簿记与物理校验。

真实流程：教务预置课表（保留的预置课先于第一轮志愿锁定名额）→ 每一轮学生可退课、可提交志愿
→ 每轮时间结束后由 allocator（AI 筛选器）决定谁进哪个教学班。

本模块不再有任何抽签或排序算法：谁中签是模型说的。代码只拒绝物理上不成立的授予——
未提交该志愿、教学班不存在、没有空位、与已持有课程重复、时间冲突——并把拒绝次数计入
llm_stats["sanitized"]。
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .llm import count_drop
from .models import World
from .policy import validate_preferences
from .timegrid import Slot, has_conflict


@dataclass
class RoundPlanEntry:
    drops: tuple[str, ...] = ()
    prefs: dict[str, int] = field(default_factory=dict)


@dataclass
class SelectionResult:
    assignments: dict[str, set[str]]
    remaining: dict[str, int]
    events: list[dict]


class SelectionState:
    def __init__(self, world: World, preset_kept: dict[str, list[str]]):
        self.world = world
        self.assignments: dict[str, set[str]] = {sid: set() for sid in world.students}
        self.remaining = {s.section_id: s.capacity for s in world.sections.values()}
        for student_id, section_ids in preset_kept.items():
            if student_id not in self.assignments:
                raise ValueError(f"预置引用未知学生：{student_id}")
            for section_id in section_ids:
                if section_id not in self.remaining:
                    raise ValueError(f"预置引用未知教学班：{section_id}")
                if self.remaining[section_id] <= 0:
                    raise ValueError(f"预置超过教学班容量：{section_id}")
                if self.course_id_of(section_id) in self.assigned_courses(student_id):
                    raise ValueError(f"学生 {student_id} 预置了同一课程的两个教学班：{section_id}")
                if has_conflict(self.world.sections[section_id].slots, self.used_slots(student_id)):
                    raise ValueError(f"学生 {student_id} 预置课表存在时间冲突：{section_id}")
                self.assignments[student_id].add(section_id)
                self.remaining[section_id] -= 1

    def course_id_of(self, section_id: str) -> str:
        return self.world.sections[section_id].course_id

    def assigned_courses(self, student_id: str) -> set[str]:
        return {self.course_id_of(s) for s in self.assignments[student_id]}

    def used_slots(self, student_id: str) -> set[Slot]:
        slots: set[Slot] = set()
        for section_id in self.assignments[student_id]:
            slots.update(self.world.sections[section_id].slots)
        return slots

    def drop(self, student_id: str, section_id: str) -> bool:
        if section_id in self.assignments[student_id]:
            self.assignments[student_id].remove(section_id)
            self.remaining[section_id] += 1
            return True
        return False

    def grant(self, student_id: str, section_id: str) -> None:
        self.assignments[student_id].add(section_id)
        self.remaining[section_id] -= 1

    def view(self) -> dict:
        return {
            "assignments": {sid: set(sections) for sid, sections in self.assignments.items()},
            "remaining": dict(self.remaining),
        }

    @classmethod
    def from_view(cls, world: World, view: dict) -> "SelectionState":
        """从快照重建状态：长跑检查点恢复、以及筛选器分块记账都用它。"""
        state = cls(world, {})
        state.assignments = {sid: set(sections) for sid, sections in view["assignments"].items()}
        state.remaining = dict(view["remaining"])
        return state


def run_rounds(world: World, state: SelectionState, plan_builder, allocator, llm_stats: dict,
               first_round: int = 1, last_round: int = 2, on_round_end=None,
               events: list | None = None) -> SelectionResult:
    """跑 [first_round, last_round] 这些轮。

    on_round_end(轮次, 状态快照) 与 events 外传列表只服务长跑检查点：
    第一轮结束时的状态和事件能落盘，中断后就只补跑第二轮。决策逻辑没有任何变化。
    """
    if events is None:
        events = []
    for round_index in range(first_round, last_round + 1):
        plans = plan_builder(round_index, state.view())
        for student_id, entry in plans.items():
            if student_id not in state.assignments:
                raise ValueError(f"未知学生提交了选课计划：{student_id}")
            for section_id in entry.drops:
                if state.drop(student_id, section_id):
                    events.append({"round": round_index, "phase": "退课",
                                   "student_id": student_id, "section_id": section_id})
        prefs = {sid: entry.prefs for sid, entry in plans.items() if entry.prefs}
        held_sections = {sid: set(state.assignments[sid]) for sid in state.assignments}
        validate_preferences(world, prefs, held_sections)
        _apply_grants(world, state, prefs, allocator(round_index, prefs, state.view()),
                      round_index, events, llm_stats)
        if on_round_end is not None:
            on_round_end(round_index, state.view())
    return SelectionResult(state.assignments, state.remaining, events)


def run_two_rounds(world: World, preset_kept: dict[str, list[str]],
                   plan_builder, allocator, llm_stats: dict) -> SelectionResult:
    """plan_builder(round_index, view) -> {student_id: RoundPlanEntry}；
    allocator(round_index, prefs, view) -> [(student_id, section_id)] 由 AI 给出。"""
    return run_rounds(world, SelectionState(world, preset_kept), plan_builder, allocator,
                      llm_stats, 1, 2)


def _apply_grants(world: World, state: SelectionState, prefs: dict[str, dict[str, int]],
                  grants, round_index: int, events: list[dict], llm_stats: dict) -> None:
    for grant in grants or []:
        student_id, section_id = grant
        reason = _reject_reason(world, state, prefs, student_id, section_id)
        if reason is not None:
            count_drop(llm_stats, "grant_reject")
            events.append({"round": round_index, "phase": "拒发", "student_id": student_id,
                           "section_id": section_id, "reason": reason})
            continue
        state.grant(student_id, section_id)
        events.append({"round": round_index, "phase": "中签", "rank": prefs[student_id][section_id],
                       "student_id": student_id, "section_id": section_id})


def _reject_reason(world: World, state: SelectionState, prefs: dict[str, dict[str, int]],
                   student_id: str, section_id: str) -> str | None:
    if student_id not in state.assignments:
        return "未知学生"
    if section_id not in world.sections:
        return "未知教学班"
    if prefs.get(student_id, {}).get(section_id) is None:
        return "该生未提交这个教学班的志愿"
    if state.remaining[section_id] <= 0:
        return "教学班已无空位"
    if world.sections[section_id].course_id in state.assigned_courses(student_id):
        return "已持有同一课程的另一教学班"
    if has_conflict(world.sections[section_id].slots, state.used_slots(student_id)):
        return "与已选课程时间冲突"
    return None
