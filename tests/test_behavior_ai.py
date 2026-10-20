import pytest

from planner.behavior import ai_allocator, ai_plan_builder, ai_satisfaction
from planner.llm import LLMError
from planner.models import NeedModel, Student, load_world
from planner.selection import run_two_rounds

from fixtures import tiny_world


class FakeClient:
    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = 0
        self.failures = 0

    def chat_json(self, system, user):
        self.calls += 1
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


def _tiny_world_with_students():
    world = load_world(tiny_world())
    from planner.models import Student
    world.students = {"N1": Student("N1", "计算机科学与技术")}
    return world


def _view(world, assigned=None):
    return {
        "assignments": {sid: set(assigned or []) for sid in world.students},
        "remaining": {s: sec.capacity for s, sec in world.sections.items()},
    }


def test_ai_plan_sanitizer_drops_invalid_entries():
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"思政类": 1})
    client = FakeClient([{"drops": [], "preferences": [
        {"section_id": "A1", "rank": 1},      # 合法且未持有 → 保留
        {"section_id": "XXX", "rank": 2},     # 不存在 → 剔除
        {"section_id": "B2", "rank": 9},      # 序号越界但教学班合法 → 保留并重排
    ]}])
    llm_stats = {}
    builder = ai_plan_builder(world, {"N1": "@SKILL N1"}, {"N1": need}, client, llm_stats)
    plans = builder(1, _view(world))
    prefs = plans["N1"].prefs
    assert set(prefs) == {"A1", "B2"}
    assert sorted(prefs.values()) == [1, 1]  # 序号按课程内重排：不同课程各从 1 开始


def test_ai_plan_skips_sections_of_held_courses():
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"思政类": 1})
    client = FakeClient([{"drops": [], "preferences": [{"section_id": "A1", "rank": 1}]}])
    builder = ai_plan_builder(world, {"N1": "@SKILL N1"}, {"N1": need}, client, {})
    plans = builder(1, _view(world, assigned={"A1"}))
    assert plans["N1"].prefs == {}


def test_ai_round_raises_instead_of_falling_back():
    # 没有规则替代通路：LLM 调用失败必须让整轮失败，不能悄悄产出一份规则志愿表。
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"思政类": 1})
    client = FakeClient([LLMError("超时")])
    llm_stats = {}
    builder = ai_plan_builder(world, {"N1": "@SKILL N1"}, {"N1": need}, client, llm_stats)
    with pytest.raises(LLMError):
        builder(1, _view(world))
    assert "fallback_rounds" not in llm_stats


def _two_students():
    world = _tiny_world_with_students()
    world.students["N2"] = Student("N2", "计算机科学与技术")
    world.students["G1"] = Student("G1", "计算机科学与技术", graduating=True)
    return world


def test_pool_lines_list_applicants_with_rank_tier_and_seats():
    from planner.behavior import _pool_chunks
    world = _two_students()
    prefs = {"N1": {"B1": 1, "B2": 2}, "G1": {"B1": 1}}
    text = "\n".join(line for chunk in _pool_chunks(world, prefs, _view(world)) for line in chunk)
    assert "B1|外语B|外语类|剩余1|志愿1：G1(毕业年级)、N1(其他)" in text
    assert "B2|外语B|外语类|剩余1|志愿2：N1(其他)" in text


def test_pool_chunks_pack_whole_courses_and_drop_nothing():
    """分块只是把长志愿池切成多次调用：同一门课的教学班必须落在同一块，且一行都不能丢。"""
    from planner.behavior import _pool_chunks
    world = _two_students()
    prefs = {"N1": {"B1": 1, "B2": 2}, "G1": {"A1": 1}}
    chunks = _pool_chunks(world, prefs, _view(world), cap=1)
    starts = [[line.split("|")[0] for line in chunk] for chunk in chunks]
    assert starts == [["B1", "B2"], ["A1"]] or starts == [["A1"], ["B1", "B2"]]
    flat = [line for chunk in chunks for line in chunk]
    assert not any("因长度限制未列出" in line for line in flat)


def test_allocator_returns_the_models_grants_and_drops_malformed_entries():
    world = _two_students()
    prefs = {"N1": {"A1": 1}, "N2": {"A1": 1}}
    client = FakeClient([{"grants": [{"student_id": "N1", "section_id": "A1"}, "N2", 7]}])
    audit = {}
    allocate = ai_allocator(world, client, audit)
    assert allocate(1, prefs, _view(world)) == [("N1", "A1")]
    assert audit["sanitized"] == 2


def test_allocator_raises_when_the_model_fails():
    world = _two_students()
    allocate = ai_allocator(world, FakeClient([LLMError("503")]), {})
    with pytest.raises(LLMError):
        allocate(1, {"N1": {"A1": 1}}, _view(world))


