"""把 zju_catalog 抓取的真实开课目录与培养方案转成 planner 的 courses.json。

数据来源（只读）：
  <catalog>/2026-2027_1/courses.json        本学期开课目录（课程 + 教学班 + 真实上课时间）
  <catalog>/pyfa_2026/plan_courses.csv      2026 级培养方案结构化结果（类别路径 + 建议学年学期）
  <catalog>/2026-2027_1/classes_capacity.csv 教学班真实容量（取「最大容量」列；「余量」是还能再选的名额，不是容量）

分类口径：优先用培养方案 module_path 的官方类别；方案没覆盖到的课程再退回开课目录分类。
容量：用 --capacity 提供真实容量；文件里没有的教学班按 --capacity-default 填充，命中量写进 _meta.capacity_source。
需求口径（发起人 2026-10-20 按方案原文更正）：--language-per-student 把外语类从"每学期必修门数"改成按比例学分池、
--pe-per-student 补上方案里不列课名的体育 standing rule、--exclude-grade 四 排除大四、
--year-indexes 一,二,三 输出 plan.recommended_by_year（把二/三学年行当作当前大二/大三同学期的代理）、
--capacity-divisor 微缩实验时同比例缩容量（需求侧必须同步缩，否则稀缺结构消失）。
"""
from __future__ import annotations

import argparse
import csv
import datetime
import json
import os
import re
import sys
from collections import Counter, defaultdict

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))

from planner.models import CATEGORIES  # noqa: E402
from planner.timegrid import DAYS as GRID_DAYS  # noqa: E402

# 培养方案 module_path → planner 九类（按先后顺序匹配，越具体越靠前）
PLAN_MAP = (
    ("思政类", "思政类"),
    ("军体类", None),          # 军体类要再分：体育项目→体育课，军训/军事理论→思政类
    ("外语类", "外语类"),
    ("计算机类", "计算机通识类"),
    ("自然科学通识类", "自然科学通识类"),
    ("通识核心", "通识核心课"),
    ("通识选修", "通识选修课"),
    ("专业基础", "专业基础课"),
    ("专业必修", "专业课"),
    ("专业选修", "专业课"),
    ("专业方向", "专业课"),
    ("专业模块", "专业课"),
    ("专业进阶", "专业课"),
    ("实践教学", "专业课"),
    ("毕业论文", "专业课"),
    ("专业课程", "专业课"),
)

# 开课目录分类 → planner 九类
CATALOG_MAP = (
    ("通识必修课程|思政军体类", None),      # 同样再分
    ("通识必修课程|外语类", "外语类"),
    ("通识必修课程|计算机类", "计算机通识类"),
    ("通识必修课程|自然科学通识类", "自然科学通识类"),
    ("通识选修课程|通识核心课程", "通识核心课"),
    ("通识选修课程|", "通识选修课"),
    ("认定型课程|", "通识选修课"),
    ("体育课程", "体育课"),
    ("专业基础课程", "专业基础课"),
    ("专业课程|", "专业课"),
    ("本类(专业)选课", "专业课"),
    ("竺院课程", "专业课"),
    ("国际化课程", "通识选修课"),
)

# 汇总位（一门课同时挂在“全部课程”等父类下），不能直接当类别用
ROLLUP = {"通识必修课程|全部课程", "通识选修课程|全部课程", "认定型课程|全部课程"}

_PE_HINT = re.compile(r"体育|运动|体测|武术|游泳|球|操|拳|击剑|攀岩|飞盘|健美|定向")

_DAY = {"一": "Mon", "二": "Tue", "三": "Wed", "四": "Thu", "五": "Fri", "六": "Sat", "日": "Sun", "天": "Sun"}
_SJ = re.compile(r"周([一二三四五六日天])第([\d,]+)节")
_BRACE = re.compile(r"\{[^}]*\}")


def is_pe(name: str, course_id: str) -> bool:
    return bool(_PE_HINT.search(name or "")) or (course_id or "").startswith("481E")


def _find(table, text: str) -> tuple[str | None, bool]:
    """返回 (映射目标, 是否命中)。目标为 None 且命中表示还要按 军体/体育 再分。"""
    for token, target in table:
        if token in text:
            return target, True
    return None, False


