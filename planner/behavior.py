"""选课行为的三处 LLM 决策：学生志愿与退课、每轮中签名单、满意度评分。代码只做事实校验（教学班存在、余座、志愿上限、时间冲突）。

本模块不再有任何规则通路：LLM 调用失败或输出不合规一律上抛 LLMError，由调用方如实报错。
"""
from __future__ import annotations

import sys
import time
from collections import Counter

from .llm import LLMClient, LLMError, bump_stats, count_drop, parallel_map
from .models import NeedModel, World, category_key
from .policy import MAX_PREFS_PER_CATEGORY, MAX_PREFS_PER_COURSE, PRIORITY_TIERS, tier_of
from .selection import RoundPlanEntry, SelectionState, _apply_grants
from .timegrid import has_conflict, slot_label

_CATALOG_LINE = "{sid}|{course}|{cat}|容量{cap}|剩余{left}|{times}"
# 真实开课目录有 4000 多个教学班，整表塞进每次 LLM 调用既放不下也不必要。
# 只列该生尚未满足的类别，并按类别轮流取行——若只按目录顺序截断，
# 容量最大的专业课会吃满整个配额，学生看不到任何其他类别的候选。
CATALOG_LINE_CAP = 80


def _catalog_lines(world: World, view: dict, need: NeedModel | None = None,
                   per_category: Counter | None = None, cap: int = CATALOG_LINE_CAP) -> list[str]:
    unmet = None
    if need is not None:
        targets = need.targets()
        have = per_category or Counter()
        unmet = {key for key, want in targets.items() if have.get(key, 0) < want}
    buckets: dict[str, list[str]] = {}
    for section_id in world.section_order:
        if view["remaining"][section_id] <= 0:
            continue
        section = world.sections[section_id]
        course = world.course_of_section(section_id)
        key = category_key(course.category)
        if unmet is not None and key not in unmet:
            continue
        times = "、".join(slot_label(s) for s in section.slots)
        buckets.setdefault(key, []).append(_CATALOG_LINE.format(
            sid=section_id, course=course.name, cat=course.category,
            cap=section.capacity, left=view["remaining"][section_id], times=times))
    keys = list(buckets)
    alloc = {key: 0 for key in keys}
    left = cap
    while left > 0:
        alive = [key for key in keys if alloc[key] < len(buckets[key])]
        if not alive:
            break
        for key in alive:
            if left == 0:
                break
            alloc[key] += 1
            left -= 1
    lines: list[str] = []
    for key in keys:
        lines.extend(buckets[key][:alloc[key]])
    hidden = {key: len(buckets[key]) - alloc[key] for key in keys
              if len(buckets[key]) > alloc[key]}
    if hidden:
        lines.append("（因长度限制未列出：" + "、".join(
            f"{key} {count} 个教学班" for key, count in hidden.items()) + "）")
    if not lines:
        lines = ["（无候选教学班）"]
    return lines


def _assigned_lines(world: World, sections: set[str]) -> list[str]:
    lines = []
    for section_id in sorted(sections):
        section = world.sections[section_id]
        course = world.course_of_section(section_id)
        times = "、".join(slot_label(s) for s in section.slots)
        lines.append(f"{section_id}|{course.name}|{course.category}|{times}")
    return lines or ["（无）"]


def _unmet_lines(need: NeedModel, per_category: Counter) -> list[str]:
    lines = []
    for key, want in need.targets().items():
        have = per_category.get(key, 0)
        mark = "（必修）" if key in need.required else ""
        lines.append(f"{key}{mark}：目标 {want}，已有 {have}")
    return lines or ["（无目标）"]


def ai_plan_builder(world: World, skill_texts: dict[str, str], needs: dict[str, NeedModel],
                    client: LLMClient, llm_stats: dict, workers: int = 1,
                    resume_for=None, on_batch=None, batch: int = 0):
    """resume_for(round_index, view) -> 已付过配额的学生结果；on_batch(round_index, view, 累计)。

    两者只为长跑检查点服务：模型决策逻辑、提示词、并发度都没有变化，只是把一次性
    barrier 改成每 batch 个学生一段，段末落盘，崩溃后已填报的学生不再问一次。
    batch<=0 时保持原来的单次 barrier（演示路径与所有既有调用方走这条）。
    """
    def build(round_index: int, view: dict) -> dict[str, RoundPlanEntry]:
        # 同一轮内所有学生看到的都是轮初快照，因此并发填报不会互相改写状态；
        # 退课与志愿要等整轮收齐后才落到状态上（run_two_rounds 负责）。
        ids = list(needs)
        done: dict[str, RoundPlanEntry] = {}
        if resume_for is not None:
            done.update(resume_for(round_index, view))
        todo = [sid for sid in ids if sid not in done]
        step = batch if batch > 0 else max(len(todo), 1)
        for start in range(0, len(todo), step):
            part = todo[start:start + step]
            done.update(zip(part, parallel_map(
                part,
                lambda sid: _ai_round(world, skill_texts[sid], needs[sid], view,
                                      round_index, client, llm_stats),
                workers)))
            if on_batch is not None:
                on_batch(round_index, view, done)
        return {sid: done[sid] for sid in ids}
    return build


