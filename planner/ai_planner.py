"""AI 排课与 AI 复盘改写：预置课表全部由 LLM 决定。

代码只做事实校验：教学班是否存在、还有没有余座、时间是否冲突、同一门课程是否重复。
校验剔除的条目计入 llm_stats["sanitized"]；LLM 调用失败直接上抛 LLMError，不出任何替代结果。

长跑可以按批落盘：resume 里的学生不再发起调用，flush_every 人一批把已排完的结果交回调用方。
group_size>1 时把多名学生打进同一次调用（批量请求）；默认仍是一人一次，提示词与旧行为一致。
"""
from __future__ import annotations

import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed

from .behavior import _assigned_lines, _catalog_lines, _unmet_lines
from .llm import LLMClient, LLMError, count_drop
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


_PRESET_BATCH_SYSTEM = (
    _PRESET_SYSTEM
    + " 本次输入含多名学生，必须按学号分别给方案，不能漏人。"
    + '只输出 JSON：{"plans": [{"student_id": "学号", "sections": ["教学班ID"], "why": "一句话"}]}'
)


def _occupy(remaining: dict[str, int], section_ids: list[str], student_id: str) -> None:
    """把已经落盘的预置占座扣回余座。可行的一组课表按任何顺序扣都不会超座。"""
    for section_id in section_ids:
        if section_id not in remaining:
            raise ValueError(f"检查点里 {student_id} 引用了未知教学班 {section_id}")
        if remaining[section_id] <= 0:
            raise ValueError(f"检查点里 {student_id} 的教学班 {section_id} 超出容量")
        remaining[section_id] -= 1


def _chunks(items: list, size: int) -> list[list]:
    if not items:
        return []
    step = len(items) if size <= 0 else size
    return [items[index:index + step] for index in range(0, len(items), step)]


def _plan_batch(world: World, client: LLMClient, llm_stats: dict,
                items: list[tuple[str, NeedModel, str, tuple]],
                remaining: dict[str, int], workers: int) -> dict[str, list[str]]:
    """并发排多名学生的预置课表。

    余座由一把锁记账：拿到的是发起调用那一刻的快照，返回后再在锁内落账。
    同时并发的几名学生彼此看不见对方刚占的座位，撞车的条目会被 _validated 剔掉
    （计入 sanitized），那个类别就留给两轮选课阶段补——顺序模式下"后一名看见前一名占座"
    在这里退化成"同批并发的学生共享一个轮初快照"，这是为把长跑压进可接受时长做的取舍。
    """
    if not items:
        return {}
    lock = threading.Lock()

    def plan(entry):
        sid, need, skill_text, review = entry
        with lock:
            snapshot = dict(remaining)
        raw = client.chat_json(system=_PRESET_SYSTEM,
                               user=_preset_user(world, need, skill_text, snapshot, review))
        with lock:
            return sid, _validated(world, list(raw.get("sections") or []), remaining, llm_stats)

    if workers <= 1 or len(items) == 1:
        return dict(plan(entry) for entry in items)
    out: dict[str, list[str]] = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(plan, entry) for entry in items]
        for future in as_completed(futures):
            sid, chosen = future.result()
            out[sid] = chosen
    return out


def _plan_grouped(world: World, client: LLMClient, llm_stats: dict,
                  items: list[tuple[str, NeedModel, str, tuple]],
                  remaining: dict[str, int], workers: int, group_size: int) -> dict[str, list[str]]:
    """把学生按 group_size 打进同一次调用。返回缺人直接失败，不拿空课表冒充模型的决定。"""
    if group_size <= 1:
        return _plan_batch(world, client, llm_stats, items, remaining, workers)
    groups = _chunks(items, group_size)
    lock = threading.Lock()

    def plan_group(group):
        with lock:
            snapshot = dict(remaining)
        blocks = [f"===== 学生 {sid} =====\n"
                  + _preset_user(world, need, skill_text, snapshot, review)
                  for sid, need, skill_text, review in group]
        raw = client.chat_json(system=_PRESET_BATCH_SYSTEM,
                               user="\n".join(blocks))
        plans = raw.get("plans")
        if not isinstance(plans, list):
            raise LLMError("批量预置输出缺少 plans")
        by_id: dict[str, list] = {}
        for item in plans:
            if not isinstance(item, dict) or item.get("student_id") is None:
                raise LLMError(f"批量预置有一条没有学号：{item!r}")
            by_id[str(item["student_id"])] = list(item.get("sections") or [])
        missing = [sid for sid, _, _, _ in group if sid not in by_id]
        if missing:
            raise LLMError(f"批量预置漏了学生：{missing}")
        chosen: dict[str, list[str]] = {}
        with lock:
            for sid, _, _, _ in group:
                chosen[sid] = _validated(world, by_id[sid], remaining, llm_stats)
        return chosen

    if workers <= 1 or len(groups) == 1:
        merged: dict[str, list[str]] = {}
        for group in groups:
            merged.update(plan_group(group))
        return merged
    merged = {}
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [pool.submit(plan_group, group) for group in groups]
        for future in as_completed(futures):
            merged.update(future.result())
    return merged


