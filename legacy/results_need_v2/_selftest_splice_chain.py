#!/usr/bin/env python3
"""Pre-run the whole verdict → README transport chain on the producer's own synthetic output.

批次还要按现在的口径再跑数小时，而判定落盘后只剩两条命令（`verdict_splice.py` 再
`verdict_readme_guard.py`）——这两条不该到早上六点才第一次调试。源文件用的是
`_selftest_from_analyze_v2.md`，即 `analyze_need_v2.build()` 在伪造行上的**真实输出**，所以判定
行格式一旦改动，这里就红，而不是等真判定出来才发现拼接器抓不到标记。

合成样本绝不写成 `FROZEN_VERDICT_V2.md`：守卫的「判定已存在」分支是靠 `VERDICT_V2_SELFTEST`
环境变量走到的，因此自测样本不可能被当成交付判定；本脚本一开始就拒绝在真名已有文件时运行。

三条变异证明核对不是空的：§7C 主判据被改一个数字、§9 主判据被改一个数字（两侧必须逐字一致）、
§7C 次要指标表少一行。每条都要求确实改动过文字。副本写在仓库内、跑完即删。

    .venv/bin/python results_need_v2/_selftest_splice_chain.py
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

PY = os.path.join(ROOT, ".venv", "bin", "python")
CONTRACT = os.path.join(HERE, "_selftest_analyze_contract.py")
SPLICE = os.path.join(ROOT, "verdict_splice.py")
GUARD = os.path.join(ROOT, "results_semantic_need", "verdict_readme_guard.py")
README = os.path.join(ROOT, "README.md")
SYNTH = os.path.join(HERE, "_selftest_from_analyze_v2.md")
REAL_VERDICT = os.path.join(HERE, "FROZEN_VERDICT_V2.md")
COPY = os.path.join(HERE, "_selftest_readme_chain.md")
MUTANT = os.path.join(HERE, "_selftest_readme_chain_mutant.md")
GUARD_OK_V2 = "OK（v2（I2/J2/K2））"


def run(script: str, *args: str) -> tuple[int, str]:
    env = dict(os.environ, VERDICT_V2_SELFTEST=SYNTH) if script == GUARD else os.environ.copy()
    done = subprocess.run([PY, script, *args], capture_output=True, text=True, env=env)
    return done.returncode, done.stdout + done.stderr


def replace_nth(hay: str, old: str, new: str, n: int) -> str:
    """只替换第 n 处（从 1 起）。§7C 与 §9 各有一份主判据，全替换等于两侧同改，
    那样「两侧必须逐字一致」这条核对根本没被碰到。
    `hay.split(old)` 给出 len(occurrences)+1 段，第 n 处正夹在 parts[n-1] 与 parts[n] 之间，
    所以尾段必须从 parts[n:] 接起——写成 parts[n+1:] 会整段吞掉正文（00:51 就是这样把 §8 之后
    全删了，守卫于是因为「找不到标题 ## 9.」而崩，被误当成抓到变异）。"""
    parts = hay.split(old)
    if len(parts) <= n:
        raise SystemExit(f"要替换第 {n} 处「{old[:24]}……」，但全文只出现 {len(parts) - 1} 次："
                         "搬运链的形状变了，先改本自检再谈判定。")
    return old.join(parts[:n]) + new + old.join(parts[n:])


def flip_first_number(line: str) -> str:
    def bump(m: re.Match) -> str:
        digits = m.group(0)
        return digits[:-1] + ("8" if digits[-1] == "9" else str(int(digits[-1]) + 1))

    flipped, count = re.subn(r"\d+(?:\.\d+)?", bump, line, count=1)
    if count == 0 or flipped == line:
        raise SystemExit(f"变异没能改动这一行，这条核对是空的：{line[:60]}")
    return flipped