def _resolve(target: str | None, name: str, course_id: str) -> str:
    if target is not None:
        return target
    return "体育课" if is_pe(name, course_id) else "思政类"


def plan_target(path: str) -> tuple[str | None, bool]:
    return _find(PLAN_MAP, path or "")


def category_from_plan(path: str, name: str, course_id: str) -> str | None:
    target, matched = plan_target(path)
    return _resolve(target, name, course_id) if matched else None


def category_from_catalog(categories: list[str], name: str, course_id: str) -> str | None:
    usable = ";".join(c for c in categories if c not in ROLLUP)
    target, matched = _find(CATALOG_MAP, usable)
    return _resolve(target, name, course_id) if matched else None


def parse_slots(text: str) -> list[str]:
    """`周二第6,7节<br>周五第1,2节{秋1,4-8周}` → ['Tue-6','Tue-7','Fri-1','Fri-2']。

    单周/双周与周次段在抓取数据里无法表达成学期内差异，按“每周都上”处理（冲突判定偏保守）。
    """
    slots: list[str] = []
    for day, periods in _SJ.findall(_BRACE.sub("", text or "")):
        for number in periods.split(","):
            if number:
                slots.append(f"{_DAY[day]}-{int(number)}")
    return slots


def load_capacity(path: str | None) -> dict[str, int]:
    if not path:
        return {}
    with open(path, encoding="utf-8-sig") as handle:
        if path.endswith(".json"):
            raw = json.load(handle)
            items = raw if isinstance(raw, list) else raw.items()
            out = {}
            for key, value in items:
                out[str(key)] = int(value)
            return out
        reader = csv.DictReader(handle)
        fields = reader.fieldnames or []
        id_col = next((f for f in fields if f in ("教学班号", "班号", "section_id", "h")), None)
        # 教务接口里的 rs 是「余量」不是容量，故不能当容量列用。
        cap_col = next((f for f in fields if f in ("最大容量", "容量", "capacity", "xlrs")), None)
        if not id_col or not cap_col:
            raise SystemExit(f"容量文件需要包含 教学班号/section_id 与 最大容量/容量/capacity 两列，"
                             f"实际列：{fields}")
        return {row[id_col].strip(): int(row[cap_col]) for row in reader
                if row.get(cap_col, "").strip().isdigit()}


def plan_targets(rows: list[dict], grade_year: str, year_index: str,
                 course_category: dict[str, str],
                 skip_categories: tuple[str, ...] = (), cap: int = 0) -> dict[str, dict[str, int]]:
    """本学期各类别必修门数：取该专业 建议学年学期 以 <year_index>( 开头且落在秋/冬 的课程。"""
    return _plan_targets(rows, grade_year, (year_index,), course_category,
                         skip_categories, by_year=False, cap=cap)[None]


def _cap_counts(counter: Counter, cap: int) -> dict[str, int]:
    """把候选课池的门数压到一个学期真正修得下的量（cap<=0 表示不压）。

    方案 CSV 的 秋/冬 行列的是该学年该类别的**候选课**（方向选修会整片列出来），
    不是必修清单：计算机科学与技术 三 有 25 门 / 64.5 学分。直接当必修门数用会让
    每个学生在两轮选课里必然缺口，满意度指标退化成一团常量。
    这里按各类别原门数等比缩到 cap，非零类别至少留 1 门，余数给占比最大的类别。
    """
    total = sum(counter.values())
    if cap <= 0 or total <= cap:
        return dict(counter)
    factor = cap / total
    raw = {key: count * factor for key, count in counter.items()}
    out = {key: max(1, int(value)) for key, value in raw.items()}
    if len(out) >= cap:
        return out
    order = sorted(raw, key=lambda key: raw[key] - int(raw[key]), reverse=True)
    index = 0
    while sum(out.values()) < cap:
        out[order[index % len(order)]] += 1
        index += 1
    return out


