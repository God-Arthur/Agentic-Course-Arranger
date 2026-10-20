"""AI 排课与 AI 复盘改写：预置课表全部由 LLM 决定。

代码只做事实校验：教学班是否存在、还有没有余座、时间是否冲突、同一门课程是否重复。
校验剔除的条目计入 llm_stats["sanitized"]；LLM 调用失败直接上抛 LLMError，不出任何替代结果。
"""
from __future__ import annotations

import threading
from collections import Counter

from .behavior import _assigned_lines, _catalog_lines, _unmet_lines
from .llm import LLMClient, count_drop, parallel_map
from .models import NeedModel, World
from .timegrid import has_conflict

_PRESET_SYSTEM = (
    "你是教务系统的预置课表规划器，为一名学生排出本学期预置课表。约束："
    "1) 只能使用给出的候选教学班ID；2) 同一门课程只选一个教学班；"
    "3) 预置课表内上课时间不得冲突；4) 先把标了（必修）的类别按目标门数排满，再排其他类别；"
    "5) 尽量满足备注里的时间偏好。"
    '只输出 JSON：{"sections": ["教学班ID", "..."], "why": "一句话"}'
)


def _validated(world: World, picked: list, remaining: dict[str, int],
               llm_stats: dict) -> list[str]:
    """按模型给出的先后顺序接受条目，只剔除物理上非法的（不存在/无余座/时间冲突/同课重复）。"""
    chosen: list[str] = []
    used_slots: set = set()
    used_courses: set[str] = set()
    for item in picked:
        section_id = str(item)
        section = world.sections.get(section_id)
        if section is None:
            count_drop(llm_stats, "preset_unknown_section")
            continue
        if remaining[section_id] <= 0:
            count_drop(llm_stats, "preset_no_seat")
            continue
        if section.course_id in used_courses:
            count_drop(llm_stats, "preset_same_course")
            continue
        if has_conflict(section.slots, used_slots):
            count_drop(llm_stats, "preset_time_conflict")
            continue
        chosen.append(section_id)
        remaining[section_id] -= 1
        used_courses.add(section.course_id)
        used_slots.update(section.slots)
    return chosen


def _preset_user(world: World, need: NeedModel, skill_text: str,
                 remaining_view: dict, review: tuple = ()) -> str:
    return "\n".join([
        "【你的 .skill 档案】", skill_text,
        "【目标完成情况】", *_unmet_lines(need, Counter()),
        *review,
        "【候选教学班】", *_catalog_lines(world, {"remaining": remaining_view}, need),
        "请排出本学期的预置课表。",
    ])


def _plan_one(world: World, client: LLMClient, llm_stats: dict, need: NeedModel,
              skill_text: str, remaining: dict[str, int], review: tuple = ()) -> list[str]:
    raw = client.chat_json(system=_PRESET_SYSTEM,
                           user=_preset_user(world, need, skill_text, remaining, review))
    return _validated(world, list(raw.get("sections") or []), remaining, llm_stats)


def _plan_batch(world: World, client: LLMClient, llm_stats: dict,
                items: list[tuple[str, NeedModel, str, tuple]],
                remaining: dict[str, int], workers: int) -> dict[str, list[str]]:
    """并发排多名学生的预置课表。

    余座由一把锁记账：拿到的是发起调用那一刻的快照，返回后再在锁内落账。
    同时并发的几名学生彼此看不见对方刚占的座位，撞车的条目会被 _validated 剔掉
    （计入 sanitized），那个类别就留给两轮选课阶段补——顺序模式下"后一名看见前一名占座"
    在这里退化成"同批并发的学生共享一个轮初快照"，这是为把长跑压进可接受时长做的取舍。
    """
    lock = threading.Lock()

    def plan(entry):
        sid, need, skill_text, review = entry
        with lock:
            snapshot = dict(remaining)
        raw = client.chat_json(system=_PRESET_SYSTEM,
                               user=_preset_user(world, need, skill_text, snapshot, review))
        with lock:
            return sid, _validated(world, list(raw.get("sections") or []), remaining, llm_stats)

    return dict(parallel_map(items, plan, workers))


def ai_presets(world: World, needs: dict[str, NeedModel], skill_texts: dict[str, str],
               client: LLMClient, llm_stats: dict, workers: int = 1) -> dict[str, list[str]]:
    """AI 排课：按学号顺序逐个学生生成预置课表，前一名学生占掉的余座对后一名可见。

    workers>1 时改用上面那段并发口径（同批共享轮初快照）。
    """
    remaining = {s.section_id: s.capacity for s in world.sections.values()}
    if workers <= 1:
        return {sid: _plan_one(world, client, llm_stats, needs[sid], skill_texts[sid], remaining)
                for sid in sorted(needs)}
    items = [(sid, needs[sid], skill_texts[sid], ()) for sid in sorted(needs)]
    return _plan_batch(world, client, llm_stats, items, remaining, workers)


def ai_retune(world: World, needs: dict[str, NeedModel], skill_texts: dict[str, str],
              client: LLMClient, llm_stats: dict, preset_map: dict[str, list[str]],
              scores: dict[str, object], reasons: dict[str, str],
              final_assignments: dict[str, set[str]], frac: float = 0.3,
              workers: int = 1) -> dict[str, list[str]]:
    """AI 复盘改写：满意度最低的一批学生连同上一轮结果交回模型重排，其余学生原样保留。"""
    refined = {sid: list(sections) for sid, sections in preset_map.items()}
    if not scores:
        return refined
    ranked = sorted(scores, key=lambda sid: (scores[sid], sid))
    worst = set(ranked[:max(1, round(len(ranked) * frac))])
    remaining = {s.section_id: s.capacity for s in world.sections.values()}
    for sid, sections in refined.items():
        if sid in worst:
            continue
        for section_id in sections:
            remaining[section_id] -= 1
    targets = sorted(worst & set(refined))
    items = [(sid, needs[sid], skill_texts[sid], (
        "【上一版预置课表】", *_assigned_lines(world, set(preset_map.get(sid, ()))),
        "【两轮选课后的最终课表】", *_assigned_lines(world, final_assignments.get(sid, set())),
        f"【上一轮满意度】{scores.get(sid)}——{reasons.get(sid, '')}",
    )) for sid in targets]
    if workers <= 1:
        for entry in items:
            sid, need, skill_text, review = entry
            refined[sid] = _plan_one(world, client, llm_stats, need, skill_text,
                                     remaining, review)
    else:
        refined.update(_plan_batch(world, client, llm_stats, items, remaining, workers))
    return refined
