"""学生 .skill 记录的合成与解析。

学生 .skill 由本项目按培养方案目标合成（教务不提供学生偏好）；课程与教学班来自真实开课目录。
"""
from __future__ import annotations

import random

from .llm import LLMClient, count_drop
from .models import CATEGORIES, MERGED_KEY, NeedModel, Student, World, category_key
from .timegrid import all_slots, parse_slot, slot_key

_NOTE_POOL = (
    "希望专业课尽量排在下午。",
    "不喜欢晚上有课。",
    "不想上早八。",
    "希望周三下午留空处理社团事务。",
    "体育课想选时间段靠后的班。",
    "希望外语课和数学课隔天排。",
    "没有特别偏好，服从安排。",
)


def _target_line(targets: dict[str, int]) -> str:
    return "；".join(f"{key} {count}" for key, count in targets.items())


_YEAR_LABEL = {"一": "大一", "二": "大二", "三": "大三", "四": "大四", "五": "大五"}


def format_skill(student: Student, targets: dict[str, int],
                 avoid_keys: list[str], note: str) -> str:
    lines = [
        f"@SKILL {student.student_id}",
        f"专业：{student.major}",
        f"学年：{_YEAR_LABEL.get(student.year, student.year)}",
        "年级：毕业年级" if student.graduating else "年级：非毕业年级",
        "已修课程：" + ("、".join(student.completed) if student.completed else "无"),
        f"本学期修读目标：{_target_line(targets)}",
        "尽量避开：" + ("、".join(avoid_keys) if avoid_keys else "无"),
        f"备注：{note}",
    ]
    return "\n".join(lines)


def _year_targets(world: World, major: str, year: str) -> dict[str, int] | None:
    """该 (专业, 学年) 本学期各类别的必修门数；方案里没有这一格时返回 None。"""
    if world.recommended_by_year:
        base = (world.recommended_by_year.get(major) or {}).get(year)
    else:
        base = world.recommended.get(major) or {}
    if base is None:
        return None
    return {k: v for k, v in base.items() if k in world.category_keys}


def synthesize_students(n: int, world: World, rng: random.Random,
                        allocation: list[tuple[str, str]] | None = None
                        ) -> tuple[dict[str, Student], dict[str, str]]:
    """合成学生与 .skill 记录。

    allocation 给出每名学生的 (专业, 学年)（实验用：按招生计划人数加权分配）；
    不给时退回"在 world.majors 上均匀抽、一律大一"的老行为。
    world.need_rules 存在时按培养方案原文补 standing rule（体育每生 1 门、外语按学期比例、
    排除大四就不造毕业年级标记），不存在就完全保持老口径。
    """
    catalog_keys = world.category_keys
    if not catalog_keys:
        raise ValueError("课程目录没有任何分类，无法合成学生目标")
    ids = [f"S{i + 1:03d}" for i in range(n)]
    if allocation is None:
        majors = [m for m in (world.majors or ["通用"]) if _year_targets(world, m, "一")]
        if not majors:
            raise ValueError("培养方案里没有任何大一学期目标，无法合成学生")
        cells = [(rng.choice(majors), "一") for _ in ids]
    else:
        if len(allocation) != n:
            raise ValueError(f"人口分配给了 {len(allocation)} 个格子，但学生数是 {n}")
        cells = list(allocation)
    rules = world.need_rules or {}
    order = list(ids)
    rng.shuffle(order)
    n_grad = 0 if rules.get("exclude_grades") else (
        max(1, round(n * 0.1)) if n >= 3 else (1 if n >= 1 else 0))
    n_transfer = max(1, round(n * 0.06)) if n >= 6 else 0
    n_move = max(1, round(n * 0.06)) if n >= 6 else 0
    graduating_ids = set(order[:n_grad])
    transfer_ids = set(order[n_grad:n_grad + n_transfer])
    move_ids = set(order[n_grad + n_transfer:n_grad + n_transfer + n_move])
    share = float(rules.get("language_share_per_student") or 0.0)
    language_order = list(ids)
    rng.shuffle(language_order)
    language_ids = set(language_order[:round(n * share)]) if share > 0 else set()
    pe_per_student = int(rules.get("pe_courses_per_student") or 0)

    students: dict[str, Student] = {}
    skill_texts: dict[str, str] = {}
    slot_pool = all_slots()
    courses_per_key: dict[str, int] = {}
    for course in world.courses.values():
        key = category_key(course.category)
        courses_per_key[key] = courses_per_key.get(key, 0) + 1
    for student_id, (major, year) in zip(ids, cells):
        if year not in ("一", "二", "三"):
            raise ValueError(f"学生 {student_id} 的学年 {year} 不在 一/二/三 之内")
        base = _year_targets(world, major, year)
        if base is None:
            raise ValueError(f"培养方案里没有 {major} 的 {year} 学年学期目标，无法分配该格")
        required = dict(base)
        if pe_per_student and MERGED_KEY in catalog_keys:
            required[MERGED_KEY] = required.get(MERGED_KEY, 0) + pe_per_student
        if student_id in language_ids and "外语类" in catalog_keys:
            required["外语类"] = required.get("外语类", 0) + 1
        student = Student(
            student_id=student_id, major=major, year=year,
            graduating=student_id in graduating_ids,
            transfer=student_id in transfer_ids,
            campus_move=student_id in move_ids,
            completed=tuple(f"已修课程{i}" for i in range(1, rng.randint(2, 6) + 1)),
        )
        elective_pool = [k for k in catalog_keys
                         if k not in required and courses_per_key.get(k, 0) >= 1]
        rng.shuffle(elective_pool)
        elective = {}
        for key in elective_pool[:rng.randint(1, 2)]:
            elective[key] = rng.randint(1, min(2, courses_per_key[key]))
        targets = dict(required)
        for key, value in elective.items():
            targets[key] = targets.get(key, 0) + value
        avoid_keys = [slot_key(s) for s in rng.sample(slot_pool, rng.randint(0, 2))]
        note = rng.choice(_NOTE_POOL)
        students[student_id] = student
        skill_texts[student_id] = format_skill(student, targets, avoid_keys, note)
    return students, skill_texts