_AI_SYSTEM = (
    "你扮演一名中国大学生，模拟真实两轮选课行为。规则："
    "1) 同一门课程最多 3 个志愿（不同教学班），序号从 1 连续编号；"
    "2) 每个类别最多 3 个志愿，其中通识核心课、通识选修课、体育课合并视为同一类别；"
    "3) 志愿时间不得与已选课程冲突；"
    "4) 只能退已选上的教学班；"
    "5) 优先保证必修类别，再按备注偏好选择；"
    "6) 只能从给出的候选教学班中填报新志愿，候选清单可能因长度限制不完整。"
    "只输出 JSON：{\"drops\": [\"教学班ID\"], \"preferences\": [{\"section_id\": \"ID\", \"rank\": 1}]}"
)


def _ai_round(world: World, skill_text: str, need: NeedModel, view: dict,
              round_index: int, client: LLMClient, llm_stats: dict) -> RoundPlanEntry:
    assigned = set(view["assignments"][need.student_id])
    per_category = Counter(category_key(world.course_of_section(s).category) for s in assigned)
    user = "\n".join([
        f"【第 {round_index} 轮】",
        "【你的 .skill 档案】", skill_text,
        "【当前已选课程】", *_assigned_lines(world, assigned),
        "【目标完成情况】", *_unmet_lines(need, per_category),
        "【候选教学班】", *_catalog_lines(world, view, need, per_category),
        "请决定本轮退课与志愿。",
    ])
    raw = client.chat_json(system=_AI_SYSTEM, user=user)
    drops = [str(d) for d in (raw.get("drops") or []) if str(d) in assigned]
    raw_entries = list(raw.get("preferences") or [])

    # 退课生效后重新计算持有课程与已用时段。
    after_drop = assigned - set(drops)
    held_after = {world.sections[s].course_id for s in after_drop}
    used_after: set = set()
    for section_id in after_drop:
        used_after.update(world.sections[section_id].slots)

    prefs: dict[str, int] = {}
    per_course: dict[str, int] = {}
    per_cat: Counter = Counter(category_key(world.course_of_section(s).category) for s in after_drop)
    ordered = sorted(raw_entries, key=lambda e: int(e.get("rank", 99)))
    for entry in ordered:
        section_id = str(entry.get("section_id", ""))
        if section_id not in world.sections or section_id in prefs or section_id in after_drop:
            count_drop(llm_stats, "plan_invalid_section")
            continue
        course = world.course_of_section(section_id)
        if course.course_id in held_after:
            count_drop(llm_stats, "plan_already_held")
            continue
        if per_course.get(course.course_id, 0) >= MAX_PREFS_PER_COURSE:
            count_drop(llm_stats, "plan_course_cap")
            continue
        key = category_key(course.category)
        if per_cat.get(key, 0) >= MAX_PREFS_PER_CATEGORY:
            count_drop(llm_stats, "plan_category_cap")
            continue
        if has_conflict(world.sections[section_id].slots, used_after):
            count_drop(llm_stats, "plan_time_conflict")
            continue
        prefs[section_id] = 0
        used_after.update(world.sections[section_id].slots)
        per_course[course.course_id] = per_course.get(course.course_id, 0) + 1
        per_cat[key] += 1
    by_course: dict[str, list[str]] = {}
    for section_id in prefs:
        by_course.setdefault(world.sections[section_id].course_id, []).append(section_id)
    result: dict[str, int] = {}
    for sections in by_course.values():
        for rank, section_id in enumerate(sections, 1):
            result[section_id] = rank
    return RoundPlanEntry(drops=tuple(drops), prefs=result)


_ALLOC_SYSTEM = (
    "你是教务系统的选课筛选器，负责一轮选课结束后的中签名单。规则："
    "1) 只能把教学班名额授予在该班「志愿N」名单里出现过的学生；"
    "2) 一个教学班授予的人数不能超过它的剩余名额；"
    "3) 一名学生同一轮只能进同一门课程的一个教学班；"
    "4) 志愿序号越小越优先，先满足志愿1，再看志愿2、3；"
    "5) 优先级分组从高到低为：毕业年级 > 转专业 > 即将换校区 > 本专业学生选专业课 > 其他；"
    "6) 同组内名额不足时由你判断给谁，名额不足时宁可留空也不要超授。"
    "只输出 JSON：{\"grants\": [{\"student_id\": \"学号\", \"section_id\": \"教学班ID\"}]}"
)

# 一轮志愿池可能有几百个教学班，逐个列出会顶爆上下文；按目录顺序截断并如实告知缺失部分。
ALLOC_LINE_CAP = 300


