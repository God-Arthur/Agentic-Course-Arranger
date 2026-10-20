#!/usr/bin/env python3
"""证明 `prereg_coverage_guard.py` 真的会失败——它检查的是"承诺的指标有没有印出来"，而这类检查最容易空转。

漏印的风险是静默的：2026-10-18 00:55 人眼发现 §4 承诺了"重要需求识别召回"、`analyze_need_v2.py` 从来没印它。
补上之后必须有东西钉住它，否则下次改文档或改代码照样会漏。所以本自检同时从**两侧**注入错误：
- 判定侧：删掉一行指标、把标签改成全角括号、删掉成本/低分位/门槛/种子范围——守卫必须逐条喊出对应的原因；
- 文档侧：新增一项承诺（映射表跟不上）、给承诺改名（映射表反过来失效）、删掉一项承诺（解析条数少到触发下限）、
  改门槛数字、把 §4 条目改名（解析不到就拒绝执行）——守卫不许"少承诺就少检查"。

每条变异都要求三件事同时成立：文本确实变了、退出码非 0、输出里出现预期的那句 FAIL。只满足前两条不算抓到：
上一轮搬运链自检就是因为守卫崩在别的原因上报了 3/3 通过，那种绿是假的。

    .venv/bin/python results_need_v2/_selftest_prereg_coverage.py
"""
from __future__ import annotations

import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
# 开发机仍然用仓库里的 .venv；镜像里没有 .venv，就用当前解释器，检查内容不变。
_venv = os.path.join(ROOT, ".venv", "bin", "python")
PY = _venv if os.path.exists(_venv) else sys.executable
GUARD = os.path.join(HERE, "prereg_coverage_guard.py")
CONTRACT = os.path.join(HERE, "_selftest_analyze_contract.py")
PREREG = os.path.join(HERE, "PREREGISTRATION_NEED_V2.md")
SYNTH = os.path.join(HERE, "_selftest_from_analyze_v2.md")
SYNTH_COPY = os.path.join(HERE, "_selftest_from_analyze_v2_mutant.md")
PREREG_COPY = os.path.join(HERE, "_selftest_prereg_mutant.md")


def run(prereg: str, verdict: str) -> tuple[int, str]:
    done = subprocess.run([PY, GUARD, "--prereg", prereg, "--verdict", verdict],
                          capture_output=True, text=True)
    return done.returncode, done.stdout + done.stderr


def drop_line(text: str, needle: str) -> str:
    return "\n".join(line for line in text.splitlines() if needle not in line)


def mutants():
    """(名称, 改哪一侧, 改法, 预期出现在 FAIL 里的那句)。注入点取自两侧文件的现状文字。"""
    v = ("verdict", SYNTH)
    d = ("doc", PREREG)
    return [
        ("判定少印一行「重要需求识别召回」——复刻 00:55 那次漏印", v,
         lambda t: drop_line(t, "重要需求识别召回(%)"), "§4 承诺「重要需求识别召回」"),
        ("判定把「需求识别 F1(%)」改成全角括号", v,
         lambda t: t.replace("需求识别 F1(%)", "需求识别 F1（%）"), "判定里没有「需求识别 F1(%)」"),
        ("判定删掉缓存复用那句披露", v,
         lambda t: drop_line(t, "缓存复用实测"), "任务书 §四 要求的缓存复用"),
        ("判定删掉低分位那一行（测试与开发两处）", v,
         lambda t: drop_line(t, "- 低分位：p10 学生效用差"), "任务书 §四 要求的低分位效用"),
        ("判定的显著门槛被写成 ≥14 胜", v,
         lambda t: t.replace("门槛 ≥15 胜", "门槛 ≥14 胜", 1), "§4 冻结的显著门槛是至少 15 胜"),
        ("判定的测试种子范围被写成 7-25", v,
         lambda t: t.replace("独立测试种子 7-26", "独立测试种子 7-25", 1), "§3 划定的测试种子范围"),
        ("文档新增一项次要判据，映射表还没跟上", d,
         lambda t: t.replace("p10 学生效用、以及", "p10 学生效用、中位学生效用、以及", 1),
         "§4 新增了次要判据「中位学生效用」"),
        ("文档给已承诺的指标改名（映射表反向失效，等于悄悄放弃承诺）", d,
         lambda t: t.replace("、重要需求识别召回、", "、重要需求识别召回率、", 1),
         "映射表还写着「重要需求识别召回」"),
        ("文档整项删掉一个承诺（解析条数必须触发下限，不许少承诺就少检查）", d,
         lambda t: t.replace("重要需求识别召回、", "", 1), "§4 承诺列表只解析到"),
        ("文档用逗号分隔次要判据（换行/改标点打断解析）", d,
         lambda t: t.replace("重要需求识别召回、重要需求最终满足率、",
                             "重要需求识别召回，重要需求最终满足率，", 1), "§4 承诺列表只解析到"),
        ("文档把显著门槛改成 12 胜（判定仍是 15）", d,
         lambda t: t.replace("至少 15 胜", "至少 12 胜", 1), "§4 冻结的显著门槛是至少 12 胜"),
        ("文档把 §4 条目改名（解析落空，守卫必须拒绝执行而不是当作没承诺）", d,
         lambda t: t.replace("- **次要判据（只做描述", "- **次要指标（只做描述", 1),
         "找不到以「- **次要判据」开头的条目"),
    ]


