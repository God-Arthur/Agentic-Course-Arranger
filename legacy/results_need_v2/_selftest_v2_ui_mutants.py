"""Can ``tests/test_app_need_v2_tab.py`` fail? Re-introduce four UI defects and require the named check to catch each.

The v2 expander's green run alone proves nothing about the assertions behind it, so this probe
mutates the *rendering* decisions the test is supposed to police — the two value-column branches
that a mixed ``False``/``None``/percent column silently destroys (both are defects this page
actually shipped today), the radio's default arm, and the button label that is the app's own
"不进求解器" promise. Each mutant must fail, and fail in the one test method that owns that
promise; a mutant caught by some other check, or catching nothing, is reported as a defect of this
probe rather than a pass.

Runs against a copy of ``app.py`` written next to it (``V2_UI_APP``), never by editing the real
file, so a batch run or a browser session cannot observe a mutated app. Solver-free and
network-free like the test it drives: the whole probe is ~15 s of single-core wall clock.

Run: ``.venv/bin/python results_need_v2/_selftest_v2_ui_mutants.py``
"""
from __future__ import annotations

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(ROOT, "app.py")
PROBE = os.path.join(ROOT, "_app_v2_mutant_probe.py")
TEST = "tests.test_app_need_v2_tab"
NONE_BRANCH = '"不适用" if value is None else'
BOOL_BRANCH = '("是" if value else "否") if isinstance(value, bool)'
RULE_FIRST = 'V2_ARMS = {"I2 规则解析（离线，无 API 成本）": "rule",\n                   "J2 AI 解析（调用 LLM）": "llm",'
LLM_FIRST = 'V2_ARMS = {"J2 AI 解析（调用 LLM）": "llm",\n                   "I2 规则解析（离线，无 API 成本）": "rule",'
LABEL = '"只用 v2 解析器重算需求模型（不进求解器、不重跑两轮）"'

# (name, old, new, owning test method, marker proving the stated reason)
MUTANTS = [
    ("K2 的 None 退回裸值（读者会读成 0 分）", NONE_BRANCH, '"%s" % value if value is None else',
     "test_truth_arm_has_no_natural_language_goals_by_contract", "K2 的 goals_per_student 印成了"),
    ("布尔列退回恒是（今天真犯过的错）", BOOL_BRANCH,
     '("是" if isinstance(value, bool) else "否") if isinstance(value, bool)',
     "test_truth_arm_has_no_natural_language_goals_by_contract", "AssertionError: '是' != '否'"),
    ("默认臂改成 J2（静默偷花批次配额）", RULE_FIRST, LLM_FIRST,
     "test_default_selection_is_the_offline_rule_arm", "!= 'I2 规则解析（离线，无 API 成本）'"),
    ("抹掉按钮上的「不进求解器」承诺", LABEL, '"只用 v2 解析器重算需求模型"',
     "test_expander_declares_that_it_does_not_re_solve", "'不进求解器' not found in"),
]

FAIL_LINE = re.compile(r"^(test_\w+).*\.\.\. (FAIL|ERROR)$", re.MULTILINE)


def _run(app_path: str) -> tuple:
    env = dict(os.environ, V2_UI_APP=app_path, PYTHONHASHSEED="0")
    proc = subprocess.run([sys.executable, "-m", "unittest", "-v", TEST],
                          capture_output=True, text=True, cwd=ROOT, env=env, timeout=900)
    out = proc.stdout + proc.stderr
    return proc.returncode, out, sorted({m.group(1) for m in FAIL_LINE.finditer(out) if m.group(2) == "FAIL"})


def main() -> int:
    source = open(APP, encoding="utf-8").read()
    problems = []
    try:
        # Control leg: the copy path must be faithful, otherwise "mutant caught" means nothing.
        open(PROBE, "w", encoding="utf-8").write(source)
        rc, out, failed = _run(PROBE)
        if rc != 0:
            problems.append(f"对照腿失败：未变异的 app.py 副本没有跑绿（退出码 {rc}，失败 {failed}）")
        else:
            print("对照腿：未变异的副本 8 例 OK（退出码 0）")

        for name, old, new, owner, marker in MUTANTS:
            if source.count(old) != 1:
                problems.append(f"{name}: 变异锚点在 app.py 里出现 {source.count(old)} 次，锚点已失效")
                continue
            mutated = source.replace(old, new)
            if mutated == source:
                problems.append(f"{name}: 变异没有改动文本，这条检查是空的")
                continue
            open(PROBE, "w", encoding="utf-8").write(mutated)
            rc, out, failed = _run(PROBE)
            if rc == 0:
                problems.append(f"{name}: 变异后测试仍然全绿——那条承诺没有被看守")
            elif owner not in failed:
                problems.append(f"{name}: 失败了，但归属错：期望 {owner}，实际 {failed}")
            elif len(failed) != 1:
                problems.append(f"{name}: 由 {owner} 抓到，但不止一处失败：{failed}")
            elif marker not in out:
                problems.append(f"{name}: {owner} 失败了，却没说出原因「{marker}」")
            else:
                print(f"抓到：{name} → {owner}（原因标记「{marker}」）")
    finally:
        if os.path.exists(PROBE):
            os.remove(PROBE)
    if open(APP, encoding="utf-8").read() != source:
        problems.append("app.py 与探针开始时不一致——探针写坏了生产文件")

    if problems:
        print(f"\n问题 = {len(problems)} 条")
        for line in problems:
            print("  ✗", line)
        return 1
    print("\n问题 = 无：4 个变异全部由预期的那条检查、以预期原因抓到；副本对照腿绿；app.py 未被改动。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