def _plan_targets(rows: list[dict], grade_year: str, year_indexes: tuple[str, ...],
                  course_category: dict[str, str], skip_categories: tuple[str, ...],
                  by_year: bool, cap: int = 0) -> dict:
    """按学年统计各专业的学期类别门数。by_year=True 时返回 {学年: {专业: {类别: 门数}}}，
    False 时返回 {None: {专业: {类别: 门数}}}（只取 year_indexes 里的那一个学年）。

    计数口径：grid 行按 (专业, 学年, 课程号) 去重；秋/冬任一列非空才算落在本学期
    （方案里"一(秋冬)"这类合并单元格被解析成"一(冬)"，标签写窄但筛子照样命中一次）。
    """
    out: dict = {yi: {} for yi in year_indexes} if by_year else {None: {}}
    per_major: dict = {}
    seen: dict = {}
    for row in rows:
        if row["grade_year"] != grade_year:
            continue
        term = (row["建议学年学期"] or "").strip()
        if not term:
            continue
        index = term[0]
        if index not in year_indexes or not term.startswith(f"{index}("):
            continue
        if not ((row["秋"] or "").strip() or (row["冬"] or "").strip()):
            continue
        course_id = (row["课程号"] or "").strip()
        category = course_category.get(course_id)
        if not category or category in skip_categories:
            continue
        major = row["major"].strip()
        bucket = index if by_year else None
        seen.setdefault(bucket, {}).setdefault(major, set())
        if course_id in seen[bucket][major]:
            continue
        seen[bucket][major].add(course_id)
        per_major.setdefault(bucket, {}).setdefault(major, Counter())[category] += 1
    for bucket, majors in per_major.items():
        target = out[bucket if by_year else None]
        for major, counter in majors.items():
            target[major] = _cap_counts(counter, cap)
    return out


