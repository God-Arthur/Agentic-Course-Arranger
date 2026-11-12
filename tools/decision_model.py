"""离线决策模型：机制实验的被试，不是生产兜底。

生产入口（演示、cli、run_experiment 默认路径）不会导入本模块。
它只实现提示词里已经写明的事实约束，用来在不联网时把「两轮有没有竞争」
「复盘改写会不会动课表」这两件事跑成可判定的数字。
线上模型换不换得出同样的方向，不由本文件声称。
"""
from __future__ import annotations

import zlib
from collections import Counter

from planner.models import World, category_key
from planner.policy import PRIORITY_TIERS
from planner.timegrid import has_conflict


class DecisionModel:
    """按 system 提示词分流。seed / jitter 只影响满意度里的可配对噪声。"""

    def __init__(self, world: World, seed: int = 0, jitter: int = 0, morning_penalty: int = 0,
                 score_anchor: int | None = None):
        self.world = world
        self.seed = seed
        self.jitter = jitter
        self.morning_penalty = morning_penalty
        # 流水线会把满意度夹到 0–100。锚在 60 时，早八扣分和 ±20 噪声都落在夹紧区间里面，
        # 配对差分不会被夹没。默认 None 表示满分就是 100。
        self.score_anchor = score_anchor
        self.calls = 0
        self.failures = 0

    def perf(self) -> dict:
        return {"avg_latency_s": 0.0, "max_latency_s": 0.0, "call_span_s": 0.0,
                "effective_rpm": None, "backoffs": 0}

    def chat_json(self, system: str, user: str) -> dict:
        self.calls += 1
        if system.startswith("你是教务系统的需求解析器"):
            return _parse_need(user)
        if "plans" in system and "多名学生" in system:
            return {"plans": [{"student_id": sid, "sections": _choose_sections(self.world, block),
                               "why": "按目标覆盖必修"}
                              for sid, block in _student_blocks(user)]}
        if system.startswith("你是教务系统的预置课表规划器"):
            return {"sections": _choose_sections(self.world, user), "why": "按目标覆盖必修"}
        if system.startswith("你扮演一名中国大学生"):
            return _choose_round(self.world, user)
        if system.startswith("你是教务系统的选课筛选器"):
            return {"grants": _choose_grants(self.world, user)}
        if system.startswith("你是选课满意度评估器"):
            return _score(self, user)
        raise AssertionError(f"决策模型不认识的提示词：{system[:40]}")


def _parse_need(user: str) -> dict:
    required: dict[str, int] = {}
    avoid: list[str] = []
    notes = ""
    for line in user.splitlines():
        if line.startswith("本学期修读目标："):
            body = line.split("：", 1)[1].strip()
            if body and body != "无":
                for part in body.split("；"):
                    name, _, count = part.strip().rpartition(" ")
                    if name and count:
                        required[name] = int(count)
        elif line.startswith("尽量避开："):
            body = line.split("：", 1)[1].strip()
            if body and body != "无":
                avoid.extend(item.strip() for item in body.split("、") if item.strip())
        elif line.startswith("备注："):
            notes = line.split("：", 1)[1].strip()
    return {"required": required, "elective": {}, "avoid_slots": avoid, "notes": notes}