def main() -> int:
    if os.path.exists(REAL_VERDICT):
        print(f"FAIL：{REAL_VERDICT} 已经存在——判定已落盘，本自检不再往 README 副本里拼合成样本。")
        return 1
    rc, out = run(CONTRACT)
    if rc != 0:
        print("FAIL：判定产出格式自检没通过，搬运链的来源不可信，先修它。")
        print(out.strip()[-1000:])
        return 1

    from verdict_splice import verdict_blocks
    blocks = verdict_blocks(SYNTH, "_V2")
    marks = list(blocks)
    head = blocks["@@VERDICT_MAIN_V2@@"][0]
    table = blocks["@@VERDICT_TABLE_V2@@"]
    with open(README, encoding="utf-8") as handle:
        base = handle.read()

    fails: list[str] = []
    for mark in marks:
        if base.count(mark) != 1:
            fails.append(f"README 的占位符 {mark} 出现 {base.count(mark)} 次，应恰好 1 次")
    if GUARD_OK_V2 in base:
        fails.append("真 README 里已出现 v2 逐行命中句，占位符状态可疑")
    if fails:
        for message in fails:
            print("FAIL：" + message)
        return 1

    print(f"来源：{os.path.basename(SYNTH)} 的 {len(blocks['@@VERDICT_MAIN_V2@@'])} 行主判据 + "
          f"{len(table)} 行次要指标表（合成样本，不得引用）。")
    try:
        with open(COPY, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(base)
        rc, out = run(SPLICE, "--verdict", SYNTH, "--readme", COPY, "--suffix", "_V2")
        if rc != 0 or not os.path.exists(COPY):
            print(f"FAIL：拼接器退出 {rc}：{out.strip()[-400:]}")
            return 1
        print("OK：拼接（--suffix _V2）→ " + out.strip().splitlines()[-1])
        with open(COPY, encoding="utf-8") as handle:
            spliced = handle.read()
        leftover = [mark for mark in marks if mark in spliced]
        if leftover:
            print(f"FAIL：拼接后仍有占位符没被替换：{leftover}")
            return 1
        if spliced.count(head) != 2:
            print(f"FAIL：主判据在拼接后的副本里出现 {spliced.count(head)} 次（应 §7C 与 §9 各一次）。")
            return 1
        rc, out = run(GUARD, COPY)
        if rc != 0 or GUARD_OK_V2 not in out:
            print(f"FAIL：守卫对拼接副本退出 {rc}，或没走「判定已存在」分支：{out.strip()[-500:]}")
            return 1
        print("OK：" + [line for line in out.splitlines() if GUARD_OK_V2 in line][-1])

        mutators = [
            ("§7C 主判据被改一个数字", replace_nth(spliced, head, flip_first_number(head), 1),
             head[:60]),
            ("§9 主判据被改一个数字（与 §7C 不再逐字一致）",
             replace_nth(spliced, head, flip_first_number(head), 2), "§9 缺少主判据原文"),
            ("§7C 次要指标表少一行", replace_nth(spliced, table[-1], "", 1), table[-1]),
        ]
        caught = 0
        for name, mutated, expect in mutators:
            if mutated == spliced:
                print(f"FAIL：变异「{name}」一个字都没改——这条变异本身是空的。")
                continue
            if sum(1 for line in mutated.splitlines() if line.startswith("## 9.")) != 1:
                print(f"FAIL：变异「{name}」把 §9 标题弄没了——它改坏了正文结构，不是一次可用变异。")
                continue
            with open(MUTANT, "w", encoding="utf-8", newline="\n") as handle:
                handle.write(mutated)
            rc, out = run(GUARD, MUTANT)
            hits = [line.strip() for line in out.splitlines() if line.startswith("FAIL")]
            if rc == 0:
                print(f"FAIL：变异「{name}」没被守卫抓到——搬运链的这半边在空转。")
                continue
            if expect not in out:
                print(f"FAIL：变异「{name}」让守卫非 0 退出，但报的不是这一处（应含「{expect[:40]}」）："
                      f"{(hits[0] if hits else out.strip().splitlines()[-1])[:160]}")
                continue
            caught += 1
            print(f"OK：变异「{name}」被抓到 → {hits[0] if hits else out.strip()[-160:]}")
        if caught != len(mutators):
            fails.append(f"变异 {len(mutators)} 条中只抓到 {caught} 条")
    finally:
        for path in (COPY, MUTANT):
            if os.path.exists(path):
                os.remove(path)
    for message in fails:
        print("FAIL：" + message)
    if fails:
        return 1
    print(f"OK：判定 → README 搬运链（拼接 + 双向核对）已在真实产出格式上预跑通过；"
          f"副本已删：{not os.path.exists(COPY) and not os.path.exists(MUTANT)}，"
          f"真判定名仍未创建：{not os.path.exists(REAL_VERDICT)}。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