def plan_targets_by_year(rows: list[dict], grade_year: str, year_indexes: tuple[str, ...],
                         course_category: dict[str, str],
                         skip_categories: tuple[str, ...] = (),
                         cap: int = 0) -> dict[str, dict[str, dict[str, int]]]:
    """{专业: {学年: {类别: 门数}}}，用于把 二/三 学年当作当前大二/大三 同学期的代理。"""
    per_year = _plan_targets(rows, grade_year, year_indexes, course_category,
                             skip_categories, by_year=True, cap=cap)
    merged: dict[str, dict[str, dict[str, int]]] = {}
    for index, majors in per_year.items():
        for major, counts in majors.items():
            merged.setdefault(major, {})[index] = counts
    return merged


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--catalog", default="/home/redaunt/ai_course_planner_mvp/zju_catalog")
    parser.add_argument("--capacity", default=None, help="教学班号→容量 的 CSV 或 JSON（抓取数据无容量字段）")
    parser.add_argument("--capacity-note", default=None,
                        help="容量快照口径说明，写进 _meta.capacity_source（容量是时点数据，务必记时间）")
    parser.add_argument("--capacity-default", type=int, default=100)
    parser.add_argument("--capacity-divisor", type=int, default=1,
                        help="把每个教学班容量按此倍数缩小（微缩实验用，最小 1 座）；需求侧要同比例缩小，否则稀缺结构消失")
    parser.add_argument("--grade-year", default="2026")
    parser.add_argument("--year-index", default="一", help="本学期在培养方案里的学年序号（2026 级 → 一）")
    parser.add_argument("--year-indexes", default="一",
                        help="逗号分隔，额外输出 plan.recommended_by_year（如 一,二,三 把三个年段都建出来）")
    parser.add_argument("--language-per-student", type=float, default=0.0,
                        help="外语类每学期每生门数（方案原文是四年 6 学分池，不是每学期必修）。"
                             ">0 时外语类不再计入必修门数，改写进 plan.need_rules")
    parser.add_argument("--pe-per-student", type=int, default=0,
                        help="体育每学期每生门数（方案原文：大一到大三每学期 1 门，但方案不列课名）。"
                             ">0 时写进 plan.need_rules")
    parser.add_argument("--exclude-grade", action="append", default=[],
                        help="排除的学年序号（可重复），如 --exclude-grade 四 --exclude-grade 五")
    parser.add_argument("--courses-per-student-cap", type=int, default=0,
                        help="把每个 (专业,学年) 的目标门数等比压到此上限（0=不压）。"
                             "方案行是候选课池而非必修清单，不压会让所有学生必然缺口")
    parser.add_argument("--out", default=None)
    parser.add_argument("--report", default=None, help="把无法归类/被剔除的记录写到这个 JSON")
    args = parser.parse_args()

    catalog_path = f"{args.catalog}/2026-2027_1/courses.json"
    plans_path = f"{args.catalog}/pyfa_2026/plan_courses.csv"
    catalog = json.load(open(catalog_path, encoding="utf-8"))
    plan_rows = list(csv.DictReader(open(plans_path, encoding="utf-8-sig")))
    capacity = load_capacity(args.capacity)
    if args.capacity_divisor < 1:
        raise SystemExit("--capacity-divisor 必须 ≥ 1")

    def scaled_capacity(section_id: str) -> int:
        return max(1, round(capacity.get(section_id, args.capacity_default)
                            / args.capacity_divisor))

    excluded = tuple(g.strip() for g in args.exclude_grade if g.strip())
    year_indexes = tuple(y for y in (s.strip() for s in args.year_indexes.split(","))
                         if y and y not in excluded)
    rule_skip = tuple(key for key, on in (
        ("外语类", args.language_per_student > 0), ("体育课", args.pe_per_student > 0)) if on)
    need_rules: dict = {}
    if args.language_per_student > 0:
        need_rules["language_share_per_student"] = args.language_per_student
    if args.pe_per_student > 0:
        need_rules["pe_courses_per_student"] = args.pe_per_student
    if excluded:
        need_rules["exclude_grades"] = sorted(excluded)
    if args.courses_per_student_cap > 0:
        need_rules["courses_per_student_cap"] = args.courses_per_student_cap
        need_rules["cap_reason"] = (
            "方案 CSV 的 秋/冬 行是该学年该类别的候选课（方向选修整片列出），不是必修清单："
            "计算机科学与技术 三 列出 25 门 / 64.5 学分。这里按各类别原门数等比压到每学期 "
            f"{args.courses_per_student_cap} 门（非零类别至少 1 门）；不压则两轮选课下所有学生"
            "必然缺口，满意度指标失去区分度")
    if need_rules:
        need_rules["source"] = ("2026 级培养方案原文：外语类是 3-6 学分阶梯池（四年累计 6 学分），"
                                "大英Ⅲ/Ⅳ 不是每学期必修；体育课要求前三年每学期选 1 门，"
                                "但方案不列课名，grid 行数恒为 0，只能作 standing rule 交给合成器")

    # 先扫全部方案行，得到 课程号 → 官方类别（与学期无关，能覆盖开课目录里的课）
    course_category: dict[str, str] = {}
    course_major: dict[str, set[str]] = defaultdict(set)
    for row in plan_rows:
        course_id = (row["课程号"] or "").strip()
        if not course_id:
            continue
        path = row["module_path"] or ""
        candidate = category_from_plan(path, row["课程名称"] or "", course_id)
        if candidate:
            course_category.setdefault(course_id, candidate)
            if candidate == "专业课":
                course_major[course_id].add(row["major"].strip())

    courses: list[dict] = []
    sections: list[dict] = []
    dropped = {"no_time": 0, "unmapped": 0, "bad_time": 0, "dup_section": 0, "weekend": 0}
    unmapped: list[dict] = []
    seen_sections: set[str] = set()
    per_category = Counter()

    for course in catalog["courses"]:
        course_id = course["kcdm"]
        name = course["kcmc"]
        category = course_category.get(course_id) or category_from_catalog(
            course.get("categories", []), name, course_id)
        if category not in CATEGORIES:
            dropped["unmapped"] += 1
            unmapped.append({"course_id": course_id, "name": name,
                             "catalog_categories": course.get("categories", [])})
            continue
        item = {"course_id": course_id, "name": name, "category": category}
        if category == "专业课":
            majors = course_major.get(course_id) or set()
            if len(majors) == 1:
                item["major"] = next(iter(majors))
        courses.append(item)
        per_category[category] += 1

        for jxb in course.get("jxb", []):
            section_id = jxb["xkkh"]
            time_text = jxb.get("sksj") or ""
            if time_text in ("--", ""):
                dropped["no_time"] += 1
                continue
            slots = parse_slots(time_text)
            if not slots:
                dropped["bad_time"] += 1
                continue
            # 发起人 2026-10-19 定：模拟只排周一到周五，周末班在转换阶段就剔除，
            # 让 load_world 的非法星期校验继续能挡住别处进来的周末数据。
            if any(slot.split("-")[0] not in GRID_DAYS for slot in slots):
                dropped["weekend"] += 1
                continue
            if section_id in seen_sections:
                dropped["dup_section"] += 1
                continue
            seen_sections.add(section_id)
            sections.append({
                "section_id": section_id,
                "course_id": course_id,
                "capacity": scaled_capacity(section_id),
                "slots": sorted(set(slots)),
                "teacher": re.sub(r"<br\s*/?>", "、", jxb.get("jsxm") or "") or None,
                "room": jxb.get("skdd"),
                "term_flag": jxb.get("xxq"),
            })

    recommended = plan_targets(plan_rows, args.grade_year, args.year_index,
                               course_category, skip_categories=rule_skip,
                               cap=args.courses_per_student_cap)
    by_year = (plan_targets_by_year(plan_rows, args.grade_year, year_indexes,
                                    course_category, skip_categories=rule_skip,
                                    cap=args.courses_per_student_cap)
               if len(year_indexes) > 1 else {})
    majors = sorted(set(recommended) | set(by_year))
    used_capacity = sum(1 for s in sections if s["section_id"] in capacity)
    divisor_note = (f"，再按 --capacity-divisor {args.capacity_divisor} 缩小（最小 1 座）"
                    if args.capacity_divisor > 1 else "")
    data = {
        "_meta": {
            "synthetic": False,
            "source": "ZJU 教务选课系统 2026-2027 学期(秋冬) 开课目录 + 2026 级培养方案结构化解析",
            "catalog_semester": catalog.get("semester"),
            "catalog_scraped_at": catalog.get("scraped_at"),
            "generated_at": datetime.date.today().isoformat(),
            "course_count": len(courses),
            "section_count": len(sections),
            "dropped": dropped,
            "capacity_source": (f"容量来自 {os.path.basename(args.capacity)} 的「最大容量」列"
                                f"（{used_capacity}/{len(sections)} 个教学班命中）{divisor_note}"
                                + (f"；{args.capacity_note}" if args.capacity_note else "")
                                if args.capacity else
                                f"未提供真实容量：全部按假设值 {args.capacity_default} 填充，非教务数据{divisor_note}"),
            "capacity_divisor": args.capacity_divisor,
            "category_mapping": "见 tools/build_courses_from_zju.py 的 PLAN_MAP / CATALOG_MAP",
            "time_semantics": "节次 1-13 直接对应时间槽；单双周与周次段按每周都上处理；"
                              "秋/冬/短学期班一律纳入；周六/周日班已剔除（见 dropped.weekend）",
        },
        "courses": courses,
        "sections": sections,
        "plan": {"majors": majors,
                 "recommended": {m: recommended[m] for m in majors if m in recommended}},
    }
    if by_year:
        data["plan"]["recommended_by_year"] = by_year
        data["_meta"]["year_proxy_assumption"] = (
            f"只爬到 {args.grade_year} 级方案：plan.recommended_by_year 把同一份方案的 "
            f"{'/'.join(year_indexes)} 学年行当作当前 {'/'.join(year_indexes)} 届学生本学期修读结构的代理，"
            "假设方案跨届结构稳定（与\"四个年级同构\"的人数假设同源）；"
            f"排除的学年：{'/'.join(excluded) or '无'}")
    if need_rules:
        data["plan"]["need_rules"] = need_rules
        data["_meta"]["need_rules"] = need_rules

    text = json.dumps(data, ensure_ascii=False, indent=1)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
    if args.report:
        with open(args.report, "w", encoding="utf-8") as handle:
            json.dump({"unmapped_courses": unmapped[:400], "unmapped_total": len(unmapped),
                       "dropped": dropped}, handle, ensure_ascii=False, indent=1)
    if not args.out:
        sys.stdout.write(text)

    print(f"courses={len(courses)} sections={len(sections)} majors={len(majors)}", file=sys.stderr)
    print("各类别课程数:", dict(per_category), file=sys.stderr)
    print("剔除统计:", dropped, file=sys.stderr)
    print(f"容量：{data['_meta']['capacity_source']}", file=sys.stderr)
    if need_rules:
        print(f"需求口径：{need_rules}", file=sys.stderr)
    if by_year:
        print(f"按学年目标：{'/'.join(year_indexes)} 学年，覆盖专业 {len(by_year)} 个"
              f"（{sum(len(v) for v in by_year.values())} 个 (专业,学年) 组合）", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
