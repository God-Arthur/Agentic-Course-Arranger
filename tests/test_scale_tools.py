"""专业人数推测工具的回归用例（tools/estimate_major_sizes.py）。全部用例自带临时目录，不读 zju_catalog。

原先同文件里的 tools/scale_test.py 用例已随该脚本一起删除：规模测试走的是规则通路，
且它把四届需求压进一个学期，口径不成立。
"""
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, ROOT / "tools" / filename)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


est = _load("estimate_major_sizes", "estimate_major_sizes.py")

MAJORS = ["计算机科学与技术", "机械工程", "机械工程（卓越工程师班）", "竺可桢学院"]


def test_allocate_exact_hit_and_unique_normalized_hit():
    rows = [{"major": "计算机科学与技术", "plan": 60, "source": "一段"},
            {"major": "计算机科学与技术（舟山校区）", "plan": 20, "source": "一段"}]  # 去括号后唯一命中
    sizes, stats = est.allocate(MAJORS, rows)
    assert sizes["计算机科学与技术"] == 80
    assert stats == {"direct_majors": 1, "direct_plan": 80, "group_rows": 0, "group_plan": 0,
                     "undecided_majors": 3, "pool_share_per_major": 0.0}


def test_allocate_exact_name_wins_over_a_parenthetical_sibling():
    """一段表的「机械工程」就是普通班：方案清单里 机械工程 与 机械工程（卓越工程师班）并存时精确名优先。"""
    rows = [{"major": "计算机科学与技术", "plan": 10, "source": "一段"},
            {"major": "机械工程", "plan": 117, "source": "一段"}]
    sizes, stats = est.allocate(MAJORS, rows)
    assert sizes["机械工程"] == 117 and sizes["计算机科学与技术"] == 10
    assert (stats["group_rows"], stats["group_plan"]) == (0, 0)
    # 没有任何浙江计划行命中、又不在大类池里 → 只能是 0，等分只兜住大类行
    assert sizes["机械工程（卓越工程师班）"] == sizes["竺可桢学院"] == 0.0


def test_allocate_pools_only_genuinely_ambiguous_rows():
    majors = ["城乡规划（四年制）", "自动化（卓越工程师班）", "自动化（控制）", "计算机科学与技术"]
    rows = [{"major": "计算机科学与技术", "plan": 20, "source": "一段"},
            {"major": "自动化", "plan": 10, "source": "一段"},       # 方案里有多个自动化变体 → 不猜，进 pool
            {"major": "城乡规划", "plan": 5, "source": "三位一体"},   # 去括号后唯一命中 → 记到 城乡规划（四年制）
            {"major": "工科试验班", "plan": 100, "source": "一段"}]   # 大类行，没有官方分流构成
    sizes, stats = est.allocate(majors, rows)
    assert sizes["计算机科学与技术"] == 20 and sizes["城乡规划（四年制）"] == 5
    assert sizes["自动化（卓越工程师班）"] == sizes["自动化（控制）"] == 55.0  # (10+100)/2
    assert stats == {"direct_majors": 2, "direct_plan": 25, "group_rows": 2, "group_plan": 110,
                     "undecided_majors": 2, "pool_share_per_major": 55.0}


def test_scale_to_rounds_to_exact_total_and_keeps_ranking():
    assert est.scale_to({"甲": 1.0, "乙": 1.0, "丙": 1.0}, 10) == {"丙": 4, "乙": 3, "甲": 3}
    got = est.scale_to({"甲": 90, "乙": 10, "丙": 0.5}, 6000)
    assert sum(got.values()) == 6000
    assert got["甲"] > got["乙"] > got["丙"] >= 0


def _write_enroll(tmp_path: Path) -> Path:
    enroll = tmp_path / "enroll_2026"
    enroll.mkdir()
    (enroll / "zhejiang_yiduan_2026.csv").write_text(
        "专业名称,计划数,来源\n计算机科学与技术,30,一段\n工科试验班,60,一段\n", encoding="utf-8")
    (enroll / "zhejiang_sanyi_tixi_2026.csv").write_text(
        "专业名称,计划数\n竺可桢学院,10\n", encoding="utf-8")
    return enroll


def test_main_writes_sizes_that_sum_to_the_anchors(tmp_path, monkeypatch):
    courses = tmp_path / "courses.json"
    courses.write_text(json.dumps({"plan": {"majors": MAJORS}}, ensure_ascii=False), encoding="utf-8")
    out = tmp_path / "major_sizes.json"
    monkeypatch.setattr(sys, "argv", ["estimate", "--courses", str(courses),
                                      "--enroll", str(_write_enroll(tmp_path)),
                                      "--per-grade", "6000", "--out", str(out)])
    assert est.main() == 0
    doc = json.loads(out.read_text(encoding="utf-8"))
    assert sum(doc["per_grade"].values()) == 6000
    assert sum(doc["all_grades"].values()) == 24000
    meta = doc["_meta"]
    assert meta["zhejiang_plan_total"] == 100 and meta["national_scale_factor"] == 60.0
    # 口径必须随产物一起交付，否则下游会把推测人数当教务实数
    assert any("招生计划数≠实际录取人数" in a for a in meta["assumptions"])
    assert any("大类行" in a for a in meta["assumptions"])


def test_main_refuses_a_plan_column_that_is_not_an_integer(tmp_path, monkeypatch):
    courses = tmp_path / "courses.json"
    courses.write_text(json.dumps({"plan": {"majors": MAJORS}}), encoding="utf-8")
    enroll = _write_enroll(tmp_path)
    (enroll / "zhejiang_yiduan_2026.csv").write_text("专业名称,计划数\n工科试验班,约30\n", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", ["estimate", "--courses", str(courses), "--enroll", str(enroll)])
    with pytest.raises(SystemExit, match="计划数非整数"):
        est.main()
