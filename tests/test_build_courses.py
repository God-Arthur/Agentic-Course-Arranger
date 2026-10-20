"""转换器 tools/build_courses_from_zju.py 的测试（真实抓取数据只读，用例自带临时目录）。"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

from planner import models

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "build_courses_from_zju", ROOT / "tools" / "build_courses_from_zju.py")
bc = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bc)


def test_parse_slots_real_formats():
    assert bc.parse_slots("周四第3,4节") == ["Thu-3", "Thu-4"]
    assert bc.parse_slots("周二第6,7节<br>周五第1,2节") == ["Tue-6", "Tue-7", "Fri-1", "Fri-2"]
    # 周次段与单双周标记不改变“这一节每周都占位”的保守口径
    assert bc.parse_slots("周一第1,2节{秋1,4-8周；冬1-8周}(双周)") == ["Mon-1", "Mon-2"]
    # 解析层认得周六/周日，剔除发生在 main()（网格只有周一至周五）
    assert bc.parse_slots("周六第11,12,13节") == ["Sat-11", "Sat-12", "Sat-13"]
    assert bc.parse_slots("--") == []
    assert bc.parse_slots("不安排时间") == []


def test_category_from_plan_uses_official_module_path():
    assert bc.category_from_plan("通识课程 / 通识必修课程 / 外语类 / 必修课程", "大学英语Ⅲ", "SIS1001G") == "外语类"
    assert bc.category_from_plan("通识课程 / 通识必修课程 / 计算机类", "C程序设计基础及实验", "CS1001G") == "计算机通识类"
    assert bc.category_from_plan("个性修读课程 / 本专业进阶模块", "编译原理", "CS3001G") == "专业课"
    assert bc.category_from_plan("通识课程 / 通识必修课程 / 专业基础课程", "离散数学", "CS1302F") == "专业基础课"


def test_plan_junti_split_between_pe_and_ideology():
    pe = bc.category_from_plan("通识课程 / 通识必修课程 / 军体类 / 必修课程", "体育Ⅰ—基础体能", "PE1001G")
    military = bc.category_from_plan("通识课程 / 通识必修课程 / 军体类 / 必修课程", "军事理论", "EDU2001G")
    assert pe == "体育课"
    assert military == "思政类"
    # 481E 前缀是体育课号
    assert bc.category_from_plan("通识课程 / 通识必修课程 / 军体类", "定向越野", "481E0100") == "体育课"


def test_category_from_catalog_fallback_and_rollups():
    assert bc.category_from_catalog(["通识选修课程|全部课程", "通识选修课程|科技创新"], "航空航天导论", "AA0403G") == "通识选修课"
    assert bc.category_from_catalog(["通识选修课程|全部课程", "通识选修课程|通识核心课程"], "文明经典导论", "CC1001G") == "通识核心课"
    assert bc.category_from_catalog(["体育课程"], "游泳", "PPAE0002G") == "体育课"
    assert bc.category_from_catalog(["专业课程|所有类（专业）"], "编译器构造", "CS4001M") == "专业课"
    # 只剩汇总位时不能凭空归类
    assert bc.category_from_catalog(["通识必修课程|全部课程"], "未知课", "ZZ0001G") is None


def test_catalog_junti_split_uses_course_name():
    assert bc.category_from_catalog(["通识必修课程|思政军体类"], "中国改革开放史", "HIST2001G") == "思政类"
    assert bc.category_from_catalog(["通识必修课程|思政军体类"], "体育Ⅶ--体测与锻炼", "481E0090") == "体育课"


def _row(major, course_id, term, autumn, winter, path):
    return {"grade_year": "2026", "major": major, "课程号": course_id, "建议学年学期": term,
            "秋": autumn, "冬": winter, "module_path": path, "课程名称": course_id}


def test_plan_targets_counts_this_term_courses_per_category():
    rows = [
        _row("计算机科学与技术", "CS1001G", "一(冬)", "", "80", "通识课程 / 通识必修课程 / 计算机类"),
        _row("计算机科学与技术", "MARX1004G", "一(冬)", "", "32", "通识课程 / 通识必修课程 / 思政类"),
        _row("计算机科学与技术", "MARX1005G", "一(秋)", "16", "", "通识课程 / 通识必修课程 / 思政类"),
        _row("计算机科学与技术", "SIS1001G", "一(春夏)", "", "", "通识课程 / 通识必修课程 / 外语类"),
        _row("计算机科学与技术", "CS1001G", "一(冬)", "", "80", "通识课程 / 通识必修课程 / 计算机类"),
        _row("软件工程", "CS1001G", "一(冬)", "", "80", "通识课程 / 通识必修课程 / 计算机类"),
    ]
    catalog = {"CS1001G": "计算机通识类", "MARX1004G": "思政类", "MARX1005G": "思政类"}
    got = bc.plan_targets(rows, "2026", "一", catalog)
    # 春夏学期被排除；同一课程重复行只计一次；不在目录里的课号被忽略
    assert got["计算机科学与技术"] == {"计算机通识类": 1, "思政类": 2}
    assert got["软件工程"] == {"计算机通识类": 1}


def test_main_drops_weekend_sections(tmp_path, monkeypatch):
    """不考虑周末：含周六/周日时间的教学班在转换阶段就剔除，别把网格放不下的数据交给 load_world。"""
    cat = tmp_path / "2026-2027_1"
    cat.mkdir()
    (cat / "courses.json").write_text(json.dumps({
        "semester": "2026-2027-1", "scraped_at": "2026-10-19",
        "courses": [{"kcdm": "CS1001G", "kcmc": "C程序设计基础及实验",
                     "categories": ["通识必修课程|计算机类"],
                     "jxb": [{"xkkh": "CS1001G-001", "sksj": "周一第1,2节", "jsxm": "甲", "skdd": "A", "xxq": "秋"},
                             {"xkkh": "CS1001G-002", "sksj": "周六第3,4节", "jsxm": "乙", "skdd": "B", "xxq": "秋"},
                             {"xkkh": "CS1001G-003", "sksj": "周五第1节<br>周日第2节", "jsxm": "丙", "skdd": "C", "xxq": "秋"}]}],
    }), encoding="utf-8")
    plans = tmp_path / "pyfa_2026"
    plans.mkdir()
    (plans / "plan_courses.csv").write_text(
        "grade_year,major,课程号,建议学年学期,秋,冬,module_path,课程名称\n"
        '2026,计算机科学与技术,CS1001G,一(秋),80,,通识课程 / 通识必修课程 / 计算机类,C程序设计基础及实验\n',
        encoding="utf-8")
    out = tmp_path / "courses.json"
    monkeypatch.setattr(sys, "argv", ["build", "--catalog", str(tmp_path), "--out", str(out)])
    assert bc.main() == 0
    data = json.loads(out.read_text(encoding="utf-8"))
    assert [s["section_id"] for s in data["sections"]] == ["CS1001G-001"]
    assert data["_meta"]["dropped"]["weekend"] == 2
    assert "周六/周日班已剔除" in data["_meta"]["time_semantics"]
    # 剔除后的数据必须能被 planner 装载（网格只有周一到周五）
    models.load_world(data)


def test_load_capacity_json_and_csv(tmp_path):
    j = tmp_path / "cap.json"
    j.write_text(json.dumps({"A-001": 30, "A-002": 45}), encoding="utf-8")
    assert bc.load_capacity(str(j)) == {"A-001": 30, "A-002": 45}
    c = tmp_path / "cap.csv"
    c.write_text("教学班号,容量\nA-001,60\nA-002,20\n", encoding="utf-8")
    assert bc.load_capacity(str(c)) == {"A-001": 60, "A-002": 20}
    real = tmp_path / "real.csv"
    real.write_text("教学班号,余量,最大容量\nA-001,7,50\nA-002,-2,30\n", encoding="utf-8")
    assert bc.load_capacity(str(real)) == {"A-001": 50, "A-002": 30}  # 取最大容量，不是余量
    only_rs = tmp_path / "rs.csv"
    only_rs.write_text("教学班号,rs\nA-001,7\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        bc.load_capacity(str(only_rs))  # rs 是余量，绝不能被当成容量
    bad = tmp_path / "bad.csv"
    bad.write_text("课程号,人数\nA-001,60\n", encoding="utf-8")
    with pytest.raises(SystemExit):
        bc.load_capacity(str(bad))
    assert bc.load_capacity(None) == {}
