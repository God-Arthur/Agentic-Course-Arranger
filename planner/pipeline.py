"""端到端演示流水线（AI 唯一通路）：读取课程数据 → 合成学生 → LLM 解析需求 → LLM 排预置课表 → 两轮模拟（LLM 填志愿 + LLM 筛中签）→ LLM 满意度 → LLM 复盘改写 → 重复。"""
from __future__ import annotations

import json
import random
import time
from collections import Counter
from dataclasses import dataclass, field

from .behavior import ai_allocator, ai_plan_builder, ai_satisfaction
from .llm import LLMClient, parallel_map
from .models import NeedModel, World, category_key, load_world
from .optimize import optimize
from .selection import SelectionResult, run_two_rounds
from .skills import parse_skill_llm, synthesize_students
from .timegrid import slot_key, slot_label


class DemoError(RuntimeError):
    """演示输入问题（缺课程数据、参数非法等），面向用户的错误信息。"""


@dataclass
class Evaluation:
    preset: dict[str, list[str]]
    result: SelectionResult
    scores: dict[str, int]
    reasons: dict[str, str] = field(default_factory=dict)
    kept: dict[str, list[str]] = field(default_factory=dict)

    def mean(self) -> float:
        return sum(self.scores.values()) / len(self.scores)

    def summary(self) -> dict:
        preset_total = sum(len(v) for v in self.preset.values())
        kept_total = sum(len(self.kept.get(sid, [])) for sid in self.preset)
        unmet = 0
        return {
            "mean_satisfaction": round(self.mean(), 1),
            "preset_keep_pct": round(kept_total / preset_total * 100, 1) if preset_total else None,
            "final_assigned_total": sum(len(v) for v in self.result.assignments.values()),
        }

    def unmet_required_students(self, world: World, needs: dict[str, NeedModel]) -> int:
        count = 0
        for student_id, need in needs.items():
            assigned = self.result.assignments[student_id]
            per_category = Counter(category_key(world.course_of_section(s).category)
                                   for s in assigned)
            if any(per_category.get(k, 0) < want for k, want in need.required.items()):
                count += 1
        return count

    def unmet_required_slots(self, world: World, needs: dict[str, NeedModel]) -> int:
        """没补上的必修门次总数。人数指标在供<求时恒等于全员，只有这个量级能区分轮次。"""
        total = 0
        for student_id, need in needs.items():
            per_category = Counter(category_key(world.course_of_section(s).category)
                                   for s in self.result.assignments[student_id])
            total += sum(max(0, want - per_category.get(k, 0)) for k, want in need.required.items())
        return total


def make_evaluator(world: World, needs: dict[str, NeedModel], skill_texts: dict[str, str],
                   client: LLMClient, llm_stats: dict, workers: int = 1):
    """一轮评估：两轮模拟（AI 填志愿 + AI 筛中签）→ 逐生满意度。演示与长跑共用这套口径。"""
    def evaluate(preset_map: dict[str, list[str]]) -> Evaluation:
        builder = ai_plan_builder(world, skill_texts, needs, client, llm_stats, workers)
        allocator = ai_allocator(world, client, llm_stats)
        result = run_two_rounds(world, preset_map, builder, allocator, llm_stats)
        ids = list(needs)

        def score(student_id: str) -> tuple[str, int, str]:
            assigned = result.assignments[student_id]
            value, reason = ai_satisfaction(world, skill_texts[student_id], needs[student_id],
                                            assigned, client, llm_stats)
            return student_id, value, reason

        scored = parallel_map(ids, score, workers)
        scores = {sid: value for sid, value, _ in scored}
        reasons = {sid: reason for sid, _, reason in scored}
        kept = {sid: sorted(set(result.assignments[sid]) & set(preset_map.get(sid, [])))
                for sid in ids}
        return Evaluation(preset_map, result, scores, reasons, kept)
    return evaluate