def _section_line(world: World, section_id: str, applicants: list[tuple[int, str]],
                  view: dict) -> str:
    course = world.course_of_section(section_id)
    parts = []
    for rank in sorted({r for r, _ in applicants}):
        names = "、".join(f"{sid}({PRIORITY_TIERS[tier_of(world.students[sid], course)]})"
                          for r, sid in sorted(applicants) if r == rank)
        parts.append(f"志愿{rank}：{names}")
    return (f"{section_id}|{course.name}|{course.category}|"
            f"剩余{view['remaining'][section_id]}|" + "；".join(parts))


def _pool_chunks(world: World, prefs: dict[str, dict[str, int]], view: dict,
                 cap: int = ALLOC_LINE_CAP) -> list[list[str]]:
    """把本轮志愿池按课程打包成若干不超过 cap 行的分块。

    同一门课的各教学班尽量落在同一块里（学生常在同一门课的班之间比较），
    分块之间不截断任何志愿——以前 300 行封顶会把几百个教学班的志愿整段抹掉，
    模型看不到那些志愿就不可能授名额，等于代码替它做了决定。
    """
    by_section: dict[str, list[tuple[int, str]]] = {}
    for student_id, student_prefs in prefs.items():
        for section_id, rank in student_prefs.items():
            by_section.setdefault(section_id, []).append((rank, student_id))
    groups: list[list[str]] = []
    previous_course = None
    for section_id in world.section_order:
        applicants = by_section.get(section_id)
        if not applicants:
            continue
        course_id = world.sections[section_id].course_id
        line = _section_line(world, section_id, applicants, view)
        if groups and course_id == previous_course:
            groups[-1].append(line)
        else:
            groups.append([line])
            previous_course = course_id
    chunks: list[list[str]] = []
    buffer: list[str] = []
    for lines in groups:
        if buffer and len(buffer) + len(lines) > cap:
            chunks.append(buffer)
            buffer = []
        buffer.extend(lines)
    if buffer:
        chunks.append(buffer)
    return chunks


def ai_allocator(world: World, client: LLMClient, llm_stats: dict,
                 line_cap: int = ALLOC_LINE_CAP):
    def allocate(round_index: int, prefs: dict[str, dict[str, int]],
                 view: dict) -> list[tuple[str, str]]:
        chunks = _pool_chunks(world, prefs, view, line_cap)
        if not chunks:
            return []
        bump_stats(llm_stats, "alloc_chunks", len(chunks))
        # 分块必须顺序跑：前一块发出的名额要从后一块的余座里扣掉，否则同一批座位会被授两次。
        # 这块台账只记账不做决定——规则与外层 _apply_grants 完全同一个函数。
        ledger = SelectionState.from_view(world, view)
        grants: list[tuple[str, str]] = []
        for index, lines in enumerate(chunks, 1):
            started = time.monotonic()
            user = "\n".join([
                f"【第 {round_index} 轮筛选 · 第 {index}/{len(chunks)} 块】",
                f"本轮 {len(prefs)} 名学生提交志愿；本块只涉及下列教学班。",
                "【志愿池】", *lines,
                "请给出本块的中签名单。",
            ])
            raw = client.chat_json(system=_ALLOC_SYSTEM, user=user)
            print(f"[alloc] r{round_index} 块 {index}/{len(chunks)} 行数 {len(lines)} "
                  f"用时 {time.monotonic() - started:.0f}s 累计调用 {client.calls}",
                  file=sys.stderr, flush=True)
            proposed = []
            for item in raw.get("grants") or []:
                if not isinstance(item, dict):
                    count_drop(llm_stats, "grant_malformed")
                    continue
                proposed.append((str(item.get("student_id", "")),
                                 str(item.get("section_id", ""))))
            events: list[dict] = []
            _apply_grants(world, ledger, prefs, proposed, round_index, events, llm_stats)
            grants.extend((e["student_id"], e["section_id"])
                          for e in events if e["phase"] == "中签")
        return grants
    return allocate


def ai_satisfaction(world: World, skill_text: str, need: NeedModel,
                    assigned_sections: set[str],
                    client: LLMClient, llm_stats: dict) -> tuple[int, str]:
    assigned = set(assigned_sections)
    per_category = Counter(category_key(world.course_of_section(s).category) for s in assigned)
    user = "\n".join([
        "【你的 .skill 档案】", skill_text,
        "【最终课表】", *_assigned_lines(world, assigned),
        "【目标完成情况】", *_unmet_lines(need, per_category),
        "请给这名学生的选课满意度打分（0-100 整数）并用一句话说明原因。",
    ])
    raw = client.chat_json(
        system="你是选课满意度评估器。" + '只输出 JSON：{"satisfaction": 0-100整数, "reason": "一句话"}',
        user=user)
    try:
        score = int(raw.get("satisfaction"))
    except (TypeError, ValueError) as exc:
        raise LLMError(f"LLM 满意度输出不合规：{raw!r}") from exc
    return max(0, min(100, score)), str(raw.get("reason") or "")
