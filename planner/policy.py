"""选课制度规则与志愿校验。口径来自发起人 2026-10-18 描述，未与官方文件核实。"""
from __future__ import annotations

from .models import World, category_key

MAX_PREFS_PER_COURSE = 3
MAX_PREFS_PER_CATEGORY = 3
ROUND_COUNT = 2
PRIORITY_TIERS = ("毕业年级", "转专业", "即将换校区", "本专业学生选专业课", "其他")


def tier_of(student, course) -> int:
    """优先级分组（互斥，编号越小越优先）。"""
    if student.graduating:
        return 0
    if student.transfer:
        return 1
    if student.campus_move:
        return 2
    if course.category == "专业课" and course.major is not None and student.major == course.major:
        return 3
    return 4


def validate_preferences(world: World, prefs_by_student: dict[str, dict[str, int]],
                         held_sections: dict[str, set[str]] | None = None) -> None:
    """校验志愿：教学班存在、序号 1-3 且每门课内连续、每门课 ≤3、每个类别（合并后）≤3、不与已持有课程重复。"""
    held_sections = held_sections or {}
    for student_id, prefs in prefs_by_student.items():
        if student_id not in world.students:
            raise ValueError(f"未知学生：{student_id}")
        held = held_sections.get(student_id, set())
        held_courses = {world.sections[s].course_id for s in held if s in world.sections}
        per_course: dict[str, int] = {}
        per_category: dict[str, int] = {}
        ranks_by_course: dict[str, list[int]] = {}
        seen_sections: set[str] = set()
        for section_id, rank in prefs.items():
            if section_id not in world.sections:
                raise ValueError(f"{student_id} 志愿引用未知教学班：{section_id}")
            if section_id in seen_sections:
                raise ValueError(f"{student_id} 对同一教学班重复提交志愿：{section_id}")
            seen_sections.add(section_id)
            if rank not in (1, 2, 3):
                raise ValueError(f"{student_id} 志愿序号必须是 1-3：{section_id}={rank}")
            if section_id in held:
                raise ValueError(f"{student_id} 已持有教学班 {section_id}，不能重复提交志愿")
            course = world.course_of_section(section_id)
            if course.course_id in held_courses:
                raise ValueError(f"{student_id} 已持有课程 {course.course_id}，不能再提交该课程志愿")
            per_course[course.course_id] = per_course.get(course.course_id, 0) + 1
            if per_course[course.course_id] > MAX_PREFS_PER_COURSE:
                raise ValueError(f"{student_id} 对课程 {course.course_id} 志愿数超过 {MAX_PREFS_PER_COURSE}")
            key = category_key(course.category)
            per_category[key] = per_category.get(key, 0) + 1
            if per_category[key] > MAX_PREFS_PER_CATEGORY:
                raise ValueError(f"{student_id} 类别 {key} 志愿数超过 {MAX_PREFS_PER_CATEGORY}")
            ranks_by_course.setdefault(course.course_id, []).append(rank)
        for course_id, ranks in ranks_by_course.items():
            if sorted(ranks) != list(range(1, len(ranks) + 1)):
                raise ValueError(f"{student_id} 课程 {course_id} 的志愿序号必须从 1 连续编号：{sorted(ranks)}")
