#!/usr/bin/env python3
"""Every number in README §7C must trace back to a file on disk, not to something I typed.

`results_batches/readme_number_guard.py` only walks §7 (the v1 tables), so §7C — the one section that
already quotes v2 utilities — was hand-transcribed from the batch log. This guard closes that hole:
each `臂名=数值` token in §7C has to appear in a landed `raw_rows.csv` (smoke or any chunk) or in a
`[seed N]` line that the runner itself printed, and each `从 X 变成 Y（−D）` difference has to equal
X − Y computed here.

It also fails when it finds nothing to check: a renamed section silently disabling a guard is the same
class of bug as a misspelled CSV column reading as `None`.

Cost lines are checked differently on purpose. `分钟/种子 ⇒ 整批 X 小时` and the `个档案/分钟 ⇒ N ÷ rate`
route are recomputed here against the chunk plan (`need_v2_chunk_plan.CHUNKS` → seeds; `--n-students` in
the batch script → profiles per seed), so the arithmetic is verifiable at any time, while the raw readings
that the batch keeps moving are deliberately *not* quoted as single points.

    .venv/bin/python results_need_v2/need_v2_number_guard.py [path/to/README.md]
"""
from __future__ import annotations

import csv
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
README = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "README.md")
LOG = os.path.join(ROOT, "logs_v2", "v2_chunks.log")
CHUNK_SH = os.path.join(HERE, "run_need_v2_chunks.sh")
TOKEN = re.compile(r"(I2|J2|K2)_[^\s=，、（）]+=\d+(?:\.\d+)?")
DRIFT = re.compile(r"从[^\d]{0,10}?(\d+(?:\.\d+)?)\s*变成\s*(\d+(?:\.\d+)?)\s*（([−+-])(\d+(?:\.\d+)?)）")
MIN_PER_SEED = re.compile(r"([\d.]+) 分钟/种子 ⇒ 整批 ([\d.]+) 小时")
RATE_TO_HOURS = re.compile(r"([\d.]+) 个档案/分钟 ⇒ (\d+) ÷ ([\d.]+) = ([\d.]+) 小时")


def section(text: str) -> str:
    start = text.find("## 7C.")
    if start < 0:
        raise SystemExit("FAIL：README 里没有「## 7C.」小节，本守卫无事可做——先确认小节标题有没有被改掉。")
    rest = text[start + len("## 7C."):]
    end = rest.find("\n## ")
    return rest if end < 0 else rest[:end]


def sources() -> set:
    """Tokens the runner itself produced: CSV cells first, then its own stdout lines."""
    out = set()
    dirs = [d for d in os.listdir(HERE)
            if os.path.isdir(os.path.join(HERE, d)) and os.path.exists(os.path.join(HERE, d, "raw_rows.csv"))]
    for name in sorted(dirs):
        with open(os.path.join(HERE, name, "raw_rows.csv"), encoding="utf-8-sig", newline="") as handle:
            for row in csv.DictReader(handle):
                value = (row.get("total_preference_utility") or "").strip()
                if value:
                    out.add(f"{row['mechanism']}={value}")
    if os.path.exists(LOG):
        with open(LOG, encoding="utf-8") as handle:
            for line in handle:
                for token in re.findall(r"\S+_\S*=\d+(?:\.\d+)?", line):
                    out.add(token.strip("，、"))
    return out


def batch_scale() -> tuple[int, int]:
    """种子数只认 `need_v2_chunk_plan.CHUNKS`，每种子档案数只认批次脚本自己的 `--n-students`。

    §7C 写"1872 个档案"时，这个数必须由这两处推出来；写不出来源就当无出处。
    """
    sys.path.insert(0, ROOT)
    from need_v2_chunk_plan import CHUNKS
    seeds = sum(len(seeds) for _, seeds in CHUNKS)
    with open(CHUNK_SH, encoding="utf-8") as handle:
        found = re.findall(r"--n-students (\d+)", handle.read())
    if not found:
        raise SystemExit("FAIL：批次脚本里读不到 --n-students，§7C 的档案总数就无从核对。")
    return seeds, int(found[0])


