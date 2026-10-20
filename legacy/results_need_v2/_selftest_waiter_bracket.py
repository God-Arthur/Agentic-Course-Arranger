#!/usr/bin/env python3
"""Prove the waiters' pgrep patterns still see what they wait for, and no longer see themselves.

`waiter_pattern_probe.py` judges a live /proc snapshot. Once the batch has exited there is nothing to
match, so "0 hits" is a pass over an empty population — it cannot tell a bracketed pattern from the
unbracketed one that deadlocked the chain at 01:57. This check needs no processes at all: `pgrep -f`
is a regex search over a command line, so both halves are testable synthetically.

Three sides, all required:
  self-match — no pattern may match a command line that merely carries that pattern's own text, which
               is what happens when a waiter is launched through `bash -c` and its loop line lands in
               the parent shell's argv.
  dead weight — each pattern must recognise at least one of the command lines that script waits for.
  coverage — together a script's patterns must see every one of its own targets. The 收尾器 waits for
               the 等待器 as well as the batch, so the target set is per-script; asking one script's
               pattern to match another script's target is a bug in this file, not in the chain.

The pattern list is imported from the real probe, so this cannot drift from what the scripts run.
Two mutants, each of which MUST break something: restoring the pre-fix literal has to reintroduce the
self-match, and an over-tightened bracket class has to go blind. If either mutant survives, this file
has no discriminating power and says so instead of passing.

Read-only: no solver, no network, no writes.

    .venv/bin/python results_need_v2/_selftest_waiter_bracket.py
"""
from __future__ import annotations

import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from waiter_pattern_probe import ROOT, patterns_from  # noqa: E402  (reuse the real reader)

BATCH = ".venv/bin/python run_need_v2.py --seeds 7-26 --n-students 72 --out results_need_v2/chunk_a"
CHUNK = "bash results_need_v2/run_need_v2_chunks.sh"
WAITER = "bash results_need_v2/wait_batch_then_fulltests.sh"

# relpath -> the command lines that script's loop is supposed to wait on.
SCRIPTS = {
    "results_need_v2/wait_batch_then_fulltests.sh": {"真实批次": BATCH, "跑批外壳": CHUNK},
    "results_need_v2/post_batch_pipeline.sh": {"真实批次": BATCH, "跑批外壳": CHUNK, "等待器自身": WAITER},
}


def carrier(pattern: str) -> str:
    """A launcher shell whose argv carries the loop line verbatim — the deadlock shape.

    Deliberately contains no target name: only the pattern's own text. A pattern that matches this is
    seeing its own writing rather than the batch. The 收尾器 does have to match the 等待器's path, so
    the path strings live in the per-script forbidden set below, not here.
    """
    return f"bash -c while pgrep -f '{pattern}' >/dev/null 2>&1; do sleep 60; done"


def audit(rel: str, pats: list[str], targets: dict, own_path: str) -> list[str]:
    """A pattern may match its own targets and nothing built from its own writing.

    Not modelled as forbidden, because a regex cannot tell them apart: a wrapper shell that mentions
    the path of the process being waited on carries the same bytes as that process itself. The chain's
    protection there is `waiter_pattern_probe.py`, which only counts such a line as a problem when it
    outlives one poll interval.
    """
    out = []
    covered = set()
    forbidden = {carrier(p) for p in pats} | {f"bash -c setsid nohup {own_path} >/dev/null 2>&1 &"}
    for pat in pats:
        rx = re.compile(pat)
        hits = {label for label, cmd in targets.items() if rx.search(cmd)}
        covered |= hits
        print(f"    {pat!r} 认出 {len(hits)}/{len(targets)} 个目标：{'、'.join(sorted(hits)) or '无'}")
        if not hits:
            out.append(f"{rel} 的 {pat!r} 认不出任何被等对象（写瞎了，循环永远看不见批次）")
        for cmd in sorted(forbidden):
            if rx.search(cmd):
                out.append(f"{rel} 的 {pat!r} 会命中「不属于被等对象」的命令行：{cmd[:70]}")
    blind = set(targets) - covered
    if blind:
        out.append(f"{rel} 的匹配串合起来看不见：{sorted(blind)}")
    return out


def main() -> int:
    if not patterns_from(os.path.join(ROOT, "results_need_v2", "wait_batch_then_fulltests.sh")):
        print("FAIL：从等待器脚本里抽不到匹配串，无事可核对，不算通过")
        return 1
    problems: list[str] = []
    for rel, targets in SCRIPTS.items():
        pats = patterns_from(os.path.join(ROOT, rel))
        print(f"[{os.path.basename(rel)}] 匹配串 {pats}，目标 {len(targets)} 个")
        problems.extend(audit(rel, pats, targets, rel))

    print("\n变异：")
    pre_fix = "run_need_v2_chunks.sh"
    fires_self = bool(re.compile(pre_fix).search(carrier(pre_fix)))
    print(f"  改前的字面量 {pre_fix!r} → 自我命中 = {fires_self}（应为 True，否则本核对没有区分力）")
    blind_pat = "[r]un_need_v2_chunxks.sh"
    fires_blind = not any(re.compile(blind_pat).search(cmd) for cmd in (BATCH, CHUNK, WAITER))
    print(f"  收得过紧的括号类 {blind_pat!r} → 一个目标都认不出 = {fires_blind}（应为 True）")
    if not fires_self:
        problems.append("变异「改前字面量」没有重新产生自我命中 → 自我命中这一核对是空转")
    if not fires_blind:
        problems.append("变异「过紧的括号类」仍认得出目标 → 认不出目标这一核对是空转")

    print()
    if problems:
        print(f"问题 = {len(problems)} 处：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("OK：两条等待循环的每个匹配串都仍认出自己的被等对象，且都不再命中携带自身字面量的命令行；"
          "两个变异（改前字面量、过紧括号类）都被指名抓到。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
