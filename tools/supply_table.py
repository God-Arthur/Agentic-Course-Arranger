"""按（专业 × 学年 × 类别）列出供给侧扩容建议。

需求用培养方案目标，加上 need_rules 里的体育 standing rule，以及外语名额的期望值
（总数用 round(人数 × language_share)，再按各格子人数做最大余数分配）。
不调用模型，也不使用某一颗种子抽中的那三分之一学生——表是结构性的。

专业课 / 专业基础课里标了开课专业的座位只算给该专业；没标专业的座位按该类别需求占比分。
一个格子的建议增座 = ceil(需求 − 专用座位 − 分到的共享座位)。别的专业富余的专用座位
不拿来抵这一个格子，所以各格子正缺口之和可以大于「全类别座位 − 全类别需求」。
"""
from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from planner.models import category_key, load_world  # noqa: E402


def _read(path: Path):
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)


def language_extras(counts: dict[tuple[str, str], int], share: float) -> dict[tuple[str, str], int]:
    """把 round(总人数 × share) 个外语名额按格子人数做最大余数分配。"""
    total = sum(counts.values())
    if share <= 0 or total <= 0:
        return {cell: 0 for cell in counts}
    quota = int(round(total * share))
    raw = {cell: quota * count / total for cell, count in counts.items()}
    assigned = {cell: int(math.floor(value)) for cell, value in raw.items()}
    left = quota - sum(assigned.values())
    order = sorted(counts, key=lambda cell: (-(raw[cell] - assigned[cell]), cell))
    for cell in order[:max(left, 0)]:
        assigned[cell] += 1
    return assigned


def expansion_table(world, allocation: list[tuple[str, str]]) -> dict:
    rules = world.need_rules or {}
    share = float(rules.get("language_share_per_student") or 0.0)
    pe = int(rules.get("pe_courses_per_student") or 0)
    counts = Counter((major, year) for major, year in allocation)
    extras = language_extras(dict(counts), share)

    demand: dict[tuple[str, str, str], float] = defaultdict(float)
    for (major, year), count in counts.items():
        if world.recommended_by_year:
            base = (world.recommended_by_year.get(major) or {}).get(year) or {}
        else:
            base = world.recommended.get(major) or {}
        for key, per_student in base.items():
            demand[(major, year, key)] += count * int(per_student)
        if pe:
            demand[(major, year, "通识与体育类")] += count * pe
        extra = extras.get((major, year), 0)
        if extra:
            demand[(major, year, "外语类")] += extra

    global_seats: Counter = Counter()
    dedicated: Counter = Counter()
    shared: Counter = Counter()
    capacities: dict[str, list[int]] = defaultdict(list)
    for section in world.sections.values():
        course = world.course_of_section(section.section_id)
        key = category_key(course.category)
        global_seats[key] += section.capacity
        capacities[key].append(section.capacity)
        if course.major:
            dedicated[(course.major, key)] += section.capacity
        else:
            shared[key] += section.capacity

    demand_by_key: Counter = Counter()
    for (_major, _year, key), amount in demand.items():
        demand_by_key[key] += amount

    rows = []
    for (major, year, key) in sorted(demand):
        amount = demand[(major, year, key)]
        own = int(dedicated.get((major, key), 0))
        pool = int(shared.get(key, 0))
        total_demand = demand_by_key[key]
        fair = pool * amount / total_demand if total_demand else 0.0
        supply = own + fair
        gap = max(0.0, amount - supply)
        extra_seats = math.ceil(gap - 1e-9) if gap > 1e-6 else 0
        median_cap = statistics.median(capacities[key]) if capacities.get(key) else 0
        extra_sections = math.ceil(extra_seats / median_cap) if extra_seats and median_cap else 0
        rows.append({
            "major": major,
            "year": year,
            "category": key,
            "students": int(counts[(major, year)]),
            "demand_slots": round(amount, 3),
            "dedicated_seats": own,
            "shared_seats_global": pool,
            "fair_shared_seats": round(fair, 3),
            "supply_slots": round(supply, 3),
            "gap_slots": round(gap, 3),
            "extra_seats": extra_seats,
            "median_section_capacity": median_cap,
            "extra_sections": extra_sections,
        })
    rows.sort(key=lambda row: (-row["extra_seats"], -row["gap_slots"], row["major"], row["year"], row["category"]))

    rollup = []
    for key in sorted(set(global_seats) | set(demand_by_key)):
        needed = float(demand_by_key[key])
        seats = int(global_seats[key])
        rollup.append({
            "category": key,
            "demand_slots": round(needed, 3),
            "seats": seats,
            "gap_slots": round(max(0.0, needed - seats), 3),
            "surplus_slots": round(max(0.0, seats - needed), 3),
        })
    for item in rollup:
        gap = item["gap_slots"]
        item["actionable_extra_seats"] = math.ceil(gap - 1e-9) if gap > 1e-6 else 0
    positive = [row for row in rows if row["extra_seats"] > 0]
    return {
        "students": len(allocation),
        "method": {
            "demand": "培养方案门数 × 格子人数；体育按 need_rules 每人另加；外语名额为 round(人数×share) 的最大余数分配",
            "supply": "标了开课专业的座位只计入该专业；其余座位按该类别需求占比分到格子",
            "extra_seats": "ceil(需求 − 专用座位 − 分到的共享座位)，富余专业的专用座位不跨专业抵扣",
        },
        "need_rules": rules,
        "category_rollup": rollup,
        "positive_gap_cells": len(positive),
        "extra_seats_sum": sum(row["extra_seats"] for row in rows),
        "actionable_extra_seats": sum(item["actionable_extra_seats"] for item in rollup),
        "rows": rows,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="产出（专业 × 学年 × 类别）扩容建议表")
    parser.add_argument("--courses", default=str(ROOT / "data" / "courses_k10.json"))
    parser.add_argument("--sizes", default=str(ROOT / "data" / "major_sizes.json"))
    parser.add_argument("--n", type=int, default=1800)
    parser.add_argument("--out", required=True)
    parser.add_argument("--top", type=int, default=15)
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT / "tools"))
    from run_experiment import allocate_population

    world = load_world(_read(Path(args.courses)))
    per_grade = {key: int(value) for key, value in _read(Path(args.sizes))["per_grade"].items()}
    allocation = allocate_population(args.n, world, per_grade)
    table = expansion_table(world, allocation)
    table["courses"] = args.courses
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    Path(args.out).write_text(json.dumps(table, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps({
        "students": table["students"],
        "positive_gap_cells": table["positive_gap_cells"],
        "extra_seats_sum": table["extra_seats_sum"],
        "rollup": table["category_rollup"],
        "top": table["rows"][:args.top],
    }, ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
