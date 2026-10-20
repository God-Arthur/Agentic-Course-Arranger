import re
from collections import Counter

import pytest

from planner import pipeline
from planner.llm import LLMError
from planner.models import category_key
from planner.pipeline import DemoError, run_pipeline

from fixtures import demo_world, write_courses


def test_missing_course_file_raises_clear_error(tmp_path):
    with pytest.raises(DemoError, match="缺少课程数据"):
        run_pipeline(6, str(tmp_path / "nope.json"))


def test_requires_llm_config(tmp_path):
    path = write_courses(tmp_path, demo_world(30))
    with pytest.raises(DemoError, match="LLM 配置"):
        run_pipeline(6, path, llm=None)


def test_invalid_student_count(tmp_path):
    path = write_courses(tmp_path, demo_world(30))
    with pytest.raises(DemoError):
        run_pipeline(1, path)
    with pytest.raises(DemoError):
        run_pipeline(61, path)


def test_bad_course_data_rejected(tmp_path):
    data = demo_world(30)
    data["sections"][0]["capacity"] = 0
    path = write_courses(tmp_path, data)
    with pytest.raises(DemoError, match="不合规"):
        run_pipeline(6, path)


def _candidate_ids(user: str) -> list[str]:
    """候选/已排课表行都是 `教学班ID|课程|…` 格式，取出模型看到的班号。"""
    return [line.split("|")[0] for line in user.splitlines() if "|" in line]


def _prefs_from_candidates(user: str) -> list[dict]:
    """把候选教学班全都申报为志愿（每门课内连续编号、每类别 ≤3），剩下的交给事实校验。"""
    per_course: dict[str, int] = {}
    per_category: Counter = Counter()
    prefs: list[dict] = []
    for line in user.splitlines():
        fields = line.split("|")
        if len(fields) != 6 or not fields[3].startswith("容量"):
            continue
        section_id = fields[0]
        course_id = section_id.rsplit("-", 1)[0] if "-" in section_id else section_id
        key = category_key(fields[2])
        rank = per_course.get(course_id, 0)
        if rank >= 3 or per_category[key] >= 3:
            continue
        per_course[course_id] = rank + 1
        per_category[key] += 1
        prefs.append({"section_id": section_id, "rank": rank + 1})
    return prefs


def _grants_from_pool(user: str) -> list[dict]:
    """筛选器把志愿池里出现的每个学生都授一次，超授/冲突由 selection 层拒绝。"""
    grants: list[dict] = []
    for line in user.splitlines():
        fields = line.split("|")
        if len(fields) != 5 or not fields[3].startswith("剩余"):
            continue
        for student_id in re.findall(r"([^、；：]+)\(", fields[4]):
            grants.append({"student_id": student_id, "section_id": fields[0]})
    return grants


class FakePipelineClient:
    """按 system 提示词分发结构化回复的假 LLM 客户端（不发网络请求）。

    排课、申报志愿、筛选中签一律回答"把给出的都算上"：流水线里剩下的只有事实校验
    （余座、时间冲突、同课重复、志愿上限），没有任何规则替模型做取舍。
    """

    def __init__(self, base_url, model, api_key, min_interval=3.0):
        self.calls = 0
        self.failures = 0

    def chat_json(self, system, user):
        self.calls += 1
        if "需求解析器" in system:
            return {"required": {"思政类": 1, "外语类": 1},
                    "elective": {"通识与体育类": 1},
                    "avoid_slots": [], "notes": "假解析"}
        if "预置课表规划器" in system:
            return {"sections": _candidate_ids(user), "why": "全都选"}
        if "选课筛选器" in system:
            return {"grants": _grants_from_pool(user)}
        if "满意度评估器" in system:
            return {"satisfaction": 85, "reason": "假评分"}
        return {"drops": [], "preferences": _prefs_from_candidates(user)}


def test_ai_end_to_end_with_fake_client(tmp_path, monkeypatch):
    monkeypatch.setattr(pipeline, "LLMClient", FakePipelineClient)
    path = write_courses(tmp_path, demo_world(30))
    result = run_pipeline(6, path, llm={"base_url": "http://x/v1", "model": "m",
                                        "api_key": "k"}, seed=7, restarts=2)
    summary = result["summary"]
    assert summary["students"] == 6
    assert "mode" not in summary
    assert summary["mean_satisfaction"] == 85.0
    assert len(result["history"]) == 3  # 首轮 + 2 次复盘改写
    assert result["llm_stats"]["calls"] > 0
    for student in result["students"]:
        assert student["preset"], f"{student['student_id']} 预置为空"
        assert student["satisfaction"] == 85
        assert student["need"]["source"] == "llm"
        # 预置覆盖解析出的全部类别目标：必修 + 选修都出现
        preset_keys = {s["category_key"] for s in student["preset"]}
        for key in list(student["need"]["required"]) + list(student["need"]["elective"]):
            assert key in preset_keys, f"{student['student_id']} 预置缺少类别 {key}"
        # 最终课表无时间冲突
        occupied = []
        for section in student["final"]:
            assert not set(section["slots"]) & set(occupied)
            occupied.extend(section["slots"])
        # 预置保留 = 预置 ∩ 最终
        preset_ids = {s["section_id"] for s in student["preset"]}
        final_ids = {s["section_id"] for s in student["final"]}
        kept_ids = {s["section_id"] for s in student["preset_kept"]}
        assert kept_ids == preset_ids & final_ids


def test_llm_failure_is_not_masked(tmp_path, monkeypatch):
    """AI 调用失败必须报错退出：流水线没有任何规则替代通路可以兜底。"""
    class DeadClient(FakePipelineClient):
        def chat_json(self, system, user):
            raise LLMError("端点 503")

    monkeypatch.setattr(pipeline, "LLMClient", DeadClient)
    path = write_courses(tmp_path, demo_world(30))
    with pytest.raises(LLMError):
        run_pipeline(4, path, llm={"base_url": "http://x/v1", "model": "m", "api_key": "k"})
