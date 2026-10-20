"""长跑实验入口：和演示走同一条纯 AI 通路，但不受演示页面 2-60 人的护栏限制，并且分阶段落检查点。

    .venv/bin/python tools/run_experiment.py --courses data/courses_k10.json \
        --scale-divisor 10 --iterations 1 --workers 6 --out results/micro_k10.json

检查点写在 <out>.ckpt/：needs.json、presets_<i>.json、iter<i>_round<k>.json、iter<i>_scores.json。
中断（含宿主机休眠）后用同一条命令重跑，就从最近的检查点继续，已经花掉配额的部分不再问一次。
检查点只省钱，不保证可复现：谁中签由模型决定，重跑同一个种子也会得到另一份名单。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import random
import sys
import threading
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from planner.ai_planner import ai_presets, ai_retune        # noqa: E402
from planner.behavior import (ai_allocator, ai_plan_builder,  # noqa: E402
                              ai_satisfaction)
from planner.env_config import resolve_llm                  # noqa: E402
from planner.llm import LLMClient                           # noqa: E402
from planner.models import NeedModel, World, load_world     # noqa: E402
from planner.pipeline import Evaluation, students_payload   # noqa: E402
from planner.selection import (RoundPlanEntry, SelectionResult,  # noqa: E402
                               SelectionState, run_rounds)
from planner.skills import parse_skill_llm, synthesize_students  # noqa: E402
from planner.timegrid import parse_slot, slot_key           # noqa: E402

YEARS = ("一", "二", "三")
# 志愿阶段的落盘粒度：一轮 1800 次要 75 分钟，段与段之间只多等一小撮慢尾巴。
PLAN_FLUSH_EVERY = 200
LAST_ROUND = 2


def write_json(path: Path, payload) -> None:
    """先写临时文件再改名：长跑中途被杀也不会留下半截 JSON。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    os.replace(tmp, path)


