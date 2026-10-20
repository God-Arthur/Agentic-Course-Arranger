"""测试夹具： tiny（小容量，测筛选细节）/ demo（大容量，测端到端）。测试内合成课程仅用于验证代码正确性，不进入演示数据。"""
from __future__ import annotations

import json


def tiny_world() -> dict:
    return {
        "courses": [
            {"course_id": "A", "name": "思政A", "category": "思政类"},
            {"course_id": "B", "name": "外语B", "category": "外语类"},
            {"course_id": "C", "name": "专业C", "category": "专业课", "major": "计算机科学与技术"},
            {"course_id": "PE", "name": "体育", "category": "体育课"},
            {"course_id": "GE", "name": "通识选修", "category": "通识选修课"},
        ],
        "sections": [
            {"section_id": "A1", "course_id": "A", "capacity": 1, "slots": ["Mon-1"]},
            {"section_id": "B1", "course_id": "B", "capacity": 1, "slots": ["Mon-1"]},
            {"section_id": "B2", "course_id": "B", "capacity": 1, "slots": ["Tue-2"]},
            {"section_id": "C1", "course_id": "C", "capacity": 1, "slots": ["Wed-1"]},
            {"section_id": "PE1", "course_id": "PE", "capacity": 1, "slots": ["Thu-11"]},
            {"section_id": "PE2", "course_id": "PE", "capacity": 1, "slots": ["Thu-12"]},
            {"section_id": "GE1", "course_id": "GE", "capacity": 1, "slots": ["Thu-13"]},
            {"section_id": "GE2", "course_id": "GE", "capacity": 1, "slots": ["Fri-11"]},
        ],
        "plan": {},
    }


def demo_world(scale: int = 10) -> dict:
    return {
        "courses": [
            {"course_id": "P1", "name": "思想道德与法治", "category": "思政类"},
            {"course_id": "E1", "name": "大学英语(一)", "category": "外语类"},
            {"course_id": "M1", "name": "高等数学", "category": "自然科学通识类"},
            {"course_id": "K1", "name": "程序设计", "category": "计算机通识类"},
            {"course_id": "S1", "name": "数据结构", "category": "专业课", "major": "计算机科学与技术"},
            {"course_id": "F1", "name": "操作系统", "category": "专业基础课", "major": "计算机科学与技术"},
            {"course_id": "PE1", "name": "大学体育(一)", "category": "体育课"},
            {"course_id": "GE1", "name": "影视鉴赏", "category": "通识选修课"},
            {"course_id": "GC1", "name": "人文经典导读", "category": "通识核心课"},
        ],
        "sections": [
            {"section_id": "P1-1", "course_id": "P1", "capacity": scale, "slots": ["Mon-1", "Mon-2"]},
            {"section_id": "E1-1", "course_id": "E1", "capacity": scale, "slots": ["Tue-3", "Tue-4"]},
            {"section_id": "M1-1", "course_id": "M1", "capacity": scale, "slots": ["Mon-3", "Mon-4"]},
            {"section_id": "K1-1", "course_id": "K1", "capacity": scale, "slots": ["Tue-6", "Tue-7"]},
            {"section_id": "S1-1", "course_id": "S1", "capacity": scale, "slots": ["Wed-1", "Wed-2"]},
            {"section_id": "F1-1", "course_id": "F1", "capacity": scale, "slots": ["Thu-1", "Thu-2"]},
            {"section_id": "PE1-1", "course_id": "PE1", "capacity": scale, "slots": ["Thu-11"]},
            {"section_id": "GE1-1", "course_id": "GE1", "capacity": scale, "slots": ["Fri-6"]},
            {"section_id": "GC1-1", "course_id": "GC1", "capacity": scale, "slots": ["Fri-11"]},
        ],
        "plan": {
            "majors": ["计算机科学与技术"],
            "recommended": {"计算机科学与技术": {"思政类": 1, "外语类": 1, "专业课": 1, "专业基础课": 1}},
        },
    }


def write_courses(tmp_path, data: dict) -> str:
    path = tmp_path / "courses.json"
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return str(path)
