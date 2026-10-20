#!/usr/bin/env python3
"""从批次自己的文件算 v2 的实测耗时与吞吐：只读，不碰求解器、不调 LLM，批次期间可以随时跑。

三个可核对的来源：
- `logs_v2/v2_chunks.log` 的创建时间 = 批次启动时刻，修改时间 = 最近一行 `[seed N]` 的打印时刻，
  `[seed ` 行数 = 已跑完的种子数（跨分块累加）。
- `results_need_v2/cost_samples_v2.csv` = `cost_sample_v2.sh` 每 120 秒记一次
  「缓存行数 / 已打印种子数」。种子数发生跳变的那一行，就给出该种子完成时刻（误差 ± 采样间隔）。
- `semantic_cache/llm_relevance_v2.jsonl` 的行数只用于算档案/分钟的瞬时值；缓存里没有 token 字段，
  所以提示/生成 token 只能等分块 CSV 落盘后从 `llm_prompt_tokens`、`llm_completion_tokens` 读。

    .venv/bin/python results_need_v2/cost_estimate_v2.py
"""
from __future__ import annotations

import csv
import os
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
LOG = os.path.join(ROOT, "logs_v2", "v2_chunks.log")
CACHE = os.path.join(ROOT, "semantic_cache", "llm_relevance_v2.jsonl")
SAMPLES = os.path.join(HERE, "cost_samples_v2.csv")
INTERVAL = 120.0

# 种子数与每种子档案数都从批次自己的定义里读（分块计划 + 批次脚本的 --n-students），与
# need_v2_number_guard 同一来源：手抄的 26 与 72 会在计划改动后继续印旧数。
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)
from need_v2_number_guard import batch_scale  # noqa: E402


def sample_epoch(value: str, now: float) -> float:
    """Map a stored HH:MM:SS to the most recent occurrence that is not later than `now`.

    The first version pinned every stamp to *today's* date and only added a day for times before
    06:00. The batch started at 22:10 and was still running past midnight, so the pre-midnight rows
    landed tomorrow and the per-seed gaps came out as +1502.28 / −1420.53 minutes at once.
    """
    h, m, s = (int(part) for part in value.split(":"))
    today = time.localtime(now)
    base = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, 0, 0, 0, 0, 0, -1))
    stamp = base + h * 3600 + m * 60 + s
    while stamp > now:
        stamp -= 86400.0
    return stamp


def sample_times(rows: list, now: float) -> list:
    """File order is append order, so the reconstructed clock may only move forward."""
    out, prev = [], None
    for row in rows:
        stamp = sample_epoch(row["local_time"], now)
        if prev is not None and stamp < prev:
            stamp += 86400.0
        out.append(stamp)
        prev = stamp
    return out


def _rows() -> list:
    if not os.path.exists(SAMPLES):
        return []
    with open(SAMPLES, encoding="utf-8") as handle:
        return [row for row in csv.DictReader(handle)]


def seed_boundaries(now: float) -> list:
    """(跳变被观测到的时刻, 此刻累计已完成的种子数)，只取确实增加的那些行。

    采样器 22:47 才挂上，而批次 22:10 就启动了：它观测到的第一个跳变，从批次启动算起其实覆盖了
    好几颗种子。把每个跳变当成"一颗种子"会把首段读成 62 分钟，所以调用方要用累计数归一化。
    """
    rows = _rows()
    stamps = sample_times(rows, now)
    out = []
    for prev, cur, stamp in zip(rows, rows[1:], stamps[1:]):
        if int(cur["seed_lines_printed"]) > int(prev["seed_lines_printed"]):
            out.append((stamp, int(cur["seed_lines_printed"])))
    return out


def row_series(now: float) -> list:
    rows = _rows()
    return [(stamp, int(row["llm_cache_rows"])) for row, stamp in zip(rows, sample_times(rows, now))]


def frozen_window(now: float):
    """挂起时长（秒）：采样文件里远超 120 s 的空窗，扣除采样器自己的 sleep。

    采样器和批次进程一起被冻住，所以相邻两行的时间戳之差超出间隔容差的那部分，就是没有在执行
    任何求解或请求的墙钟。02:50:05→08:58:27 那段 6 小时就是这样发现的（进程 etime 只有 4:20，
    而外壳 22:10 就起了：差值即睡眠时间）。**它必须从"分钟/种子"里剔出去**——含挂起的均值会把
    笔记本睡眠记成 LLM 限流的代价，而 §7C 那句成本结论正是由这个均值推整批小时数的。
    """
    rows = _rows()
    if len(rows) < 2:
        return None, 0.0
    stamps = sample_times(rows, now)
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    worst = max(gaps)
    frozen = sum(g - INTERVAL for g in gaps if g > 3 * INTERVAL)
    return round(worst / 60.0, 1), round(max(0.0, frozen) / 60.0, 2)