def main() -> int:
    with open(README, encoding="utf-8") as handle:
        body = section(handle.read())
    known = sources()
    tokens = sorted({m.group(0) for m in TOKEN.finditer(body)})
    bad = 0
    if not tokens:
        print("FAIL：§7C 里找不到任何「臂名=数值」token，本守卫什么都没核对——空核对不算通过。")
        return 1
    for token in tokens:
        if token in known:
            print(f"OK：{token} 能在盘上的落盘文件或批次日志里找到。")
        else:
            arm = token.split("_", 1)[0]
            near = sorted(t for t in known if t.startswith(arm + "_"))[:6]
            print(f"FAIL：{token} 不是任何一份 raw_rows.csv 或批次日志给出的值；该臂现有值：{near}")
            bad += 1
    drifts = DRIFT.findall(body)
    said = body.count("变成")
    if said != len(drifts):
        print(f"FAIL：§7C 出现 {said} 次「变成」，但差值正则只匹配到 {len(drifts)} 处——"
              "检查正在漏，不是没有差值可查。")
        return 1
    for a, b, sign, d in drifts:
        want = abs(float(a) - float(b))
        if abs(want - float(d)) < 0.005:
            print(f"OK：差值 {sign}{d} 与 {a}、{b} 的算术一致（本守卫重算）。")
        else:
            print(f"FAIL：文中写 {sign}{d}，但 {a} 与 {b} 相差 {round(want, 2)}。")
            bad += 1
    seeds_total, students = batch_scale()
    profiles_total = seeds_total * students
    costs = MIN_PER_SEED.findall(body)
    rates = RATE_TO_HOURS.findall(body)
    if body.count("分钟/种子 ⇒") != len(costs):
        print(f"FAIL：§7C 出现 {body.count('分钟/种子 ⇒')} 次「分钟/种子 ⇒」，正则只匹配到 {len(costs)} 处"
              "——耗时检查正在漏，不是没有耗时可查。")
        return 1
    if body.count("个档案/分钟 ⇒") != len(rates):
        print(f"FAIL：§7C 出现 {body.count('个档案/分钟 ⇒')} 次「个档案/分钟 ⇒」，正则只匹配到 {len(rates)} 处"
              "——同上，先修正则再谈通过。")
        return 1
    if not costs or not rates:
        print("FAIL：§7C 的耗时核对少了一条路径（「分钟/种子 ⇒」与「个档案/分钟 ⇒」两条都要在）。"
              "只剩一条时，把另一句改写掉就能让那条检查空转。")
        return 1
    hours: list[float] = []
    for per_seed, total in costs:
        want = round(float(per_seed) * seeds_total / 60.0, 2)
        if abs(want - float(total)) < 0.005:
            print(f"OK：{per_seed} 分钟/种子 × 分块计划的 {seeds_total} 颗种子 = {total} 小时（本守卫重算）。")
        else:
            print(f"FAIL：文中写 {total} 小时，但 {per_seed} × {seeds_total} ÷ 60 = {want}。")
            bad += 1
        hours.append(float(total))
    for rate, profiles, rate_again, total in rates:
        if rate != rate_again:
            print(f"FAIL：同一句里写了两个速率 {rate} 与 {rate_again}。")
            bad += 1
            continue
        if int(profiles) != profiles_total:
            print(f"FAIL：文中写 {profiles} 个档案，但分块计划是 {seeds_total} 种子 × {students} 人 "
                  f"= {profiles_total}。")
            bad += 1
            continue
        want = round(int(profiles) / float(rate) / 60.0, 2)
        if abs(want - float(total)) < 0.005:
            print(f"OK：{profiles} 档案 ÷ {rate} 个/分钟 = {total} 小时，"
                  f"且 {profiles} = {seeds_total} × {students}（分块计划与批次脚本）。")
        else:
            print(f"FAIL：{profiles} ÷ {rate} ÷ 60 = {want} 小时，不是文中的 {total}。")
            bad += 1
        hours.append(float(total))
    if len(hours) >= 2 and max(hours) - min(hours) > 0.05:
        print(f"FAIL：§7C 两条算法路径给出的整批耗时对不上：{hours}。")
        bad += 1
    if bad:
        print(f"问题 = {bad} 处：§7C 有数字无出处或算不回来。")
        return 1
    print(f"问题 = 无：§7C 的 {len(tokens)} 个数值 token、{len(drifts)} 个差值与 "
          f"{len(costs) + len(rates)} 条耗时算术全部可追溯。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
