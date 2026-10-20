#!/usr/bin/env python3
"""Check that README quotes each frozen verdict verbatim — and claims nothing before it exists.

The verdicts are computed by `analyze_semantic_need.py` (v1, I/J/K) and `analyze_need_v2.py`
(v2, I2/J2/K2). Hand-copied numbers drift and paraphrases turn a negative result into a softer one,
so this guard compares README against the verdict files line by line:

    ../.venv/bin/python verdict_readme_guard.py     # from results_semantic_need/, or any cwd
    ../.venv/bin/python verdict_readme_guard.py README.selftest.md   # check a spliced copy instead

While a verdict is absent the guard fails if README already reads as if that verdict existed.
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, os.pardir)
README = os.path.abspath(sys.argv[1]) if len(sys.argv) > 1 else os.path.join(ROOT, "README.md")
TITLES = ("## 7B.", "## 7C.", "## 9.")
# 自检要在「判定已存在」这条分支上跑搬运链，但绝不能把合成样本写成 FROZEN_VERDICT_V2.md——那个名字
# 一旦有文件，交付时就分不清它是判定还是自测样本。只有显式设了这个环境变量才改路径，正常跑仍是写死的真名。
V2_VERDICT = os.environ.get("VERDICT_V2_SELFTEST") or os.path.join(ROOT, "results_need_v2", "FROZEN_VERDICT_V2.md")
CHECKS = (
    {"name": "v1（I/J/K）", "verdict": os.path.join(HERE, "FROZEN_VERDICT.md"),
     "arm": "J − I =", "body": "## 7B.", "doc": "FROZEN_VERDICT.md"},
    {"name": "v2（I2/J2/K2）", "verdict": V2_VERDICT,
     "arm": "J2 − I2 =", "body": "## 7C.", "doc": "FROZEN_VERDICT_V2.md"},
)


def sections() -> dict:
    """Return the text of every §7B / §7C / §9 block, each ending at the next tracked heading."""
    with open(README, encoding="utf-8") as handle:
        lines = handle.read().splitlines()
    starts = {}
    for prefix in TITLES:
        hit = next((i for i, line in enumerate(lines) if line.startswith(prefix)), None)
        if hit is None:
            raise SystemExit(f"README 里找不到标题 {prefix}：章节被改名了，先把本脚本的 TITLES 一起改掉。")
        starts[prefix] = hit
    bounds = sorted(starts.values())
    out = {}
    for prefix, i in starts.items():
        nxt = min((b for b in bounds if b > i), default=len(lines))
        out[prefix] = "\n".join(lines[i:nxt])
    return out


def required_lines(verdict: str, arm: str) -> list:
    """Verdict lines that must be copied without editing: the main criterion and the test-seed table."""
    with open(verdict, encoding="utf-8") as handle:
        text = handle.read()
    lines = [line.strip() for line in text.splitlines()]
    head = next((line for line in lines
                 if line.startswith("- 判据（") and arm in line), None)
    if head is None:
        raise SystemExit(f"判定文件里找不到主判据那一行（以「- 判据（」开头且含「{arm}」）：先查生成脚本。")
    out = [head]
    for line in lines[lines.index(head) + 1:]:
        if line.startswith(("- 幅度：", "- 差距：", "- 低分位：", "- 踩线检查：")):
            out.append(line)
        else:
            break
    table_at = next((i for i, line in enumerate(lines) if line.startswith("| 指标（")), None)
    if table_at is None:
        raise SystemExit(f"判定文件里找不到次要指标表（表头应以「| 指标（」开头）。")
    for line in lines[table_at:]:
        if not line.startswith("|"):
            break
        out.append(line)
    return out


def check(secs: dict, cfg: dict) -> int:
    verdict, arm = cfg["verdict"], cfg["arm"]
    body, foot = secs[cfg["body"]], secs["## 9."]
    if not os.path.exists(verdict):
        if arm in body or arm in foot:
            print(f"FAIL：{cfg['name']} 的判定文件还没落盘，README 已经写了它的主判据——这就是凭空宣称。")
            return 1
        if cfg["doc"] not in body:
            print(f"FAIL：{cfg['name']} 判定尚未产出，但 {cfg['body']} 没有任何一处点名 {cfg['doc']} 说明状态。")
            return 1
        print(f"OK（{cfg['name']} 判定未产出）：{verdict} 不存在，README 也没有冒充判定。")
        return 0

    lines = required_lines(verdict, arm)
    head = lines[0]
    bad = [line for line in lines if line not in body]
    if head not in foot:
        bad.append(head + "   [§9 缺少主判据原文]")
    if bad:
        print(f"FAIL：{cfg['name']} 有 {len(bad)}/{len(lines) + 1} 行没有原样出现在 README"
              f"（主判据要同时出现在 {cfg['body']} 与 §9，次要指标表出现在 {cfg['body']}）：")
        for line in bad:
            print("  " + (line[:160] + " …" if len(line) > 160 else line))
        return 1
    print(f"OK（{cfg['name']}）：判定原文 {len(lines) + 1} 行已逐行出现在 {cfg['body']}"
          "（含次要指标表）与 §9（主判据），没有改写。")
    return 0


def main() -> int:
    secs = sections()
    return sum(check(secs, cfg) for cfg in CHECKS)


if __name__ == "__main__":
    raise SystemExit(main())