def _run_planned(world: World, client: LLMClient, llm_stats: dict,
                 items: list[tuple[str, NeedModel, str, tuple]],
                 remaining: dict[str, int], workers: int, *,
                 on_progress=None, on_open_batch=None,
                 flush_every: int = 0, group_size: int = 1) -> dict[str, list[str]]:
    """按批调用模型。items 里只放还没付过钱的学生，余座由调用方事先扣好。

    每一批开始前 on_open_batch(学号) 记下在途的人；这一批全部返回后 on_progress(已完成)。
    进程死在批内时，检查点停在上一批，在途的就是 open batch 里的学号。
    """
    done: dict[str, list[str]] = {}
    for chunk in _chunks(items, flush_every):
        if on_open_batch is not None:
            on_open_batch([entry[0] for entry in chunk])
        done.update(_plan_grouped(world, client, llm_stats, chunk, remaining,
                                  workers, group_size))
        if on_progress is not None:
            on_progress(dict(done))
    return done


def ai_presets(world: World, needs: dict[str, NeedModel], skill_texts: dict[str, str],
               client: LLMClient, llm_stats: dict, workers: int = 1, *,
               resume: dict[str, list[str]] | None = None,
               on_progress=None, on_open_batch=None,
               flush_every: int = 0, group_size: int = 1) -> dict[str, list[str]]:
    """AI 排课：按学号顺序逐个学生生成预置课表，前一名学生占掉的余座对后一名可见。

    workers>1 时同批并发的学生共享该批开始时的余座快照。
    resume 中的学生直接占座、不再调用模型。
    """
    remaining = {s.section_id: s.capacity for s in world.sections.values()}
    kept = {sid: list(sections) for sid, sections in (resume or {}).items() if sid in needs}
    for sid in sorted(kept):
        _occupy(remaining, kept[sid], sid)
    items = [(sid, needs[sid], skill_texts[sid], ()) for sid in sorted(needs) if sid not in kept]
    planned = _run_planned(world, client, llm_stats, items, remaining, workers,
                           on_progress=_progress_with_kept(kept, on_progress),
                           on_open_batch=on_open_batch,
                           flush_every=flush_every, group_size=group_size)
    kept.update(planned)
    if len(kept) != len(needs):
        missing = sorted(set(needs) - set(kept))
        raise LLMError(f"预置课表缺少学生：{missing}")
    return {sid: kept[sid] for sid in needs}


def _progress_with_kept(kept: dict[str, list[str]], on_progress):
    if on_progress is None:
        return None

    def report(done: dict[str, list[str]]) -> None:
        merged = dict(kept)
        merged.update(done)
        on_progress(merged, None)

    return report


def _review_of(world: World, sid: str, preset_map: dict[str, list[str]],
               scores: dict[str, object], reasons: dict[str, str],
               final_assignments: dict[str, set[str]]) -> tuple:
    return (
        "【上一版预置课表】", *_assigned_lines(world, set(preset_map.get(sid, ()))),
        "【两轮选课后的最终课表】", *_assigned_lines(world, final_assignments.get(sid, set())),
        f"【上一轮满意度】{scores.get(sid)}——{reasons.get(sid, '')}",
    )


def ai_retune(world: World, needs: dict[str, NeedModel], skill_texts: dict[str, str],
              client: LLMClient, llm_stats: dict, preset_map: dict[str, list[str]],
              scores: dict[str, object], reasons: dict[str, str],
              final_assignments: dict[str, set[str]], frac: float = 0.3,
              workers: int = 1, *,
              resume: dict[str, list[str]] | None = None,
              on_progress=None, on_open_batch=None,
              flush_every: int = 0, group_size: int = 1) -> dict[str, list[str]]:
    """AI 复盘改写：满意度最低的一批学生连同上一轮结果交回模型重排，其余学生原样保留。

    resume 只含已经重排完的那部分学生。未重排的差生会释放旧座位后重新调用模型。
    """
    refined = {sid: list(sections) for sid, sections in preset_map.items()}
    if not scores:
        return refined
    ranked = sorted(scores, key=lambda sid: (scores[sid], sid))
    worst = set(ranked[:max(1, round(len(ranked) * frac))])
    remaining = {s.section_id: s.capacity for s in world.sections.values()}
    for sid, sections in refined.items():
        if sid in worst:
            continue
        _occupy(remaining, sections, sid)
    already = {sid: list(sections) for sid, sections in (resume or {}).items() if sid in worst}
    for sid in sorted(already):
        refined[sid] = list(already[sid])
        _occupy(remaining, already[sid], sid)
    targets = [sid for sid in sorted(worst & set(refined)) if sid not in already]
    items = [(sid, needs[sid], skill_texts[sid],
              _review_of(world, sid, preset_map, scores, reasons, final_assignments))
             for sid in targets]

    def report(done: dict[str, list[str]]) -> None:
        merged = dict(refined)
        merged.update(done)
        on_progress(merged, sorted(set(already) | set(done)))

    planned = _run_planned(world, client, llm_stats, items, remaining, workers,
                           on_progress=report if on_progress is not None else None,
                           on_open_batch=on_open_batch,
                           flush_every=flush_every, group_size=group_size)
    refined.update(planned)
    return refined
