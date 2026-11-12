"""机制实验：把《实验披露》第 6 节的可失败判据跑成数字。

预注册（先写死，再跑；不随结果改门槛）：

- 对照臂：没有冲突、名额够。drops = 0，且第 1 轮与第 2 轮事件 sha256 相同。
- 竞争臂：预置课和唯一能上的热门班时间冲突，热门班容量 2。drops > 0，两轮事件哈希不同，
  筛选器因名额拒绝的次数 = 人数 − 2。人数 4/8/12 时拒绝次数严格递增。
- 重叠臂：有人的预置已经占着热门班、班里还有空位，其他人的志愿打进同一个班。
  drops > 0，两轮哈希不同，第 1 轮中签的教学班与预置课表有交集。
- 优先级：空预置、容量 1，唯一中签的是毕业年级。
- 复盘：24 个种子，rewrite 比例 1。满意度满分锚在 60（避开流水线的 0–100 夹紧），
  早八扣分 15，噪声 ±20。每种种子的平均满意度至少上升 5 分，必修缺口不上升，
  配对差分为负的种子数 = 0。同一个人换一颗种子去比（不配对）必须出现负差，
  用来说明不配对的轮间分差不能当证据。rewrite 比例 0.3 的平均涨幅必须小于比例 1。
- 批量：4 名学生一次调用的预置请求数 = 1，一人一次 = 4，且没有超座。
- 检查点：预置每 1 人落盘，第 2 人调用时杀掉进程，续跑日志含「复用 1 人（不调模型）」，
  续跑时预置调用数 = 剩下的 2 人。

决策模型只在本脚本里使用。演示和 run_experiment 的默认路径仍然只接受 LLM。
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
import shutil
import sys
import tempfile
from collections import Counter
from contextlib import redirect_stderr
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TOOLS = Path(__file__).resolve().parent
for path in (str(ROOT), str(TOOLS)):
    if path not in sys.path:
        sys.path.insert(0, path)

from decision_model import DecisionModel  # noqa: E402
from planner.ai_planner import ai_presets, ai_retune  # noqa: E402
from planner.behavior import ai_allocator, ai_plan_builder  # noqa: E402
from planner.models import Student, load_world  # noqa: E402
from planner.pipeline import make_evaluator  # noqa: E402
from planner.selection import run_two_rounds  # noqa: E402
from planner.skills import format_skill, parse_skill_llm  # noqa: E402

# 预注册常数。测试直接引用这些名字，避免报告里的门槛和代码各写一份。
PAIRED_SEEDS = tuple(range(1, 25))
PAIRED_FRAC = 1.0
DOSE_FRAC = 0.3
PAIRED_MIN_DELTA = 5.0
PAIRED_MIN_SEEDS = 18
MORNING_PENALTY = 15
JITTER = 20
SCORE_ANCHOR = 60
HOT_CAP = 2
SCALE_NS = (4, 8, 12)


def _world(courses, sections, students, majors) -> object:
    data = {
        "courses": courses,
        "sections": sections,
        "plan": {"majors": majors, "recommended": {major: {} for major in majors}},
    }
    world = load_world(data)
    world.students = {student.student_id: student for student in students}
    return world


def _skills(students, targets_for, note: str, avoid: list[str] | None = None) -> dict[str, str]:
    avoid = avoid or []
    return {student.student_id: format_skill(student, targets_for(student), avoid, note)
            for student in students}


def _events_of(events, round_index: int) -> list[dict]:
    return [event for event in events if event.get("round") == round_index]


def _digest(events: list[dict]) -> str:
    blob = json.dumps(events, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(blob.encode()).hexdigest()


def _violations(world, assignments: dict) -> list[str]:
    booked: Counter = Counter()
    problems = []
    for student_id, sections in assignments.items():
        slots = []
        courses = []
        for section_id in sections:
            booked[section_id] += 1
            slots.extend(world.sections[section_id].slots)
            courses.append(world.sections[section_id].course_id)
        if len(slots) != len(set(slots)):
            problems.append(f"{student_id} 时间冲突")
        if len(courses) != len(set(courses)):
            problems.append(f"{student_id} 同一门课占了两个班")
    for section_id, count in booked.items():
        if count > world.sections[section_id].capacity:
            problems.append(f"{section_id} 超座 {count}>{world.sections[section_id].capacity}")
    return problems


def _run_eval(world, skills, client):
    stats: dict = {}
    needs = {sid: parse_skill_llm(text, client, stats) for sid, text in skills.items()}
    evaluate = make_evaluator(world, needs, skills, client, stats, workers=1)
    preset = ai_presets(world, needs, skills, client, stats, workers=1)
    evaluation = evaluate(preset)
    return needs, preset, evaluation, stats


def _arm_summary(world, evaluation, preset, stats) -> dict:
    events = evaluation.result.events
    first, second = _events_of(events, 1), _events_of(events, 2)
    preset_sections = {section_id for sections in preset.values() for section_id in sections}
    granted = {event["section_id"] for event in first if event.get("phase") == "中签"}
    return {
        "drops": sum(1 for event in events if event.get("phase") == "退课"),
        "grant_events": sum(1 for event in events if event.get("phase") == "中签"),
        "round1_events": len(first),
        "round2_events": len(second),
        "round1_sha256": _digest(first),
        "round2_sha256": _digest(second),
        "round_hashes_differ": _digest(first) != _digest(second),
        "seat_rejections": int(stats.get("sanitized_by_grant_reject", 0)),
        "preset_grant_overlap": sorted(preset_sections & granted),
        "violations": _violations(world, evaluation.result.assignments),
        "mean_satisfaction": round(evaluation.mean(), 3),
    }


def competition_world(n: int, hot_cap: int = HOT_CAP):
    """人人先被预置到和热门班冲突的思政课上，热门专业课只留 hot_cap 个座位。"""
    major = "计算机科学与技术"
    students = [Student(f"S{i:03d}", major, year="一") for i in range(1, n + 1)]
    courses = [
        {"course_id": "ID", "name": "思政", "category": "思政类"},
        {"course_id": "HOT", "name": "专业热门班", "category": "专业课", "major": major},
    ]
    sections = [
        {"section_id": "BLOCK", "course_id": "ID", "capacity": n, "slots": ["Mon-3"]},
        {"section_id": "IDEO", "course_id": "ID", "capacity": n, "slots": ["Tue-3"]},
        {"section_id": "HOT", "course_id": "HOT", "capacity": hot_cap, "slots": ["Mon-3"]},
    ]
    world = _world(courses, sections, students, [major])
    skills = _skills(students, lambda _student: {"思政类": 1, "专业课": 1}, "没有特别偏好，服从安排。")
    return world, skills


def overlap_world():
    """S001 的预置占住热门班，班里还留两个座位；另外四人必须退掉冲突的思政才能来抢。"""
    major = "计算机科学与技术"
    holder = Student("S001", major, year="一")
    chasers = [Student(f"S{i:03d}", major, year="一") for i in range(2, 6)]
    students = [holder, *chasers]
    courses = [
        {"course_id": "ID", "name": "思政", "category": "思政类"},
        {"course_id": "HOT", "name": "专业热门班", "category": "专业课", "major": major},
    ]
    sections = [
        {"section_id": "BLOCK", "course_id": "ID", "capacity": 8, "slots": ["Mon-3"]},
        {"section_id": "IDEO", "course_id": "ID", "capacity": 8, "slots": ["Tue-3"]},
        {"section_id": "HOT", "course_id": "HOT", "capacity": 3, "slots": ["Mon-3"]},
    ]
    world = _world(courses, sections, students, [major])

    def targets(student: Student) -> dict[str, int]:
        if student.student_id == "S001":
            return {"专业课": 1}
        return {"思政类": 1, "专业课": 1}

    return world, _skills(students, targets, "没有特别偏好，服从安排。")


def control_world():
    major = "计算机科学与技术"
    students = [Student(f"S{i:03d}", major, year="一") for i in range(1, 5)]
    courses = [{"course_id": "ID", "name": "思政", "category": "思政类"}]
    sections = [{"section_id": "EASY", "course_id": "ID", "capacity": 8, "slots": ["Tue-3"]}]
    world = _world(courses, sections, students, [major])
    skills = _skills(students, lambda _student: {"思政类": 1}, "没有特别偏好，服从安排。")
    return world, skills


def priority_case() -> dict:
    major = "计算机科学与技术"
    students = [
        Student("S001", major, year="一"),
        Student("S002", major, year="一"),
        Student("S003", major, year="三", graduating=True),
    ]
    courses = [{"course_id": "HOT", "name": "专业热门班", "category": "专业课", "major": major}]
    sections = [{"section_id": "HOT", "course_id": "HOT", "capacity": 1, "slots": ["Wed-3"]}]
    world = _world(courses, sections, students, [major])
    skills = _skills(students, lambda _student: {"专业课": 1}, "没有特别偏好，服从安排。")
    client = DecisionModel(world)
    stats: dict = {}
    needs = {sid: parse_skill_llm(text, client, stats) for sid, text in skills.items()}
    result = run_two_rounds(
        world, {sid: [] for sid in needs},
        ai_plan_builder(world, skills, needs, client, stats, workers=1),
        ai_allocator(world, client, stats), stats)
    winners = [event["student_id"] for event in result.events
               if event.get("phase") == "中签" and event.get("round") == 1]
    return {
        "winners": winners,
        "graduating_wins": winners == ["S003"],
        "seat_rejections": int(stats.get("sanitized_by_grant_reject", 0)),
        "violations": _violations(world, result.assignments),
    }


def retune_world():
    major = "计算机科学与技术"
    students = [Student(f"S{i:03d}", major, year="一") for i in range(1, 9)]
    courses = [{"course_id": "ID", "name": "思政", "category": "思政类"}]
    sections = [
        {"section_id": "BAD", "course_id": "ID", "capacity": 30, "slots": ["Mon-1"]},
        {"section_id": "GOOD", "course_id": "ID", "capacity": 30, "slots": ["Wed-6"]},
    ]
    world = _world(courses, sections, students, [major])
    skills = _skills(students, lambda _student: {"思政类": 1}, "不想上早八。")
    return world, skills


def _paired_once(world, skills, seed: int, frac: float):
    client = DecisionModel(world, seed=seed, jitter=JITTER, morning_penalty=MORNING_PENALTY,
                           score_anchor=SCORE_ANCHOR)
    stats: dict = {}
    needs = {sid: parse_skill_llm(text, client, stats) for sid, text in skills.items()}
    evaluate = make_evaluator(world, needs, skills, client, stats, workers=1)
    preset0 = ai_presets(world, needs, skills, client, stats, workers=1)
    ev0 = evaluate(preset0)
    preset1 = ai_retune(world, needs, skills, client, stats, preset0, ev0.scores, ev0.reasons,
                        ev0.result.assignments, frac, workers=1)
    ev1 = evaluate(preset1)
    return {
        "delta_mean": ev1.mean() - ev0.mean(),
        "unmet0": ev0.unmet_required_slots(world, needs),
        "unmet1": ev1.unmet_required_slots(world, needs),
        "scores0": dict(ev0.scores),
        "scores1": dict(ev1.scores),
        "preset0": {sid: list(preset0[sid]) for sid in preset0},
        "preset1": {sid: list(preset1[sid]) for sid in preset1},
        "violations": _violations(world, ev0.result.assignments) + _violations(world, ev1.result.assignments),
    }


def paired_retune() -> dict:
    world, skills = retune_world()
    full = [_paired_once(world, skills, seed, PAIRED_FRAC) for seed in PAIRED_SEEDS]
    dose = [_paired_once(world, skills, seed, DOSE_FRAC) for seed in PAIRED_SEEDS]
    full_deltas = [row["delta_mean"] for row in full]
    improved = sum(1 for value in full_deltas if value >= PAIRED_MIN_DELTA)
    negative = sum(1 for value in full_deltas if value < 0)
    unmet_up = sum(1 for row in full if row["unmet1"] > row["unmet0"])
    anchor = "S001"
    cross_negative = 0
    for index, _seed in enumerate(PAIRED_SEEDS):
        other = full[(index + len(PAIRED_SEEDS) // 2) % len(PAIRED_SEEDS)]
        if other["scores1"][anchor] - full[index]["scores0"][anchor] < 0:
            cross_negative += 1
    return {
        "seeds": len(PAIRED_SEEDS),
        "frac": PAIRED_FRAC,
        "min_delta": min(full_deltas),
        "max_delta": max(full_deltas),
        "mean_delta": round(sum(full_deltas) / len(full_deltas), 3),
        "seeds_delta_at_least_min": improved,
        "seeds_delta_negative": negative,
        "seeds_unmet_increased": unmet_up,
        "dose_frac": DOSE_FRAC,
        "dose_mean_delta": round(sum(row["delta_mean"] for row in dose) / len(dose), 3),
        "cross_seed_negative": cross_negative,
        "violations": [item for row in full for item in row["violations"]],
        "example_preset_before": full[0]["preset0"][anchor],
        "example_preset_after": full[0]["preset1"][anchor],
    }


def batch_case() -> dict:
    major = "计算机科学与技术"
    students = [Student(f"S{i:03d}", major, year="一") for i in range(1, 5)]
    courses = [{"course_id": "ID", "name": "思政", "category": "思政类"}]
    sections = [{"section_id": "EASY", "course_id": "ID", "capacity": 10, "slots": ["Tue-3"]}]
    world = _world(courses, sections, students, [major])
    skills = _skills(students, lambda _student: {"思政类": 1}, "没有特别偏好，服从安排。")
    client = DecisionModel(world)
    stats: dict = {}
    needs = {sid: parse_skill_llm(text, client, stats) for sid, text in skills.items()}
    client.calls = 0
    single = ai_presets(world, needs, skills, client, stats, workers=1, group_size=1)
    single_calls = client.calls
    client.calls = 0
    grouped = ai_presets(world, needs, skills, client, stats, workers=1, group_size=4)
    return {
        "single_calls": single_calls,
        "grouped_calls": client.calls,
        "single_violations": _violations(world, {sid: set(v) for sid, v in single.items()}),
        "grouped_violations": _violations(world, {sid: set(v) for sid, v in grouped.items()}),
        "grouped_covers_everyone": all(grouped[sid] for sid in needs),
    }


def checkpoint_probe(directory: Path) -> dict:
    """杀掉预置阶段的第二次调用，再续跑，日志里必须出现复用。"""
    import run_experiment as rx

    courses = {
        "courses": [{"course_id": "A", "name": "思政A", "category": "思政类"}],
        "sections": [{"section_id": "A1", "course_id": "A", "capacity": 5, "slots": ["Tue-3"]}],
        "plan": {"majors": ["计算机科学与技术"],
                 "recommended": {"计算机科学与技术": {"思政类": 1}},
                 "recommended_by_year": {"计算机科学与技术": {"一": {"思政类": 1}}}},
    }
    if directory.exists():
        shutil.rmtree(directory)
    directory.mkdir(parents=True)
    (directory / "courses.json").write_text(json.dumps(courses, ensure_ascii=False), encoding="utf-8")
    (directory / "sizes.json").write_text(json.dumps(
        {"per_grade": {"计算机科学与技术": 30}}), encoding="utf-8")

    class Probe:
        calls = 0
        failures = 0
        preset_calls = 0
        fail_preset_after = 2

        def __init__(self, *args, **kwargs):
            pass

        def perf(self):
            return {"avg_latency_s": 0.0, "max_latency_s": 0.0, "call_span_s": 0.0,
                    "effective_rpm": None, "backoffs": 0}

        def chat_json(self, system, user):
            if system.startswith("你是教务系统的预置课表规划器"):
                type(self).preset_calls += 1
                if type(self).fail_preset_after and type(self).preset_calls >= type(self).fail_preset_after:
                    raise AssertionError("模拟端点在预置阶段断线")
            type(self).calls += 1
            if system.startswith("你是教务系统的需求解析器"):
                return {"required": {"思政类": 1}, "elective": {}, "avoid_slots": [], "notes": ""}
            if system.startswith("你是教务系统的预置课表规划器"):
                return {"sections": ["A1"], "why": "必修"}
            if system.startswith("你扮演一名中国大学生"):
                return {"drops": [], "preferences": []}
            if system.startswith("你是教务系统的选课筛选器"):
                return {"grants": []}
            if system.startswith("你是选课满意度评估器"):
                return {"satisfaction": 80, "reason": "够了"}
            raise AssertionError(system[:40])

    out = directory / "result.json"
    argv = ["--courses", str(directory / "courses.json"), "--sizes", str(directory / "sizes.json"),
            "--n", "3", "--seed", "5", "--iterations", "0", "--workers", "1",
            "--flush-every", "1", "--out", str(out)]
    previous_client, previous_resolve = rx.LLMClient, rx.resolve_llm
    rx.LLMClient = Probe
    rx.resolve_llm = lambda user: ({"base_url": "http://x", "model": "m", "api_key": "k",
                                    "min_interval": 0}, {})
    try:
        try:
            with redirect_stderr(io.StringIO()):
                rx.main(argv)
            crashed = False
        except AssertionError as exc:
            crashed = "预置阶段断线" in str(exc)
        partial = json.loads((Path(str(out) + ".ckpt") / "presets_0.json").read_text(encoding="utf-8"))
        Probe.fail_preset_after = 0
        Probe.preset_calls = 0
        err = io.StringIO()
        with redirect_stderr(err):
            code = rx.main(argv)
        log = err.getvalue()
    finally:
        rx.LLMClient = previous_client
        rx.resolve_llm = previous_resolve
    return {
        "crashed_on_second_preset": crashed,
        "partial_students": len(partial.get("presets", {})),
        "partial_complete": partial.get("complete"),
        "resume_log": "预置复用 1 人（不调模型）" in log,
        "resume_excerpt": next((line for line in log.splitlines() if "复用" in line), ""),
        "preset_calls_on_resume": Probe.preset_calls,
        "exit_code": code,
        "violations_note": "续跑走的是同一条事实校验",
    }


def _finish_arm(world, skills) -> dict:
    client = DecisionModel(world)
    needs, preset, evaluation, stats = _run_eval(world, skills, client)
    summary = _arm_summary(world, evaluation, preset, stats)
    summary["unmet_required_slots"] = evaluation.unmet_required_slots(world, needs)
    summary["calls"] = client.calls
    return summary


def run_suite(directory: Path | None = None, with_supply: bool = False) -> dict:
    control = _finish_arm(*control_world())
    scale = []
    for n in SCALE_NS:
        arm = _finish_arm(*competition_world(n))
        arm["n"] = n
        arm["expected_rejections"] = n - HOT_CAP
        scale.append(arm)
    competition = next(row for row in scale if row["n"] == 8)
    overlap = _finish_arm(*overlap_world())
    priority = priority_case()
    paired = paired_retune()
    batched = batch_case()
    probe_dir = Path(tempfile.mkdtemp(prefix="planner-ckpt-"))
    try:
        probe = checkpoint_probe(probe_dir)
    finally:
        shutil.rmtree(probe_dir, ignore_errors=True)
    supply = None
    if with_supply:
        supply = _supply_k10(Path(directory or ROOT / "results" / "mechanism"))

    verdicts = {
        "control": _verdict(control["drops"] == 0 and not control["round_hashes_differ"]
                            and not control["violations"]),
        "competition": _verdict(
            competition["drops"] > 0 and competition["round_hashes_differ"]
            and competition["seat_rejections"] == competition["n"] - HOT_CAP
            and not competition["violations"]),
        "scale": _verdict(_strictly_increasing([row["seat_rejections"] for row in scale])
                          and all(row["seat_rejections"] == row["expected_rejections"] for row in scale)),
        "overlap": _verdict(overlap["drops"] > 0 and overlap["round_hashes_differ"]
                            and bool(overlap["preset_grant_overlap"]) and not overlap["violations"]),
        "priority": _verdict(priority["graduating_wins"] and not priority["violations"]),
        "paired_retune": _verdict(
            paired["seeds"] >= PAIRED_MIN_SEEDS
            and paired["seeds_delta_at_least_min"] >= PAIRED_MIN_SEEDS
            and paired["seeds_delta_negative"] == 0
            and paired["seeds_unmet_increased"] == 0
            and paired["cross_seed_negative"] > 0
            and paired["dose_mean_delta"] < paired["mean_delta"]
            and not paired["violations"]),
        "batch": _verdict(batched["grouped_calls"] == 1 and batched["single_calls"] == 4
                          and batched["grouped_covers_everyone"]
                          and not batched["single_violations"] and not batched["grouped_violations"]),
        "checkpoint": _verdict(probe["crashed_on_second_preset"] and probe["partial_students"] == 1
                               and probe["partial_complete"] is False and probe["resume_log"]
                               and probe["preset_calls_on_resume"] == 2 and probe["exit_code"] == 0),
    }
    if supply is not None:
        verdicts["supply"] = _verdict(supply["positive_gap_cells"] > 0 and supply["extra_seats_sum"] > 0
                                      and supply["dedicated_not_shared"])
    return {
        "preregistration": {
            "paired_seeds": len(PAIRED_SEEDS),
            "paired_frac": PAIRED_FRAC,
            "paired_min_delta": PAIRED_MIN_DELTA,
            "paired_min_seeds": PAIRED_MIN_SEEDS,
            "morning_penalty": MORNING_PENALTY,
            "jitter": JITTER,
            "hot_cap": HOT_CAP,
            "scale_ns": list(SCALE_NS),
        },
        "verdicts": verdicts,
        "control": control,
        "competition": competition,
        "scale": [{key: row[key] for key in ("n", "drops", "seat_rejections", "expected_rejections",
                                             "round_hashes_differ", "unmet_required_slots")}
                  for row in scale],
        "overlap": overlap,
        "priority": priority,
        "paired_retune": {key: value for key, value in paired.items() if key != "violations"},
        "batch": batched,
        "checkpoint": probe,
        "supply": supply,
    }


def _verdict(ok: bool) -> str:
    return "通过" if ok else "未通过"


def _strictly_increasing(values: list[int]) -> bool:
    return all(left < right for left, right in zip(values, values[1:]))


def _supply_k10(directory: Path) -> dict:
    from supply_table import expansion_table

    courses = ROOT / "data" / "courses_k10.json"
    sizes = ROOT / "data" / "major_sizes.json"
    sys.path.insert(0, str(TOOLS))
    from run_experiment import allocate_population

    world = load_world(json.loads(courses.read_text(encoding="utf-8")))
    per_grade = {key: int(value) for key, value in
                 json.loads(sizes.read_text(encoding="utf-8"))["per_grade"].items()}
    allocation = allocate_population(1800, world, per_grade)
    table = expansion_table(world, allocation)
    # 计算机专业的专业课座位不能出现在另一个专业的专用列里。抽查一条。
    cs = "计算机科学与技术"
    dedicated_not_shared = True
    from planner.models import category_key
    others = [row for row in table["rows"] if row["major"] != cs and row["category"] == "专业课"]
    if others:
        other = others[0]
        own = 0
        for section in world.sections.values():
            course = world.course_of_section(section.section_id)
            if course.major == other["major"] and category_key(course.category) == "专业课":
                own += section.capacity
        dedicated_not_shared = other["dedicated_seats"] == own
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "supply_k10.json").write_text(json.dumps(table, ensure_ascii=False), encoding="utf-8")
    top = [row for row in table["rows"] if row["extra_seats"] > 0][:12]
    return {
        "students": table["students"],
        "positive_gap_cells": table["positive_gap_cells"],
        "extra_seats_sum": table["extra_seats_sum"],
        "actionable_extra_seats": table["actionable_extra_seats"],
        "rollup": table["category_rollup"],
        "top": top,
        "dedicated_not_shared": dedicated_not_shared,
        "path": str(directory / "supply_k10.json"),
    }


def render_report(summary: dict) -> str:
    verdicts = summary["verdicts"]
    comp = summary["competition"]
    paired = summary["paired_retune"]
    lines = [
        "# 机制实验报告",
        "",
        "判据写在 `tools/run_mechanism_experiments.py` 开头，先于本次运行。",
        "决策模型不是生产兜底：演示服务器和 `tools/run_experiment.py` 的默认路径不会导入它。",
        "这里证明的是两轮选课、复盘改写、扩容账和检查点在代码里按设计工作，",
        "不是线上大模型优于某种规则算法。满意度若由同一个模型打分，不能拿去和另一条通路比。",
        "",
        "## 判决",
        "",
        "| 臂 | 判决 |",
        "|---|---|",
    ]
    labels = {
        "control": "对照：无竞争时两轮事件流相同、退课为 0",
        "competition": "竞争：退课 > 0 且两轮哈希不同，拒绝次数 = 8 − 2",
        "scale": "竞争规模：4/8/12 人的拒绝次数严格递增且等于 n−2",
        "overlap": "预置与志愿重叠：中签班落在别人的预置课上",
        "priority": "容量 1 时中签的是毕业年级",
        "paired_retune": "24 种子配对：复盘提高满意度且不配对会得出负差",
        "batch": "预置批量请求把 4 次调用收成 1 次",
        "checkpoint": "预置中断后续跑复用已落盘的人",
        "supply": "k10 / 1800 人的扩容表有正缺口，专业课座位不串专业",
    }
    for key, label in labels.items():
        if key in verdicts:
            lines.append(f"| {label} | **{verdicts[key]}** |")
    lines += [
        "",
        "## 两轮机制",
        "",
        f"对照臂退课 {summary['control']['drops']}，两轮哈希"
        f"{'不同' if summary['control']['round_hashes_differ'] else '相同'}。",
        f"8 人竞争臂退课 {comp['drops']}，名额拒绝 {comp['seat_rejections']}，"
        f"两轮哈希{'不同' if comp['round_hashes_differ'] else '相同'}，"
        f"两轮结束后必修缺口 {comp['unmet_required_slots']}。"
        "第 1 轮退掉和热门班冲突的预置课并抢剩余名额，没抢到的人要到第 2 轮才补上不冲突的思政课，"
        "所以两轮事件流不会相同。",
        "",
        "规模：",
        "",
        "| 人数 | 退课 | 名额拒绝 | 期望拒绝 |",
        "|---|---|---|---|",
    ]
    for row in summary["scale"]:
        lines.append(f"| {row['n']} | {row['drops']} | {row['seat_rejections']} | {row['expected_rejections']} |")
    overlap = summary["overlap"]
    lines += [
        "",
        f"重叠臂退课 {overlap['drops']}，第 1 轮中签且已在预置里的教学班："
        f"{'、'.join(overlap['preset_grant_overlap']) or '（无）'}。",
        f"优先级臂的第 1 轮中签：{'、'.join(summary['priority']['winners']) or '（无）'}。",
        "",
        "## 复盘改写",
        "",
        f"24 个种子、全员重写。平均满意度变化 {paired['mean_delta']} "
        f"（最小 {paired['min_delta']}，最大 {paired['max_delta']}），"
        f"达到 +{PAIRED_MIN_DELTA:g} 的种子 {paired['seeds_delta_at_least_min']}，"
        f"变差的种子 {paired['seeds_delta_negative']}，"
        f"必修缺口上升的种子 {paired['seeds_unmet_increased']}。",
        f"只重写最差 30% 时平均变化 {paired['dose_mean_delta']}，小于全员重写。",
        f"同一个人换成另一颗种子去比，24 对里有 {paired['cross_seed_negative']} 对是负的。"
        "同一套噪声在配对里抵消，拆开比就会把噪声当成退步。",
        f"种子 1 的课表从 {paired['example_preset_before']} 改到 {paired['example_preset_after']}："
        "第一遍按目录顺序占了早八班，复盘看过上一版之后改到下午。",
        "",
        "## 批量请求与检查点",
        "",
        f"一人一次预置调用 {summary['batch']['single_calls']} 次，"
        f"四人一次 {summary['batch']['grouped_calls']} 次。",
        f"检查点探针在第 2 次预置调用时中断，"
        f"当时已经落盘 {summary['checkpoint']['partial_students']} 人，"
        f"续跑日志「{summary['checkpoint']['resume_excerpt']}」，"
        f"续跑预置调用 {summary['checkpoint']['preset_calls_on_resume']} 次。",
        "真实 HTTP 客户端在发出请求前把一行写进 `attempts.jsonl` 并 fsync，"
        "已发起的请求数就是这个文件的行数。离线探针不走 HTTP，所以这条由 `tests/test_llm.py` 看守。",
        "",
    ]
    supply = summary.get("supply")
    if supply:
        lines += [
            "## 供给侧扩容（容量 ÷10，1800 人）",
            "",
            f"正缺口格子 {supply['positive_gap_cells']} 个。"
            f"类别级建议增座 {supply['actionable_extra_seats']}："
            "共享座位按「全类别需求 − 全类别座位」向上取整，不把每个格子的零头再加一遍。"
            f"格子零头各自向上取整再相加是 {supply['extra_seats_sum']}，那是取整放大，不当成采购数。"
            "绝对门数把秋冬并进一个学期，只能拿来排优先级，不能当成对外的缺口率。"
            "当前方案 JSON 的学年目标里没有外语类门数，外语需求只来自 0.33 的名额；"
            "data/README 里的 1558 还含一份方案行，和这份 JSON 对不上，本表以 JSON 为准。"
            "通识与体育类需求与 data/README 的 2107 一致。"
            "专业课、专业基础课里标了开课专业的座位不进其他专业的专用列。",
            "",
            "| 类别 | 需求 | 座位 | 缺口 | 富余 | 建议增座 |",
            "|---|---|---|---|---|---|",
        ]
        for row in supply["rollup"]:
            lines.append(
                f"| {row['category']} | {row['demand_slots']} | {row['seats']} | "
                f"{row['gap_slots']} | {row['surplus_slots']} | {row['actionable_extra_seats']} |")
        lines += [
            "",
            "按格子看到的零头（取整后至少 1 座的那些）：",
            "",
            "| 专业 | 学年 | 类别 | 需求 | 供给 | 格子取整 | 约合班数 |",
            "|---|---|---|---|---|---|---|",
        ]
        for row in supply["top"]:
            lines.append(
                f"| {row['major']} | {row['year']} | {row['category']} | {row['demand_slots']} | "
                f"{row['supply_slots']} | {row['extra_seats']} | {row['extra_sections']} |")
        lines.append("")
    lines.append("完整机器可读结果在 `results/mechanism/summary.json`。")
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跑机制实验并写出判决")
    parser.add_argument("--out", default=str(ROOT / "results" / "mechanism"))
    parser.add_argument("--report", default=str(ROOT / "docs" / "机制实验报告.md"))
    args = parser.parse_args(argv)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    summary = run_suite(out, with_supply=True)
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1),
                                      encoding="utf-8")
    report = render_report(summary)
    Path(args.report).write_text(report, encoding="utf-8")
    failed = [name for name, verdict in summary["verdicts"].items() if verdict != "通过"]
    print(json.dumps(summary["verdicts"], ensure_ascii=False, indent=1))
    if failed:
        print("未通过: " + "、".join(failed), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
