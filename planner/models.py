"""核心数据模型与课程数据装载。data/courses.json 现为教务抓取的真实开课目录（容量口径见文件内 _meta）。"""
from __future__ import annotations

from dataclasses import dataclass, field

from .timegrid import Slot, parse_slot

CATEGORIES = ("思政类", "外语类", "计算机通识类", "自然科学通识类",
              "专业课", "专业基础课", "通识核心课", "通识选修课", "体育课")
MERGED_CATEGORIES = ("通识核心课", "通识选修课", "体育课")
MERGED_KEY = "通识与体育类"


def category_key(category: str) -> str:
    """通识核心课/通识选修课/体育课在志愿数上限上视为同一类课程。"""
    return MERGED_KEY if category in MERGED_CATEGORIES else category


@dataclass(frozen=True)
class Course:
    course_id: str
    name: str
    category: str
    major: str | None = None


@dataclass(frozen=True)
class Section:
    section_id: str
    course_id: str
    capacity: int
    slots: tuple[Slot, ...]


@dataclass
class Student:
    student_id: str
    major: str
    graduating: bool = False
    transfer: bool = False
    campus_move: bool = False
    completed: tuple[str, ...] = ()
    year: str = "一"          # 学年序号（一/二/三）；大四按发起人 2026-10-20 的口径排除在本学期之外


@dataclass
class NeedModel:
    student_id: str
    required: dict[str, int] = field(default_factory=dict)
    elective: dict[str, int] = field(default_factory=dict)
    avoid_slots: tuple[Slot, ...] = ()
    notes: str = ""
    source: str = "llm"

    def targets(self) -> dict[str, int]:
        merged = dict(self.required)
        for key, value in self.elective.items():
            merged[key] = merged.get(key, 0) + value
        return merged


@dataclass
class World:
    courses: dict[str, Course]
    sections: dict[str, Section]
    students: dict[str, Student]
    course_order: list[str]
    section_order: list[str]
    majors: list[str]
    recommended: dict[str, dict[str, int]]
    category_keys: list[str]
    sections_by_course: dict[str, list[str]] = field(default_factory=dict)
    courses_by_key: dict[str, list[Course]] = field(default_factory=dict)
    recommended_by_year: dict[str, dict[str, dict[str, int]]] = field(default_factory=dict)
    need_rules: dict = field(default_factory=dict)

    def sections_of(self, course_id: str) -> list[str]:
        return self.sections_by_course.get(course_id, [])

    def course_of_section(self, section_id: str) -> Course:
        return self.courses[self.sections[section_id].course_id]

    def courses_of_key(self, key: str) -> list[Course]:
        return self.courses_by_key.get(key, [])


def load_world(data: dict) -> World:
    courses: dict[str, Course] = {}
    course_order: list[str] = []
    for item in data.get("courses", []):
        course_id = str(item["course_id"])
        if course_id in courses:
            raise ValueError(f"课程编号重复：{course_id}")
        category = str(item["category"])
        if category not in CATEGORIES:
            raise ValueError(f"课程 {course_id} 分类非法：{category}（必须是 {CATEGORIES} 之一）")
        courses[course_id] = Course(course_id, str(item.get("name") or course_id),
                                    category, item.get("major") or None)
        course_order.append(course_id)

    sections: dict[str, Section] = {}
    section_order: list[str] = []
    for item in data.get("sections", []):
        section_id = str(item["section_id"])
        course_id = str(item["course_id"])
        if course_id not in courses:
            raise ValueError(f"教学班 {section_id} 引用了不存在的课程 {course_id}")
        if section_id in sections:
            raise ValueError(f"教学班编号重复：{section_id}")
        capacity = int(item["capacity"])
        if capacity <= 0:
            raise ValueError(f"教学班 {section_id} 容量必须为正整数")
        slots = tuple(parse_slot(s) for s in item.get("slots", []))
        if not slots:
            raise ValueError(f"教学班 {section_id} 没有安排时间")
        if len(set(slots)) != len(slots):
            raise ValueError(f"教学班 {section_id} 存在重复时间槽")
        sections[section_id] = Section(section_id, course_id, capacity, slots)
        section_order.append(section_id)

    if not courses:
        raise ValueError("课程数据为空")
    if not sections:
        raise ValueError("教学班数据为空")

    plan = data.get("plan", {}) or {}

    def normalize(major: str, mapping: dict) -> dict[str, int]:
        clean: dict[str, int] = {}
        for key, count in (mapping or {}).items():
            normalized = category_key(str(key))
            if normalized != str(key) and str(key) not in CATEGORIES and str(key) != MERGED_KEY:
                raise ValueError(f"培养方案 {major} 的分类非法：{key}")
            clean[normalized] = int(count)
        return clean

    recommended = {str(major): normalize(str(major), mapping)
                   for major, mapping in (plan.get("recommended", {}) or {}).items()}
    recommended_by_year: dict[str, dict[str, dict[str, int]]] = {}
    for major, per_year in (plan.get("recommended_by_year", {}) or {}).items():
        recommended_by_year[str(major)] = {
            str(index): normalize(f"{major} {index}", mapping)
            for index, mapping in (per_year or {}).items()}

    majors = sorted({c.major for c in courses.values() if c.major}
                    | set(plan.get("majors", []) or []))
    category_keys = sorted({category_key(c.category) for c in courses.values()})
    sections_by_course: dict[str, list[str]] = {}
    for section_id in section_order:
        sections_by_course.setdefault(sections[section_id].course_id, []).append(section_id)
    courses_by_key: dict[str, list[Course]] = {}
    for course_id in course_order:
        courses_by_key.setdefault(category_key(courses[course_id].category),
                                  []).append(courses[course_id])
    return World(courses, sections, {}, course_order, section_order, majors,
                 recommended, category_keys, sections_by_course, courses_by_key,
                 recommended_by_year, dict(plan.get("need_rules", {}) or {}))