def students_payload(world: World, needs: dict[str, NeedModel], skill_texts: dict[str, str],
                     best: dict[str, list[str]], best_eval: Evaluation) -> list[dict]:
    rows = []
    for student_id in world.students:
        student = world.students[student_id]
        need = needs[student_id]
        rows.append({
            "student_id": student_id,
            "major": student.major,
            "year": student.year,
            "graduating": student.graduating,
            "transfer": student.transfer,
            "campus_move": student.campus_move,
            "skill": skill_texts[student_id],
            "need": {
                "required": need.required,
                "elective": need.elective,
                "avoid_slots": [slot_label(s) for s in need.avoid_slots],
                "notes": need.notes,
                "source": need.source,
            },
            "preset": [_section_payload(world, s) for s in best.get(student_id, [])],
            "preset_kept": [_section_payload(world, s) for s in best_eval.kept.get(student_id, [])],
            "final": [_section_payload(world, s)
                      for s in sorted(best_eval.result.assignments[student_id])],
            "satisfaction": best_eval.scores[student_id],
            "reason": best_eval.reasons.get(student_id, ""),
        })
    return rows


def run_pipeline(n_students: int, courses_path: str,
                 llm: dict | None = None, seed: int = 7,
                 restarts: int | None = None) -> dict:
    started = time.perf_counter()
    if not 2 <= int(n_students) <= 60:
        raise DemoError("学生数量必须在 2-60 之间")
    try:
        with open(courses_path, encoding="utf-8") as handle:
            data = json.load(handle)
    except FileNotFoundError as exc:
        raise DemoError("缺少课程数据：请把课程/班次/培养方案数据放到 data/courses.json"
                        "（格式见 data/README.md）。") from exc
    except json.JSONDecodeError as exc:
        raise DemoError(f"courses.json 不是合法 JSON：{exc}") from exc
    try:
        world = load_world(data)
    except ValueError as exc:
        raise DemoError(f"课程数据不合规：{exc}") from exc

    world.students, skill_texts = synthesize_students(int(n_students), world, random.Random(seed))
    llm_stats: dict = {"calls": 0, "failures": 0, "sanitized": 0}

    config = llm or {}
    if not (config.get("base_url") and config.get("model") and config.get("api_key")):
        raise DemoError("需要完整 LLM 配置（Base URL、模型名称、API Key）："
                        "可在 redo/.env 提供，或在页面填写")
    client = LLMClient(base_url=str(config["base_url"]), model=str(config["model"]),
                       api_key=str(config["api_key"]),
                       min_interval=float(config.get("min_interval", 3.0)))
    needs = {sid: parse_skill_llm(skill_texts[sid], client, llm_stats)
             for sid in world.students}
    restarts = 1 if restarts is None else restarts
    evaluate = make_evaluator(world, needs, skill_texts, client, llm_stats)

    best, best_eval, history = optimize(world, needs, skill_texts, client, llm_stats,
                                        evaluate, restarts)
    llm_stats["calls"] = client.calls
    llm_stats["failures"] = client.failures + llm_stats.get("failures", 0)

    rows = students_payload(world, needs, skill_texts, best, best_eval)

    elapsed = time.perf_counter() - started
    summary = best_eval.summary()
    summary.update({
        "students": len(world.students),
        "unmet_required_students": best_eval.unmet_required_students(world, needs),
        "restarts": restarts,
        "elapsed_s": round(elapsed, 1),
        "seed": seed,
    })
    return {"summary": summary, "history": history, "llm_stats": llm_stats,
            "students": rows,
            "categories": world.category_keys}


def _section_payload(world: World, section_id: str) -> dict:
    section = world.sections[section_id]
    course = world.course_of_section(section_id)
    return {
        "section_id": section_id,
        "course_id": course.course_id,
        "course_name": course.name,
        "category": course.category,
        "category_key": category_key(course.category),
        "slots": [slot_key(s) for s in section.slots],
    }