def main() -> int:
    done = subprocess.run([PY, CONTRACT], capture_output=True, text=True)
    if done.returncode != 0:
        print("FAIL：合成判定没能由 analyze_need_v2.build() 产出，先修生产者再谈覆盖检查。")
        print((done.stdout + done.stderr).strip())
        return 1
    for path in (PREREG, SYNTH):
        if not os.path.exists(path):
            print(f"FAIL：缺少 {path}")
            return 1
    rc, out = run(PREREG, SYNTH)
    if rc != 0:
        print("FAIL：未变异的组合自己就过不了守卫，先修正文/映射表再谈变异测试。")
        print(out.strip())
        return 1
    print("未变异通过：" + [line for line in out.splitlines() if line.startswith("问题")][-1])

    bad, ran = 0, 0
    doc_text = open(PREREG, encoding="utf-8").read()
    syn_text = open(SYNTH, encoding="utf-8").read()
    try:
        for name, (side, src), mutate, marker in mutants():
            base = doc_text if side == "doc" else syn_text
            mutated = mutate(base)
            if mutated == base:
                print(f"FAIL：变异「{name}」一个字都没改——这条变异是空的，证明不了检查有效。")
                bad += 1
                continue
            ran += 1
            prereg, verdict = (PREREG_COPY, SYNTH) if side == "doc" else (PREREG, SYNTH_COPY)
            with open(prereg if side == "doc" else verdict, "w", encoding="utf-8", newline="\n") as h:
                h.write(mutated)
            rc, out = run(prereg, verdict)
            if rc == 0:
                print(f"FAIL：变异「{name}」没被守卫抓到——这条检查在空转。")
                bad += 1
            elif marker not in out:
                print(f"FAIL：变异「{name}」让守卫非 0 退出，但理由是别的（预期含「{marker}」）——"
                      "崩溃不算抓到，必须是对的那条检查失败。")
                print("  " + out.strip().splitlines()[0])
                bad += 1
            else:
                print(f"OK：变异「{name}」按预期被抓到 → {marker}")
    finally:
        for path in (SYNTH_COPY, PREREG_COPY):
            if os.path.exists(path):
                os.remove(path)
    left = [os.path.basename(p) for p in (SYNTH_COPY, PREREG_COPY) if os.path.exists(p)]
    print(f"变异 = {ran} 条（判定侧 {sum(1 for m in mutants() if m[1][0] == 'verdict')}、"
          f"文档侧 {sum(1 for m in mutants() if m[1][0] == 'doc')}），漏抓 = {bad} 条；"
          f"副本已删：{not left}{'（残留 ' + '、'.join(left) + '）' if left else ''}。")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