def batch_start(path: str) -> float | None:
    """Linux 上的 CPython 不填 st_birthtime，所以退回 `stat -c %W`（只读，不动批次）。"""
    birth = getattr(os.stat(path), "st_birthtime", None)
    if birth:
        return float(birth)
    done = subprocess.run(["stat", "-c", "%W", path], capture_output=True, text=True)
    value = done.stdout.strip()
    return float(value) if value.isdigit() and int(value) > 0 else None


def measure() -> dict:
    now = time.time()
    total_seeds, students = batch_scale()
    facts = {"total_seeds": total_seeds, "students_per_seed": students}
    if not os.path.exists(LOG):
        return facts
    birth = batch_start(LOG)
    stat = os.stat(LOG)
    with open(LOG, encoding="utf-8", errors="replace") as handle:
        seeds = sum(1 for line in handle if line.startswith("[seed "))
    facts.update(seeds_done=seeds, batch_start=birth, last_seed_line=stat.st_mtime)
    if birth is None:
        return facts
    minutes = (stat.st_mtime - birth) / 60.0
    mean_min = minutes / max(seeds, 1)
    # 小时必须由**印出来的**分钟数推出，不能用未取整的均值：判定正文要把这两个数并排写进
    # 「分钟/种子 ⇒ 整批 … 小时」，而 need_v2_number_guard 会拿分钟数复算小时。00:31 那次就是
    # 脚本印 8.10（未取整）、守卫按 18.67 算出 8.09，正文被退回。
    mean_min_disp = round(mean_min, 2)
    facts["mean_min_per_seed"] = mean_min_disp
    facts["mean_hours_all"] = round(mean_min_disp * total_seeds / 60.0, 2)
    facts["hours_remaining"] = round(mean_min_disp * (total_seeds - seeds) / 60.0, 2)
    worst_gap, frozen_min = frozen_window(now)
    facts["largest_sample_gap_min"] = worst_gap
    if frozen_min > 0:
        facts["frozen_hours"] = round(frozen_min / 60.0, 2)
        running_min = max(minutes - frozen_min, 1e-9) / max(seeds, 1)
        running_disp = round(running_min, 2)
        facts["mean_min_per_seed_running"] = running_disp
        facts["hours_all_running"] = round(running_disp * total_seeds / 60.0, 2)
        facts["hours_remaining_running"] = round(running_disp * (total_seeds - seeds) / 60.0, 2)
    bounds = seed_boundaries(now)
    gaps, prev_t, prev_c = [], birth, 0
    for stamp, cum in bounds:
        if cum > prev_c:
            gaps.append((stamp - prev_t) / 60.0 / (cum - prev_c))
            prev_t, prev_c = stamp, cum
    facts["seeds_covered_by_samples"] = prev_c
    if gaps:
        fastest = round(min(gaps), 2)
        slowest = round(max(gaps), 2)
        facts.update(gaps_min=[round(g, 2) for g in gaps],
                     fastest_min_per_seed=fastest,
                     slowest_min_per_seed=slowest,
                     hours_fastest=round(fastest * total_seeds / 60.0, 2),
                     hours_slowest=round(slowest * total_seeds / 60.0, 2),
                     gaps_suspect=[round(g, 2) for g in gaps if g <= 0 or g > 3 * mean_min])
    if os.path.exists(CACHE):
        with open(CACHE, encoding="utf-8", errors="replace") as handle:
            facts["cache_rows_now"] = sum(1 for line in handle if line.strip())
        series = row_series(now)
        if len(series) >= 2 and series[-1][0] > series[0][0]:
            facts["profiles_per_min_sampled"] = round(
                (series[-1][1] - series[0][1]) / ((series[-1][0] - series[0][0]) / 60.0), 2)
        facts["profiles_per_min_overall"] = round(seeds * students / max(minutes, 1e-9), 2)
    return facts