def _student_blocks(user: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current_id = ""
    buf: list[str] = []
    for line in user.splitlines():
        if line.startswith("===== 学生 ") and line.endswith(" ====="):
            if current_id:
                blocks.append((current_id, "\n".join(buf)))
            current_id = line[len("===== 学生 "):-len(" =====")]
            buf = []
        else:
            buf.append(line)
    if current_id:
        blocks.append((current_id, "\n".join(buf)))
    if not blocks:
        raise AssertionError("批量预置提示词里没有学生分块")
    return blocks


def _lines_between(user: str, start: str, end: str | None) -> list[str]:
    lines = user.splitlines()
    try:
        begin = lines.index(start) + 1
    except ValueError:
        return []
    if end is None:
        return lines[begin:]
    try:
        stop = lines.index(end, begin)
    except ValueError:
        stop = len(lines)
    return lines[begin:stop]


def _section_ids(lines: list[str]) -> list[str]:
    found = []
    for line in lines:
        if not line or line.startswith("（"):
            continue
        found.append(line.split("|", 1)[0])
    return found


def _targets(user: str) -> list[tuple[str, int, int, bool]]:
    rows = []
    for line in _lines_between(user, "【目标完成情况】", "【候选教学班】"):
        if "：目标 " not in line or "，已有 " not in line:
            continue
        head, _, rest = line.partition("：目标 ")
        required = head.endswith("（必修）")
        key = head[:-len("（必修）")] if required else head
        want_text, _, have_text = rest.partition("，已有 ")
        rows.append((key, int(want_text), int(have_text), required))
    return rows


def _candidates(world: World, user: str) -> list[dict]:
    rows = []
    for index, line in enumerate(_lines_between(user, "【候选教学班】", "请排出本学期的预置课表。")):
        if line.startswith("（") or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 5:
            continue
        section_id = parts[0]
        if section_id not in world.sections:
            continue
        left_text = parts[4]
        left = int(left_text[len("剩余"):]) if left_text.startswith("剩余") else 0
        section = world.sections[section_id]
        course = world.course_of_section(section_id)
        rows.append({
            "sid": section_id,
            "course": section.course_id,
            "key": category_key(course.category),
            "slots": section.slots,
            "left": left,
            "index": index,
        })
    if rows:
        return rows
    # 选课轮次的候选块以「请决定本轮退课与志愿」结尾，不是预置那句。
    for index, line in enumerate(_lines_between(user, "【候选教学班】", "请决定本轮退课与志愿。")):
        if line.startswith("（") or "|" not in line:
            continue
        parts = line.split("|")
        if len(parts) < 5 or parts[0] not in world.sections:
            continue
        left_text = parts[4]
        left = int(left_text[len("剩余"):]) if left_text.startswith("剩余") else 0
        section = world.sections[parts[0]]
        course = world.course_of_section(parts[0])
        rows.append({
            "sid": parts[0],
            "course": section.course_id,
            "key": category_key(course.category),
            "slots": section.slots,
            "left": left,
            "index": index,
        })
    return rows


def _choose_sections(world: World, user: str) -> list[str]:
    """无复盘记录时按目录顺序先占（热门班先被填满）。

    有上一版预置时，改用「能覆盖同样多必修、且尽量不重复上一版」的选法。
    这是复盘才看得到的信息，第一遍预置没有这个输入。
    """
    review = "【上一版预置课表】" in user
    previous = set(_section_ids(_lines_between(user, "【上一版预置课表】", "【两轮选课后的最终课表】")))
    required = [(key, want - have) for key, want, have, flag in _targets(user)
                if flag and want > have]
    pool = _candidates(world, user)
    hot, hot_n = _fill(pool, required, previous, deprioritize_previous=False)
    spread, spread_n = _fill(pool, required, previous, deprioritize_previous=review)
    if review and spread_n >= hot_n:
        return spread
    return hot


def _fill(pool: list[dict], required: list[tuple[str, int]], previous: set[str],
          deprioritize_previous: bool) -> tuple[list[str], int]:
    chosen: list[str] = []
    used_slots: set = set()
    used_courses: set[str] = set()
    filled = 0
    for key, want in required:
        options = [row for row in pool if row["key"] == key]
        if deprioritize_previous:
            options = sorted(options, key=lambda row: (row["sid"] in previous, -row["left"], row["index"]))
        else:
            options = sorted(options, key=lambda row: row["index"])
        got = 0
        for row in options:
            if got >= want:
                break
            if row["sid"] in chosen or row["course"] in used_courses:
                continue
            if has_conflict(row["slots"], used_slots):
                continue
            chosen.append(row["sid"])
            used_slots.update(row["slots"])
            used_courses.add(row["course"])
            got += 1
            filled += 1
    return chosen, filled


def _assigned_ids(user: str) -> list[str]:
    return _section_ids(_lines_between(user, "【当前已选课程】", "【目标完成情况】"))


def _choose_round(world: World, user: str) -> dict:
    """缺的必修能直接加就加；只有和时间冲突的已选课互相挡住时才退课。

    志愿按剩余名额从少到多排，所以还剩一个座位的热门班会收到整批申请。
    """
    assigned = _assigned_ids(user)
    held = set(assigned)
    drops: list[str] = []
    prefs: list[str] = []
    short = [(key, want - have) for key, want, have, flag in _targets(user) if flag and want > have]
    pool = _candidates(world, user)

    def current_slots() -> set:
        found: set = set()
        for section_id in held:
            found.update(world.sections[section_id].slots)
        for section_id in prefs:
            found.update(world.sections[section_id].slots)
        return found

    for key, want in short:
        options = [row for row in pool if row["key"] == key and row["sid"] not in held and row["sid"] not in prefs]
        direct = sorted((row for row in options if not has_conflict(row["slots"], current_slots())),
                        key=lambda row: (row["left"], row["index"]))
        added = 0
        taken_courses = {world.sections[section_id].course_id for section_id in prefs}
        if direct:
            for row in direct:
                if added >= want:
                    break
                if row["course"] in taken_courses or has_conflict(row["slots"], current_slots()):
                    continue
                prefs.append(row["sid"])
                taken_courses.add(row["course"])
                added += 1
            if added:
                continue
        feasible = []
        for row in options:
            if row["course"] in taken_courses:
                continue
            if any(has_conflict(row["slots"], world.sections[picked].slots) for picked in prefs):
                continue
            blocking = [section_id for section_id in list(held)
                        if has_conflict(world.sections[section_id].slots, row["slots"])]
            feasible.append((len(blocking), row["left"], row["index"], row, blocking))
        if not feasible:
            continue
        feasible.sort(key=lambda item: (item[0], item[1], item[2]))
        _, _, _, row, blocking = feasible[0]
        for section_id in blocking:
            if section_id not in drops:
                drops.append(section_id)
                held.discard(section_id)
        prefs.append(row["sid"])
    return {"drops": drops,
            "preferences": [{"section_id": section_id, "rank": rank}
                            for rank, section_id in enumerate(prefs, 1)]}


def _choose_grants(world: World, user: str) -> list[dict]:
    """按提示词的志愿序号和优先级分组排序后逐个提名。

    超额的提名会在筛选器分块记账时被拒绝，次数进 llm_stats["sanitized_by_grant_reject"]。
    这里不把超额的人悄悄丢掉，否则名额不够这件事不会留下计数。
    """
    grants: list[dict] = []
    seen_course: dict[str, set[str]] = {}
    for line in _lines_between(user, "【志愿池】", "请给出本块的中签名单。"):
        if line.startswith("（") or "|" not in line:
            continue
        try:
            section_id, _, _, _left, body = line.split("|", 4)
        except ValueError:
            continue
        if section_id not in world.sections:
            continue
        course_id = world.sections[section_id].course_id
        applicants: list[tuple[int, int, str]] = []
        for chunk in body.split("；"):
            if "：" not in chunk or not chunk.startswith("志愿"):
                continue
            label, _, names = chunk.partition("：")
            rank = int(label[len("志愿"):] or "99")
            for name in names.split("、"):
                if "(" not in name or not name.endswith(")"):
                    continue
                student_id, _, tier_name = name[:-1].partition("(")
                tier = PRIORITY_TIERS.index(tier_name) if tier_name in PRIORITY_TIERS else len(PRIORITY_TIERS)
                applicants.append((rank, tier, student_id))
        applicants.sort()
        for _rank, _tier, student_id in applicants:
            owned = seen_course.setdefault(student_id, set())
            if course_id in owned:
                continue
            owned.add(course_id)
            grants.append({"student_id": student_id, "section_id": section_id})
    return grants


def _score(model: DecisionModel, user: str) -> dict:
    met = 0
    total = 0
    for _key, want, have, flag in _targets(user):
        if not flag:
            continue
        total += want
        met += min(want, have)
    ceiling = 100 if model.score_anchor is None else model.score_anchor
    base = ceiling if total == 0 else round(ceiling * met / total)
    penalty = 0
    if model.morning_penalty and "早八" in user:
        for section_id in _section_ids(_lines_between(user, "【最终课表】", "【目标完成情况】")):
            if section_id in model.world.sections and any(
                    period == 1 for _day, period in model.world.sections[section_id].slots):
                penalty = model.morning_penalty
                break
    noise = 0
    if model.jitter:
        student_id = ""
        for line in user.splitlines():
            if line.startswith("@SKILL "):
                student_id = line.split()[1]
                break
        noise = zlib.adler32(f"{model.seed}:{student_id}".encode()) % (2 * model.jitter + 1) - model.jitter
    score = base - penalty + noise
    reason = f"必修完成 {met}/{total}"
    if penalty:
        reason += "；早八扣分"
    if noise:
        reason += f"；噪声 {noise:+d}"
    return {"satisfaction": score, "reason": reason}


def category_counts(world: World, section_ids) -> Counter:
    return Counter(category_key(world.course_of_section(section_id).category)
                   for section_id in section_ids)
