#!/usr/bin/env python3
"""Who can see the batch? A /proc scan of every process the two waiters match on.

`wait_batch_then_fulltests.sh` blocks on two unbracketed `pgrep -f` literals and
`post_batch_pipeline.sh` blocks on two bracketed ones. If any process other than the batch matches
and outlives the poll interval, the chain never fires and the verdict is silently never generated —
which is how the 01:57 deadlock happened. Measuring this with `pgrep` is circular: the measuring
command line contains the pattern, so it matches itself and inflates the count. This reads /proc
directly and never spawns anything whose argv carries a pattern.

Patterns are taken from the two scripts' own source, so the probe cannot drift from what they run.
A match only counts as a problem when that process is already older than the wait loop's sleep
interval: a command that dies inside one cycle delays at most that cycle, it cannot deadlock.

Read-only: no solver, no network, no writes.

    .venv/bin/python results_need_v2/waiter_pattern_probe.py
"""
from __future__ import annotations

import os
import re
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PATTERN_SOURCE = re.compile(r"pgrep -f '([^']+)'")
SELF_ARGV0 = "waiter_pattern_probe.py"


def patterns_from(path: str) -> list[str]:
    with open(path, encoding="utf-8") as handle:
        return PATTERN_SOURCE.findall(handle.read())


def uptime_seconds() -> float:
    with open("/proc/uptime", encoding="utf-8") as handle:
        return float(handle.read().split()[0])


def age_seconds(pid: int, boot_uptime: float) -> float:
    """Wall-clock age from /proc/<pid>/stat field 22 (start_time in clock ticks since boot)."""
    with open(f"/proc/{pid}/stat", encoding="utf-8", errors="replace") as handle:
        after_cmd = handle.read().rsplit(") ", 1)[1]
    start_ticks = float(after_cmd.split()[19])
    return boot_uptime - start_ticks / os.sysconf("SC_CLK_TCK")


def live_processes(boot_uptime: float, min_age: float) -> list[tuple[int, str, float]]:
    out = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        try:
            with open(f"/proc/{entry}/cmdline", "rb") as handle:
                raw = handle.read()
            age = age_seconds(pid, boot_uptime)
        except (OSError, IndexError, ValueError):
            continue
        cmd = raw.replace(b"\0", b" ").decode("utf-8", "replace").strip()
        if cmd and age >= min_age:
            out.append((pid, cmd, age))
    return out


def kind_of(cmd: str) -> str:
    if "run_need_v2.py" in cmd and "--seeds" in cmd:
        return "真实批次"
    if "run_need_v2_chunks.sh" in cmd:
        return "跑批外壳"
    if "wait_batch_then_fulltests" in cmd:
        return "等待器自身"
    if "post_batch_pipeline" in cmd:
        return "收尾器自身"
    if SELF_ARGV0 in cmd:
        # 只有真正在跑本探针的那个 python 进程算"自己"。02:41 实测把这条写宽过一次：
        # 启动本探针的那层 `bash -lc` 命令行里同样含有脚本名，于是它被标成"本探针"而像是
        # 无辜的——可它的命令行里同时还含有等待器的匹配串字面量，正是等待器会误认成批次的那种进程。
        if cmd.startswith(".venv/bin/python") or cmd.startswith(sys.executable):
            return "本探针"
        return "启动本探针的外壳（命令行含匹配串字面量）"
    return "其它长期进程"


def main() -> int:
    scripts = {"等待器": os.path.join(ROOT, "results_need_v2", "wait_batch_then_fulltests.sh"),
               "收尾器": os.path.join(ROOT, "results_need_v2", "post_batch_pipeline.sh")}
    sleep_seconds = {"等待器": 60, "收尾器": 120}
    # 「被等的对象」不算污染：收尾器的第二条循环就是设计来等等待器退出的。
    # 第一版把等待器自己也报成问题，那正是把被等待对象当成核对失败——判据本身的缺陷，不是链路的缺陷。
    expected = {"等待器": {"真实批次", "跑批外壳"},
                "收尾器": {"真实批次", "跑批外壳", "等待器自身"}}
    boot = uptime_seconds()
    problems: list[str] = []
    for label, path in scripts.items():
        # 只看活过一轮轮询间隔的进程：短于一轮的命令行不可能把循环卡死。
        procs = live_processes(boot, sleep_seconds[label])
        pats = patterns_from(path)
        print(f"[{label}] {os.path.relpath(path, ROOT)}：匹配串 {pats}，"
              f"轮询间隔 {sleep_seconds[label]}s，存活 ≥{sleep_seconds[label]}s 的进程 {len(procs)} 个")
        if not pats:
            problems.append(f"{label}脚本里抽不到 pgrep -f 匹配串：无事可核对，不算通过")
            continue
        for pat in pats:
            rx = re.compile(pat)
            hits = [(pid, cmd, age) for pid, cmd, age in procs if rx.search(cmd)]
            for pid, cmd, age in hits:
                kind = kind_of(cmd)
                print(f"    {pat!r} 命中 pid={pid} 龄 {age:.0f}s {kind} | {cmd[:100]}")
                if kind not in expected[label]:
                    problems.append(f"{label}的 {pat!r} 会命中 {kind}（pid={pid}，已活 {age:.0f}s）")
            if not hits:
                print(f"    {pat!r} 命中 0 个长期进程")

    print()
    if problems:
        print(f"问题 = {len(problems)} 处：")
        for p in problems:
            print(f"  - {p}")
        print("含义：这些进程若在批次退出时仍活着，等待循环就永远看见「批次还在跑」，判定不会自动生成。")
        return 1
    print("问题 = 无：两条等待循环只命中真实批次与跑批外壳；批次一退出，链就会在一轮间隔内放行。"
          f"（判据是「活过自己的轮询间隔」，{time.strftime('%F %T')} 的 /proc 快照）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