def test_allocator_accounts_seats_across_chunks():
    """分块之间必须顺序记账：第一块已发出的名额，第二块之前就没了，超授在筛选器内部就被拒。"""
    world = _two_students()
    prefs = {"N1": {"A1": 1}, "N2": {"A1": 1}, "G1": {"B1": 1}}
    client = FakeClient([
        {"grants": [{"student_id": "N1", "section_id": "A1"},
                    {"student_id": "N2", "section_id": "A1"}]},   # A1 只有 1 座
        {"grants": [{"student_id": "G1", "section_id": "B1"}]},
    ])
    audit = {}
    allocate = ai_allocator(world, client, audit, line_cap=1)
    grants = allocate(1, prefs, _view(world))
    assert ("G1", "B1") in grants
    assert [g for g in grants if g[1] == "A1"] == [("N1", "A1")]
    assert audit["sanitized"] == 1      # N2 抢座在块内就被拒，不会带着假名额返回
    assert client.calls == 2


def test_ai_satisfaction_clamps_score():
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"思政类": 1})
    client = FakeClient([{"satisfaction": 150, "reason": "夸张"}])
    score, reason = ai_satisfaction(world, "@SKILL N1", need, set(), client, {})
    assert score == 100
    assert reason == "夸张"


def test_ai_satisfaction_raises_on_llm_error():
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"思政类": 1})
    client = FakeClient([LLMError("挂了")])
    with pytest.raises(LLMError):
        ai_satisfaction(world, "@SKILL N1", need, set(), client, {})


def test_ai_satisfaction_raises_on_non_numeric_score():
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"思政类": 1})
    client = FakeClient([{"satisfaction": "很高", "reason": "说不清"}])
    with pytest.raises(LLMError, match="不合规"):
        ai_satisfaction(world, "@SKILL N1", need, set(), client, {})


def test_ai_end_to_end_two_rounds_with_fake_llm():
    """每轮 = 每个学生一次志愿调用 + 一次筛选调用；名额不足时谁中签完全取决于筛选器给的回答。"""
    world = _tiny_world_with_students()
    world.students["N2"] = Student("N2", "计算机科学与技术")
    needs = {sid: NeedModel(student_id=sid, required={"思政类": 1}) for sid in world.students}
    skill_texts = {sid: f"@SKILL {sid}" for sid in world.students}
    pref = {"drops": [], "preferences": [{"section_id": "A1", "rank": 1}]}
    client = FakeClient([
        pref, pref,                                                            # 第一轮两份志愿表
        {"grants": [{"student_id": "N1", "section_id": "A1"},
                    {"student_id": "N2", "section_id": "A1"}]},             # 筛选器想两个都授（A1 只有 1 座）
        {"drops": [], "preferences": []}, {"drops": [], "preferences": []},  # 第二轮 N1/N2 无课可报
        {"grants": []},                                                     # 第二轮筛选
    ])
    audit = {}
    builder = ai_plan_builder(world, skill_texts, needs, client, audit)
    allocate = ai_allocator(world, client, audit)
    result = run_two_rounds(world, {}, builder, allocate, audit)
    assert [sid for sid in world.students if "A1" in result.assignments[sid]] == ["N1"]
    assert audit["sanitized"] == 1        # N2 那次超授被校验层拒掉
    # 第二轮没人报志愿：筛选器没有可决定的对象，代码不再空跑一次调用（分块改造前是 6 次）。
    assert client.calls == 5


def test_catalog_lines_only_lists_unmet_categories():
    from collections import Counter as C
    from planner.behavior import _catalog_lines
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"思政类": 1})
    text = "\n".join(_catalog_lines(world, _view(world), need, C()))
    assert "A1|思政A" in text
    assert "外语B" not in text and "PE1" not in text  # 目标之外的类别不进候选
    satisfied = "\n".join(_catalog_lines(world, _view(world), need, C({"思政类": 1})))
    assert satisfied == "（无候选教学班）"


def test_catalog_lines_caps_and_announces_omission():
    from collections import Counter as C
    from planner.behavior import _catalog_lines
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"外语类": 1})
    lines = _catalog_lines(world, _view(world), need, C(), cap=1)
    assert lines[0].startswith("B1|")
    assert lines[1] == "（因长度限制未列出：外语类 1 个教学班）"


def test_catalog_lines_share_the_cap_between_categories():
    """真实目录里专业课占两千多个班；截断必须按类别轮流取，否则学生只看到专业课。"""
    from collections import Counter as C
    from planner.behavior import _catalog_lines
    world = _tiny_world_with_students()
    need = NeedModel(student_id="N1", required={"思政类": 1, "外语类": 1, "专业课": 1})
    lines = _catalog_lines(world, _view(world), need, C(), cap=3)
    assert [line.split("|")[0] for line in lines[:3]] == ["A1", "B1", "C1"]  # 旧实现会全给排前的类别
    assert lines[3] == "（因长度限制未列出：外语类 1 个教学班）"


def test_catalog_lines_skip_full_sections():
    from collections import Counter as C
    from planner.behavior import _catalog_lines
    world = _tiny_world_with_students()
    view = _view(world)
    view["remaining"]["B2"] = 0
    need = NeedModel(student_id="N1", required={"外语类": 1})
    lines = _catalog_lines(world, view, need, C())
    assert any(line.startswith("B1|") for line in lines)
    assert not any(line.startswith("B2|") for line in lines)