def _normalize_category(raw: str) -> str:
    raw = raw.strip()
    if raw in CATEGORIES:
        return category_key(raw)
    if raw == MERGED_KEY:
        return MERGED_KEY
    raise ValueError(f"未知课程分类：{raw}")


def skill_student_id(text: str) -> str:
    """从 .skill 记录的 @SKILL 头取出学号（只做字段读取，不含任何推定规则）。"""
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("@SKILL"):
            student_id = line[len("@SKILL"):].strip()
            if student_id:
                return student_id
    raise ValueError(".skill 记录缺少 @SKILL 头")


_NEED_SCHEMA = (
    '只输出 JSON：{"required": {"分类": 门数}, "elective": {"分类": 门数}, '
    '"avoid_slots": ["Mon-1"], "notes": "一句话"}。'
    '分类只能使用：思政类、外语类、计算机通识类、自然科学通识类、专业课、专业基础课、'
    '通识核心课、通识选修课、体育课（其中通识核心课/通识选修课/体育课的志愿上限合并计算，'
    '如目标跨这三类，请把门数放进 "通识与体育类" 这一个键）。'
    'avoid_slots 的星期只能是 Mon/Tue/Wed/Thu/Fri，节次只能是 1-13（上午1-5、下午6-10、晚上11-13），'
    '不存在第 14 节。'
)


def parse_skill_llm(text: str, client: LLMClient, stats: dict | None = None) -> NeedModel:
    """把 .skill 交给模型解析；无法落到真实分类/节次上的条目剔掉计数，不替模型做决定。

    这里只校验事实（分类名、节次词汇表）：长跑里一次越界输出不能让整轮作废，
    但被剔除的条数会进 llm_stats["sanitized"]，报告里能看到模型有多少次不合规。
    """
    raw = client.chat_json(
        system="你是教务系统的需求解析器。把学生的 .skill 记录解析为结构化需求。" + _NEED_SCHEMA,
        user=text)
    required = _counts(raw.get("required"), stats)
    elective = _counts(raw.get("elective"), stats)
    avoid = []
    for item in (raw.get("avoid_slots") or []):
        try:
            avoid.append(parse_slot(item))
        except (AttributeError, TypeError, ValueError):
            if stats is not None:
                count_drop(stats, "need_slot")
    return NeedModel(student_id=skill_student_id(text), required=required, elective=elective,
                     avoid_slots=tuple(avoid), notes=str(raw.get("notes") or ""), source="llm")


def _counts(raw, stats: dict | None) -> dict[str, int]:
    out: dict[str, int] = {}
    for key, value in (raw or {}).items():
        try:
            name = _normalize_category(key)
            want = int(value)
        except (AttributeError, TypeError, ValueError):
            if stats is not None:
                count_drop(stats, "need_category")
            continue
        if want <= 0:
            continue
        out[name] = out.get(name, 0) + want
    return out