def main() -> int:
    facts = measure()
    if "mean_min_per_seed" not in facts:
        print("读不到 logs_v2/v2_chunks.log 的创建时间（Windows 上的 Python 拿不到 st_birthtime；"
              "请在 WSL 里用 .venv/bin/python 跑）。")
        return 2
    print(f"批次启动 {time.strftime('%H:%M:%S', time.localtime(facts['batch_start']))}，"
          f"最近一行种子打印于 {time.strftime('%H:%M:%S', time.localtime(facts['last_seed_line']))}，"
          f"已完成 {facts['seeds_done']}/{facts['total_seeds']} 颗种子。")
    print(f"均值 {facts['mean_min_per_seed']} 分钟/种子 ⇒ 整批 {facts['mean_hours_all']} 小时，"
          f"剩余 {facts['hours_remaining']} 小时；档案吞吐 {facts['profiles_per_min_overall']} 个/分钟"
          f"（缓存现 {facts['cache_rows_now']} 行）。")
    rate = facts.get("profiles_per_min_overall")
    if rate:
        # 把 §7C 要求的那两条算术路径原样印出来，正文直接抄，不再手算——00:31 那次退回就是因为
        # 手算的小时与守卫按文中分钟数复算的小时不一致。
        profiles = facts["total_seeds"] * facts["students_per_seed"]
        hours_via_rate = round(profiles / rate / 60.0, 2)
        print(f"§7C 两条路径（可整句照抄）：「{facts['mean_min_per_seed']} 分钟/种子 ⇒ 整批 "
              f"{facts['mean_hours_all']} 小时」；「{rate} 个档案/分钟 ⇒ {profiles} ÷ {rate} = "
              f"{hours_via_rate} 小时」（{profiles} = {facts['total_seeds']} 种子 × "
              f"{facts['students_per_seed']} 人）。")
        if abs(hours_via_rate - facts["mean_hours_all"]) > 0.05:
            print(f"FAIL：两条路径给出 {facts['mean_hours_all']} 与 {hours_via_rate} 小时，差 "
                  f"{round(abs(hours_via_rate - facts['mean_hours_all']), 2)} 小时 > 0.05，"
                  "§7C 会被守卫退回（它要求两条路对得上）。")
            return 4
    if "frozen_hours" in facts:
        students = facts["students_per_seed"]
        per_seed_run = facts["hours_all_running"] * 60.0 / facts["total_seeds"]
        rate_run = round(students / per_seed_run, 2)
        profiles_all = facts["total_seeds"] * students
        hours_run_via_rate = round(profiles_all / rate_run / 60.0, 2)
        print(f"挂起发现：采样文件里最大空窗 {facts['largest_sample_gap_min']} 分钟（采样间隔只有 "
              f"{INTERVAL / 60:.0f} 分钟），累计冻住的墙钟 {facts['frozen_hours']} 小时。这段时间求解与请求"
              "都停着，**不是实验成本**；把它算进均值，就会把笔记本睡眠写成 LLM 限流的代价。")
        print(f"§7C 只能用扣除挂起后的这两条路径：「{facts['mean_min_per_seed_running']} 分钟/种子 ⇒ 整批 "
              f"{facts['hours_all_running']} 小时」；「{rate_run} 个档案/分钟 ⇒ {profiles_all} ÷ {rate_run} = "
              f"{hours_run_via_rate} 小时」。剩余 {facts['hours_remaining_running']} 小时（按同一扣除口径）。")
        if abs(hours_run_via_rate - facts["hours_all_running"]) > 0.05:
            print(f"FAIL：扣除挂起后的两条路径给出 {facts['hours_all_running']} 与 {hours_run_via_rate} 小时，"
                  "差 > 0.05，抄进 §7C 会被守卫退回。")
            return 5
        print(f"FAIL：含挂起的均值 {facts['mean_min_per_seed']} 分钟/种子（整批 {facts['mean_hours_all']} 小时）"
              f"比扣除后的 {facts['mean_min_per_seed_running']} 高出一截，本节必须以扣除后为准；"
              "墙钟里另有这些挂起小时要在正文里说明，不能只印一个均值。")
        return 5
    if "gaps_min" in facts:
        print(f"逐种子间隔（分钟/种子，误差 ±{INTERVAL / 60:.0f}；采样器 22:47 才挂上，"
              f"首段按它覆盖的种子数归一化，累计观测到 {facts['seeds_covered_by_samples']} 颗种子的跳变）："
              f"{facts['gaps_min']}")
        print(f"按最快 {facts['fastest_min_per_seed']} 分钟/种子 = {facts['hours_fastest']} 小时；"
              f"按最慢 {facts['slowest_min_per_seed']} 分钟/种子 = {facts['hours_slowest']} 小时。")
    else:
        print("采样文件里还没有第二个种子完成时刻，区间只能等 cost_sample_v2.sh 攒够两个跳变。")
    if facts.get("gaps_suspect"):
        print(f"FAIL：逐种子间隔里出现非正值或超过均值 3 倍的值 {facts['gaps_suspect']}"
              f"（均值 {facts['mean_min_per_seed']} 分钟/种子，种子数 {facts['total_seeds']}、"
              f"每种子 {facts['students_per_seed']} 个档案）。这不是批次忽快忽慢的形状，"
              "而是时刻重建出错——先修本脚本，再谈用区间替换均值外推。")
        return 3
    if "profiles_per_min_sampled" in facts:
        print(f"采样窗内的档案/分钟 = {facts['profiles_per_min_sampled']}"
              "（含分块之间与求解期间的空窗，故低于纯调用速率）。")
    print("token 成本不在本脚本里：缓存只存 key/rows/version，"
          "提示与生成 token 要等分块 CSV 的 llm_prompt_tokens / llm_completion_tokens 落盘。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