def read_json(path: Path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def preset_hash(preset_map: dict[str, list[str]]) -> str:
    """检查点必须对得上它所依据的那份预置课表，否则续跑就是在算错东西。"""
    text = json.dumps({sid: sorted(v) for sid, v in preset_map.items()},
                      sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def allocate_population(n: int, world: World, per_grade: dict[str, int]
                        ) -> list[tuple[str, str]]:
    """按各专业每届招生人数把 n 名学生铺到 (专业, 学年) 格子上（最大余数法，总数正好 n）。

    只铺培养方案里真有目标门数的格子；格子权重取该专业一届的招生数，
    于是文理工农医各专业的比例跟招生计划一致，而不是均匀摊到 160 个专业上。
    """
    cells: list[tuple[str, str, int]] = []
    for major in sorted(world.majors):
        by_year = world.recommended_by_year or {}
        years = sorted(y for y in (by_year.get(major) or {})
                       if y in YEARS and (by_year.get(major) or {})[y])
        if not years:
            if not (world.recommended.get(major) or {}):
                continue
            years = ["一"]
        weight = int(per_grade.get(major, 1)) or 1
        cells.extend((major, year, weight) for year in years)
    if not cells:
        raise SystemExit("培养方案里没有任何 (专业, 学年) 目标格子，无法分配人口")
    total_weight = sum(weight for _, _, weight in cells)
    raw = [n * weight / total_weight for _, _, weight in cells]
    counts = [int(math.floor(value)) for value in raw]
    left = n - sum(counts)
    for index in sorted(range(len(cells)), key=lambda i: (-(raw[i] - counts[i]), i))[:max(left, 0)]:
        counts[index] += 1
    allocation: list[tuple[str, str]] = []
    for (major, year, _), count in zip(cells, counts):
        allocation.extend([(major, year)] * count)
    return allocation


def run_tasks(items, work, workers: int, on_result=None, flush_every: int = 100, log: str = ""):
    """并发跑任务并保持输入顺序；每凑满 flush_every 个结果回调一次 on_result(已完成字典)。

    任一任务抛错就原样上抛（LLM 失败是致命的，长跑不兜底）。workers<=1 时顺序执行。
    """
    pending = list(items)
    results: dict = {}
    started = time.perf_counter()
    if not pending:
        return results
    if workers <= 1:
        for item in pending:
            results[item] = work(item)
            if on_result is not None:
                on_result(results)
        return results
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(work, item): item for item in pending}
        for future in as_completed(futures):
            item = futures[future]
            results[item] = future.result()
            done += 1
            if on_result is not None and (done % flush_every == 0 or done == len(futures)):
                on_result(results)
            if log and (done % max(25, workers) == 0 or done == len(futures)):
                print(f"{log} {done}/{len(futures)} 用时 {time.perf_counter() - started:.0f}s "
                      f"调用 {done}", file=sys.stderr, flush=True)
    return {item: results[item] for item in pending}


def restore_need(sid: str, row: dict) -> NeedModel:
    return NeedModel(student_id=sid, required=dict(row.get("required") or {}),
                     elective=dict(row.get("elective") or {}),
                     avoid_slots=tuple(parse_slot(s) for s in row.get("avoid_slots") or []),
                     notes=str(row.get("notes") or ""), source=str(row.get("source") or "llm"))


def need_row(need: NeedModel) -> dict:
    return {"required": need.required, "elective": need.elective,
            "avoid_slots": [slot_key(s) for s in need.avoid_slots],
            "notes": need.notes, "source": need.source}


def parse_all_needs(skill_texts: dict[str, str], client: LLMClient, workers: int,
                    ckpt_dir: Path, stats: dict) -> dict[str, NeedModel]:
    path = ckpt_dir / "needs.json"
    rows: dict[str, dict] = read_json(path) if path.exists() else {}
    rows = {sid: row for sid, row in rows.items() if sid in skill_texts}
    pending = [sid for sid in skill_texts if sid not in rows]
    if pending:
        got = run_tasks(pending,
                        lambda sid: need_row(parse_skill_llm(skill_texts[sid], client, stats)),
                        workers, lambda accum: write_json(path, {**rows, **accum}),
                        flush_every=200, log="[needs]")
        rows.update(got)
        write_json(path, rows)
    missing = [sid for sid in skill_texts if sid not in rows]
    if missing:
        raise SystemExit(f"{len(missing)} 名学生没有需求解析结果（检查点不完整）")
    return {sid: restore_need(sid, rows[sid]) for sid in skill_texts}


def _snapshot_of(data: dict) -> dict:
    return {"assignments": {sid: set(sections) for sid, sections in data["assignments"].items()},
            "remaining": dict(data["remaining"])}


def entry_row(entry: RoundPlanEntry) -> dict:
    return {"drops": list(entry.drops),
            "prefs": {str(sid): int(rank) for sid, rank in entry.prefs.items()}}


def restore_entry(row: dict) -> RoundPlanEntry:
    return RoundPlanEntry(drops=tuple(str(sid) for sid in row.get("drops") or []),
                          prefs={str(sid): int(rank) for sid, rank in
                                 (row.get("prefs") or {}).items()})


def view_fingerprint(view: dict) -> str:
    """轮初状态指纹：志愿检查点只对同一份轮初快照有效，换了预置就必须作废。"""
    payload = json.dumps({"a": {sid: sorted(v) for sid, v in view["assignments"].items()},
                          "r": {sid: int(left) for sid, left in view["remaining"].items()}},
                         sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def simulate_rounds(world: World, preset_map: dict[str, list[str]], client: LLMClient,
                    stats: dict, skill_texts: dict[str, str], needs: dict[str, NeedModel],
                    workers: int, ckpt_dir: Path, iteration: int) -> SelectionResult:
    """两轮模拟，每轮结束落一次盘；已有检查点的轮次不再重新花钱。

    志愿阶段本身也按 PLAN_FLUSH_EVERY 人一段落盘——一轮要 1800 次调用、75 分钟，
    以前只有整轮跑完才有检查点，筛选器一断线这 1800 次就白付了。
    """
    def plans_path(round_index: int) -> Path:
        return ckpt_dir / f"iter{iteration}_round{round_index}_plans.json"

    def resume_for(round_index: int, view: dict) -> dict[str, RoundPlanEntry]:
        path = plans_path(round_index)
        if not path.exists():
            return {}
        stored = read_json(path)
        if stored.get("view_fp") != view_fingerprint(view):
            print(f"[ckpt] {path.name} 属于另一份轮初状态，作废重跑", file=sys.stderr, flush=True)
            return {}
        got = {sid: restore_entry(row) for sid, row in stored["plans"].items()}
        print(f"[resume] 第 {iteration} 轮第 {round_index} 轮志愿复用 {len(got)} 人（不调模型）",
              file=sys.stderr, flush=True)
        return got

    def on_batch(round_index: int, view: dict, accum: dict) -> None:
        write_json(plans_path(round_index),
                   {"view_fp": view_fingerprint(view),
                    "plans": {sid: entry_row(entry) for sid, entry in accum.items()}})
        print(f"[plans] iter{iteration} r{round_index} 已填 {len(accum)}/{len(needs)} "
              f"累计调用 {client.calls}", file=sys.stderr, flush=True)

    builder = ai_plan_builder(world, skill_texts, needs, client, stats, workers,
                              resume_for=resume_for, on_batch=on_batch,
                              batch=PLAN_FLUSH_EVERY)
    allocator = ai_allocator(world, client, stats)
    signature = preset_hash(preset_map)
    files = {index: ckpt_dir / f"iter{iteration}_round{index}.json" for index in (1, 2)}
    start_round, data = 1, None
    for index in (2, 1):
        if files[index].exists():
            stored = read_json(files[index])
            if stored.get("preset_hash") != signature:
                raise SystemExit(f"{files[index].name} 属于另一份预置课表，"
                                 f"请删除 {ckpt_dir} 里第 {iteration} 轮的检查点后重跑")
            start_round, data = index, stored
            break
    if data is None:
        state = SelectionState(world, preset_map)
        events: list[dict] = []
    else:
        view = _snapshot_of(data)
        state = SelectionState.from_view(world, view)
        events = list(data.get("events") or [])
        start_round += 1
        print(f"[resume] 第 {iteration} 轮跑到第 {start_round - 1} 轮已复用"
              f"（{len(events)} 条事件）", file=sys.stderr)
    if start_round > LAST_ROUND:
        return SelectionResult(state.assignments, state.remaining, events)

    def record(round_index: int, view: dict) -> None:
        write_json(files[round_index], {
            "preset_hash": signature,
            "assignments": {sid: sorted(sections) for sid, sections in view["assignments"].items()},
            "remaining": view["remaining"],
            "events": events,
        })
        print(f"[ckpt] iter{iteration} round{round_index} 落盘，累计调用 "
              f"{_call_count(client)}", file=sys.stderr, flush=True)

    return run_rounds(world, state, builder, allocator, stats, start_round, LAST_ROUND,
                      on_round_end=record, events=events)


def _call_count(client: LLMClient) -> int:
    return client.calls


def score_all(world: World, needs: dict[str, NeedModel], skill_texts: dict[str, str],
              result: SelectionResult, client: LLMClient, stats: dict, workers: int,
              ckpt_dir: Path, iteration: int) -> tuple[dict[str, int], dict[str, str]]:
    path = ckpt_dir / f"iter{iteration}_scores.json"
    stored = read_json(path) if path.exists() else {"scores": {}, "reasons": {}}
    scores = {sid: int(value) for sid, value in stored["scores"].items() if sid in needs}
    reasons = {sid: str(value) for sid, value in stored.get("reasons", {}).items()
               if sid in needs}
    pending = [sid for sid in needs if sid not in scores]

    def work(sid: str) -> tuple[str, int, str]:
        score, reason = ai_satisfaction(world, skill_texts[sid], needs[sid],
                                       result.assignments[sid], client, stats)
        return sid, score, reason

    def flush(accum: dict) -> None:
        merged_scores, merged_reasons = dict(scores), dict(reasons)
        for sid, value, reason in accum.values():
            merged_scores[sid] = value
            merged_reasons[sid] = reason
        write_json(path, {"scores": merged_scores, "reasons": merged_reasons})

    if pending:
        got = run_tasks(pending, work, workers, flush, flush_every=200,
                        log=f"[satisfy i{iteration}]")
        for sid, value, reason in got.values():
            scores[sid] = value
            reasons[sid] = reason
        flush({})
    return scores, reasons


def evaluate(world: World, needs: dict[str, NeedModel], skill_texts: dict[str, str],
             client: LLMClient, stats: dict, workers: int, ckpt_dir: Path, iteration: int):
    """返回 evaluate(preset_map)：先复用/补齐两轮模拟，再补满意度。"""

    def run(preset_map: dict[str, list[str]]) -> Evaluation:
        result = simulate_rounds(world, preset_map, client, stats, skill_texts, needs,
                                 workers, ckpt_dir, iteration)
        scores, reasons = score_all(world, needs, skill_texts, result, client, stats,
                                    workers, ckpt_dir, iteration)
        kept = {sid: sorted(set(result.assignments[sid]) & set(preset_map.get(sid, [])))
                for sid in needs}
        if len(scores) != len(needs):
            raise SystemExit(f"满意度打分只有 {len(scores)}/{len(needs)} 人，检查点不完整")
        return Evaluation(preset_map, result, scores, reasons, kept)
    return run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="纯 AI 选课长跑实验")
    parser.add_argument("--courses", default="data/courses_k10.json")
    parser.add_argument("--sizes", default="data/major_sizes.json")
    parser.add_argument("--scale-divisor", type=int, default=10,
                        help="人数 = 前三届招生计划总数 ÷ 此倍数（与容量缩小倍数保持一致）")
    parser.add_argument("--n", type=int, default=None, help="直接指定学生数，覆盖 --scale-divisor")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--iterations", type=int, default=1, help="复盘改写轮数 R")
    parser.add_argument("--refine-frac", type=float, default=0.3)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--min-interval", type=float, default=None,
                        help="全局最小请求间隔秒数；不给就用 .env 推出的值")
    parser.add_argument("--max-per-minute", type=float, default=0,
                        help="每分钟请求上限（0=不启用窗口，只按最小间隔限流）")
    parser.add_argument("--timeout", type=float, default=600.0,
                        help="单次请求读超时秒数；筛选器一块 300 行的回复可能要几分钟")
    parser.add_argument("--out", required=True)
    parser.add_argument("--dry-run", action="store_true",
                        help="只合成人口与 .skill，不调用任何 LLM，用来核对口径")
    args = parser.parse_args(argv)

    data = read_json(Path(args.courses))
    world = load_world(data)
    per_grade = {k: int(v) for k, v in read_json(Path(args.sizes))["per_grade"].items()}
    three_years = sum(per_grade.values()) * 3
    n = args.n or max(len(world.majors), round(three_years / args.scale_divisor))
    allocation = allocate_population(n, world, per_grade)
    world.students, skill_texts = synthesize_students(len(allocation), world,
                                                      random.Random(args.seed), allocation)
    by_year = Counter(year for _, year in allocation)
    by_major = Counter(major for major, _ in allocation)
    if args.dry_run:
        print(json.dumps({
            "students": len(allocation), "by_year": dict(by_year),
            "majors": len(by_major), "largest_majors": by_major.most_common(10),
            "smallest_majors": by_major.most_common()[-5:],
            "need_rules": world.need_rules,
            "sample_skill": skill_texts[next(iter(skill_texts))],
        }, ensure_ascii=False, indent=1))
        return 0

    ckpt_dir = Path(args.out + ".ckpt")
    ckpt_dir.mkdir(parents=True, exist_ok=True)
    config, _public = resolve_llm({})
    if config is None:
        print("ERROR: 需要完整 LLM 配置（项目根 .env）", file=sys.stderr)
        return 2
    min_interval = (args.min_interval if args.min_interval is not None
                    else float(config.get("min_interval", 3.0)))
    client = LLMClient(base_url=config["base_url"], model=config["model"],
                       api_key=config["api_key"], min_interval=min_interval,
                       max_per_minute=args.max_per_minute, timeout=args.timeout)
    stats: dict = {"calls": 0, "failures": 0, "sanitized": 0}
    started = time.perf_counter()

    needs = parse_all_needs(skill_texts, client, args.workers, ckpt_dir, stats)
    print(f"[phase] 需求 {len(needs)} 人就绪，累计调用 {client.calls}", file=sys.stderr, flush=True)
    print(f"[perf] {json.dumps(client.perf(), ensure_ascii=False)}", file=sys.stderr, flush=True)

    history: list[dict] = []
    timeline: list[dict] = []
    best: dict[str, list[str]] | None = None
    best_eval: Evaluation | None = None
    previous_map: dict[str, list[str]] | None = None
    previous_eval: Evaluation | None = None

    def build_payload(status: str) -> dict:
        """每轮结束就落一次盘，长跑中途被回收也有一份能审的结果。"""
        stats["calls"] = client.calls
        stats["failures"] = client.failures + stats.get("failures", 0)
        stats.update(client.perf())
        events = best_eval.result.events
        return {
            "_meta": {
                "kind": "纯 AI 长跑实验：LLM 解析需求 → LLM 排预置 → 两轮模拟（LLM 填志愿 + LLM 筛中签）"
                        "→ LLM 满意度 → LLM 复盘改写，代码只做事实校验，无规则兜底、无本地抽签",
                "courses": str(args.courses),
                "courses_meta": data.get("_meta", {}),
                "generated_at": datetime.now().isoformat(timespec="seconds"),
                "status": status,
                "seed": args.seed,
                "scale": {"scale_divisor": args.scale_divisor, "students": len(needs),
                          "by_year": dict(by_year), "majors": len(by_major),
                          "three_year_population_source": three_years},
                "need_rules": world.need_rules,
                "iterations": args.iterations,
                "iterations_completed": len(history),
                "refine_frac": args.refine_frac,
                "workers": args.workers,
                "min_interval_s": min_interval,
                "max_per_minute": args.max_per_minute,
                "reproducible": False,
                "note": "中签名单由模型决定；同一份输入重跑会得到另一份名单，"
                        "因此轮间分差不能当成改进的证据。",
            },
            "summary": {**best_eval.summary(),
                        "students": len(needs),
                        "majors": len(by_major),
                        "unmet_required_students": best_eval.unmet_required_students(world, needs),
                        "unmet_required_slots": best_eval.unmet_required_slots(world, needs),
                        "grant_events": sum(1 for e in events if e.get("phase") == "中签"),
                        "rejected_grants": sum(1 for e in events if e.get("phase") == "拒发"),
                        "drops": sum(1 for e in events if e.get("phase") == "退课"),
                        "sections_used": sum(1 for sid, left in best_eval.result.remaining.items()
                                             if left < world.sections[sid].capacity)},
            "history": history,
            "timeline": timeline,
            "llm_stats": stats,
            "wall_clock_s": round(time.perf_counter() - started, 1),
            "students": students_payload(world, needs, skill_texts, best, best_eval),
        }

    for iteration in range(args.iterations + 1):
        presets_path = ckpt_dir / f"presets_{iteration}.json"
        phase_started = time.perf_counter()
        print(f"[phase] iter {iteration} 预置开始（{len(needs)} 人，workers={args.workers}，"
              f"累计调用 {client.calls}）", file=sys.stderr, flush=True)
        if presets_path.exists():
            preset_map = {sid: list(v) for sid, v in read_json(presets_path).items()}
        elif iteration == 0:
            preset_map = ai_presets(world, needs, skill_texts, client, stats, args.workers)
            write_json(presets_path, preset_map)
        else:
            if previous_eval is None or previous_map is None:
                raise SystemExit(f"缺少第 {iteration - 1} 轮的评估结果，无法复盘改写")
            preset_map = ai_retune(world, needs, skill_texts, client, stats, previous_map,
                                   previous_eval.scores, previous_eval.reasons,
                                   previous_eval.result.assignments, args.refine_frac,
                                   args.workers)
            write_json(presets_path, preset_map)
        evaluation = evaluate(world, needs, skill_texts, client, stats, args.workers,
                              ckpt_dir, iteration)(preset_map)
        previous_map, previous_eval = preset_map, evaluation
        history.append(evaluation.summary())
        timeline.append({"iteration": iteration,
                         "phase_s": round(time.perf_counter() - phase_started, 1),
                         "calls_so_far": client.calls,
                         "unmet_required_slots": evaluation.unmet_required_slots(world, needs),
                         "summary": evaluation.summary()})
        print(f"[iter {iteration}] {json.dumps(evaluation.summary(), ensure_ascii=False)} "
              f"累计调用 {client.calls}", file=sys.stderr, flush=True)
        if best_eval is None or evaluation.mean() > best_eval.mean():
            best, best_eval = dict(preset_map), evaluation
        write_json(Path(args.out), build_payload(f"第 {iteration + 1} 轮已完成，仍在运行"))

    payload = build_payload("完成")
    write_json(Path(args.out), payload)
    print(json.dumps(payload["summary"], ensure_ascii=False))
    print(f"written: {args.out}（检查点在 {ckpt_dir}/）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
