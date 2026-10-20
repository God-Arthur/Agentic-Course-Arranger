#!/usr/bin/env python3
"""证明 `need_v2_number_guard.py` 的耗时核对真的会失败——每次改写 §7C 那条成本句之后跑一遍。

做法：把真 README 复制成仓库内的临时副本，逐条注入一处错误（或一处改写），用子进程对副本跑守卫；
守卫必须对每个副本返回非 0 并打出对应的 FAIL 句。真 README 自己必须先过，否则先修正文。
副本跑完立即删除，不在仓库里留第二份"看起来像交付物"的文件。

每条变异还检查它是否真的改动过文字：一条不改字的变异证明不了任何事，这正是空核对的另一种形态。

**变异要注入的数是从正文里读出来的，不是写死在本文件里的。** 00:35 那次实测就是反面教材：§7C 的成本句
从"2 颗种子 / 3.74"换成"7 颗种子 / 3.86 / 8.09"之后，5 条里有 3 条成了空操作——那个数字在正文里已经
不存在，replace 找不到落点，返回原文，守卫自然"通过"。写死的变异会随正文换数而静默失效，而这套守卫存在的
理由恰恰是防止 §7C 的数字无出处。现在改用守卫自己的两条正则去正文里取数；取不到就以非 0 拒绝执行。

    .venv/bin/python results_need_v2/_selftest_number_guard.py
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
GUARD = os.path.join(HERE, "need_v2_number_guard.py")
# 开发机仍然用仓库里的 .venv；镜像里没有 .venv，就用当前解释器，检查内容不变。
VENV_PY = os.path.join(ROOT, ".venv/bin/python")
PY = VENV_PY if os.path.exists(VENV_PY) else sys.executable
README = os.path.join(ROOT, "README.md")
COPY = os.path.join(HERE, "_selftest_readme_mutant.md")
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)
from need_v2_number_guard import MIN_PER_SEED, RATE_TO_HOURS  # noqa: E402


def build_mutants(text: str):
    """Derive every injection point from the cost sentences currently in §7C."""
    costs = MIN_PER_SEED.findall(text)
    rates = RATE_TO_HOURS.findall(text)
    if not costs or not rates:
        raise SystemExit(
            f"FAIL：正文里抓不到可注入的耗时句（「分钟/种子」路径 {len(costs)} 条、速率路径 {len(rates)} 条）。"
            "变异没有落点，本自检会退化成什么都不检查的空转——先确认 §7C 的两条路径还在、且与本文件复用的"
            "守卫正则一致。")
    per_seed, hours = costs[0]
    rate, profiles, _again, rate_hours = rates[0]
    bigger_hours = f"{float(hours) + 1.0:.2f}"
    drift_hours = f"{float(hours) + 0.01:.2f}"      # 00:31 被退回的就是这种末位漂移
    bad_profiles = str(int(profiles) + 2)
    bumped_rate = f"{float(rate) + 0.01:.2f}"
    return [
        ("整批小时数被改大 1 小时",
         lambda t: t.replace(f"分钟/种子 ⇒ 整批 {hours} 小时", f"分钟/种子 ⇒ 整批 {bigger_hours} 小时", 1)),
        ("整批小时数末位漂移（复刻 00:31 那次 8.10 对 8.09）",
         lambda t: t.replace(f"分钟/种子 ⇒ 整批 {hours} 小时", f"分钟/种子 ⇒ 整批 {drift_hours} 小时", 1)),
        ("档案总数与分块计划不符",
         lambda t: t.replace(f"个档案/分钟 ⇒ {profiles} ÷", f"个档案/分钟 ⇒ {bad_profiles} ÷", 1)),
        ("速率句里两个速率写成了不同的数",
         lambda t: t.replace(f"{rate} 个档案/分钟 ⇒ {profiles} ÷ {rate}",
                             f"{rate} 个档案/分钟 ⇒ {profiles} ÷ {bumped_rate}", 1)),
        ("两条耗时句整条删掉", lambda t: "\n".join(
            line for line in t.splitlines() if "分钟/种子 ⇒" not in line and "个档案/分钟 ⇒" not in line)),
        ("分钟/种子那句被改写到正则不认识", lambda t: t.replace("分钟/种子 ⇒", "分钟一种子 →", 1)),
    ]


def run(path: str) -> tuple[int, str]:
    done = subprocess.run([PY, GUARD, path],
                          capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def main() -> int:
    with open(README, encoding="utf-8") as handle:
        text = handle.read()
    rc, out = run(README)
    if rc != 0:
        print("FAIL：真 README 自己就过不了守卫，先修正文再谈变异测试。")
        print(out.strip())
        return 1
    print(f"真 README 通过：{[line for line in out.splitlines() if line.startswith('问题')][-1]}")
    mutants = build_mutants(text)
    bad = 0
    try:
        for name, mutate in mutants:
            mutated = mutate(text)
            if mutated == text:
                print(f"FAIL：变异「{name}」一个字都没改——这条变异本身是空的，证明不了检查有效。")
                bad += 1
                continue
            with open(COPY, "w", encoding="utf-8") as handle:
                handle.write(mutated)
            rc, out = run(COPY)
            hits = [line for line in out.splitlines() if "FAIL" in line]
            if rc == 0:
                print(f"FAIL：变异「{name}」没被守卫抓到——这条检查在空转。")
                bad += 1
            else:
                print(f"OK：变异「{name}」被抓到 → {hits[0] if hits else out.strip().splitlines()[-1]}")
    finally:
        if os.path.exists(COPY):
            os.remove(COPY)
    print(f"变异 = {len(mutants)} 条（注入点取自正文自己的耗时句），漏抓 = {bad} 条；副本已删：{not os.path.exists(COPY)}。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
