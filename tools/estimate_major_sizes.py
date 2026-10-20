"""从 2026 年浙江招生计划 + 2026 级培养方案推测各专业的年级人数，输出 data/major_sizes.json。

口径（全部写进产物 _meta，供页面与报告引用）：
- 专业清单来自 data/courses.json 的 plan.majors（2026 级培养方案里真正有课程行的 160 个专业）。
- 人数信号只有浙江：zhejiang_yiduan_2026.csv（普通类第一段平行投档）+ zhejiang_sanyi_tixi_2026.csv（三位一体）。
  两张表都是「招生计划数」，不是实际录取人数，且只覆盖浙江一省。
- 能按专业名精确落到某个专业的行 → 直接用计划数。精确名优先：一段表的「机械工程」就是普通班，
  不会记到 机械工程（卓越工程师班）头上；只有精确名落空、去掉括号后**唯一**命中某个专业方案才算命中。
  工科/理科/人文/社会科学试验班这类「大类行」，以及撞车的行，我们没有官方的大类→专业分流构成，
  不强行拆分，全部并入「未直接命中专业」等分。
- 浙江合计 3092 人，按发起人给的 6000/年级 整体放大到全国口径，假设浙江的专业结构与全国一致。

用法：
  .venv/bin/python tools/estimate_major_sizes.py --per-grade 6000 --out data/major_sizes.json
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
_PAREN = re.compile(r"[（(].*?[）)]")


def norm(name: str) -> str:
    return _PAREN.sub("", name).replace(" ", "").strip()


def load_rows(enroll_dir: Path) -> list[dict]:
    rows: list[dict] = []
    for filename in ("zhejiang_yiduan_2026.csv", "zhejiang_sanyi_tixi_2026.csv"):
        path = enroll_dir / filename
        if not path.exists():
            raise SystemExit(f"缺少招生计划文件：{path}")
        for row in csv.DictReader(path.open(encoding="utf-8-sig")):
            plan = (row.get("计划数") or "").strip()
            if not plan.isdigit():
                raise SystemExit(f"{filename} 计划数非整数：{plan!r}（{row.get('专业名称')}）")
            rows.append({"major": (row["专业名称"] or "").strip(), "plan": int(plan),
                         "source": row.get("来源") or filename})
    return rows


def allocate(majors: list[str], rows: list[dict]) -> tuple[dict[str, float], dict]:
    """返回 (每专业原始计划数, 归因统计)。大类行进 pool，不猜构成。"""
    by_name = {m: m for m in majors}
    norm_index: dict[str, list[str]] = {}
    for m in majors:
        norm_index.setdefault(norm(m), []).append(m)
    direct: Counter = Counter()
    pool = 0
    pool_rows = 0
    for row in rows:
        if row["major"] in by_name:
            direct[row["major"]] += row["plan"]
            continue
        hits = norm_index.get(norm(row["major"]), [])
        if len(hits) == 1:
            direct[hits[0]] += row["plan"]
        else:
            pool += row["plan"]
            pool_rows += 1
    undecided = [m for m in majors if m not in direct]
    share = pool / len(undecided) if undecided else 0.0
    sizes = {m: float(direct.get(m, share)) for m in majors}
    stats = {"direct_majors": len(direct), "direct_plan": sum(direct.values()),
             "group_rows": pool_rows, "group_plan": pool,
             "undecided_majors": len(undecided), "pool_share_per_major": round(share, 2)}
    return sizes, stats


def scale_to(sizes: dict[str, float], total: int) -> dict[str, int]:
    """最大余数法取整，保证合计正好等于 total。"""
    raw_sum = sum(sizes.values())
    scaled = {m: v / raw_sum * total for m, v in sizes.items()}
    base = {m: int(v) for m, v in scaled.items()}
    left = total - sum(base.values())
    for m in sorted(scaled, key=lambda k: (-(scaled[k] - base[k]), k))[:left]:
        base[m] += 1
    return base


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--courses", default=str(ROOT / "data" / "courses.json"))
    parser.add_argument("--enroll", default="/home/redaunt/ai_course_planner_mvp/zju_catalog/enroll_2026")
    parser.add_argument("--per-grade", type=int, default=6000, help="一个年级的在校生数（发起人给定）")
    parser.add_argument("--grades", type=int, default=4, help="在校本科年级数")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    majors = sorted(json.load(open(args.courses, encoding="utf-8"))["plan"]["majors"])
    rows = load_rows(Path(args.enroll))
    sizes, stats = allocate(majors, rows)
    per_grade = scale_to(sizes, args.per_grade)
    data = {
        "_meta": {
            "synthetic": False,
            "method": "2026 年浙江「专业级招生计划」直接命中 + 大类计划按未命中专业等分补齐，"
                      "再整体放大到全国年级规模",
            "generated_from": [f"{args.enroll}/zhejiang_yiduan_2026.csv",
                               f"{args.enroll}/zhejiang_sanyi_tixi_2026.csv",
                               "data/courses.json plan.majors"],
            "grade_year": 2026,
            "per_grade_total": args.per_grade,
            "all_grades_total": args.per_grade * args.grades,
            "zhejiang_plan_total": sum(int(r["plan"]) for r in rows),
            "national_scale_factor": round(args.per_grade / sum(int(r["plan"]) for r in rows), 3),
            "assumptions": [
                "招生计划数≠实际录取人数；分省分专业只有浙江，放大到全国假设专业结构与浙江一致",
                "两张表口径不同（一段/三位一体），此处按发起人要求合并作为一个相对规模信号",
                "专业名精确命中优先；去括号后唯一命中才算数，撞车（如 自动化 对应多个方案专业）不猜",
                "大类行（工科/理科/人文/社会科学试验班等）没有官方分流构成，其计划数在"
                "未直接命中的专业之间等分",
                "四个年级假设同构（只爬到了 2026 级方案与 2026 招生计划）",
            ],
            **stats,
        },
        "per_grade": per_grade,
        "all_grades": {m: v * args.grades for m, v in per_grade.items()},
    }
    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        Path(args.out).write_text(text, encoding="utf-8")
    else:
        sys.stdout.write(text)
    ranked = sorted(per_grade.items(), key=lambda t: (-t[1], t[0]))
    print(f"专业数={len(per_grade)} 年级合计={sum(per_grade.values())} "
          f"全校合计={sum(data['all_grades'].values())}", file=sys.stderr)
    print(f"直接命中 {stats['direct_majors']} 个专业 / {stats['direct_plan']} 个计划；"
          f"大类 {stats['group_rows']} 行 / {stats['group_plan']} 个计划等分给 "
          f"{stats['undecided_majors']} 个未命中专业（每专业 {stats['pool_share_per_major']}）",
          file=sys.stderr)
    print("最大 8:", ranked[:8], file=sys.stderr)
    print("最小 8:", ranked[-8:], file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
