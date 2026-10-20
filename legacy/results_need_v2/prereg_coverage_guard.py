#!/usr/bin/env python3
"""核对「预注册承诺要报告的每一项指标，判定文件里真的印出来了」。

文档与代码之间靠人眼对齐过一次：2026-10-18 00:55 发现 §4 写了"重要需求识别召回"，而 `analyze_need_v2.py`
的次要指标表从来没有印它。补上之后，**没有任何一条已有守卫会保持这个对齐**——`need_v2_number_guard.py` 管数字
能不能追溯，`verdict_readme_guard.py` 管判定与正文是否逐字一致，`_selftest_analyze_contract.py` 管列在不在。
以后任何一方改动都可能再漏印，而漏印是静默的：表格少一行、少一句披露，报告照样能自洽地发布。

分工：
- §4 的承诺列表是**运行时解析**的，不是抄写在本文件里的。改文档措辞 → 本文件立刻报"映射表跟不上"，
  逼人来更新映射；从文档里删掉一项承诺同样会报错，所以不能用"改文档"来绕过检查。
- "承诺在判定里叫什么名字" 是本文件里的映射表（因为报告印的是全称加单位，文档写的是简称）。映射只允许变严。

    .venv/bin/python results_need_v2/prereg_coverage_guard.py                      # 批次跑完后核对真判定
    .venv/bin/python results_need_v2/prereg_coverage_guard.py --verdict <合成样本>  # 自检：核对生产者的真实输出
"""
from __future__ import annotations

import argparse
import os
import re

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# §4 次要判据的措辞 → 判定 §2 表格行首列必须出现的子串（键必须逐字出现在文档里）。
SECONDARY = {
    "需求识别 F1": "需求识别 F1(%)",
    "重要需求识别召回": "重要需求识别召回(%)",
    "重要需求最终满足率": "重要需求最终满足率(%)",
    "p10 学生效用": "p10 学生效用",
}
# §4「以及 §1 的新字段指标（…）」括号里的简称 → §3 表格必须出现的子串（多行时要求全部出现）。
FIELDS = {
    "点名识别率": ("点名第一志愿识别率(%)",),
    "替代项正确率": ("可接受替代项判断正确率(%)",),
    "类别纠正率": ("自称明确要求被规则纠正数", "类别虚报率(%)"),
    "类别与真值一致性": ("一般偏好行命中率(%)", "仍有歧义行命中率(%)"),
}
# 固定要求：来源标在括号里，全部是文档原句或任务书 §四 的点名指标，不是本文件自创的。
FIXED = (
    ("§4 主判据的比较对象", "J2 − I2"),
    ("§4 主判据的检验方式", "双侧精确符号检验"),
    ("§4 主判据的最终总效用", "| 最终总效用 |"),
    ("§4 功效声明的措辞", "本设计测不出差异"),
    ("§4 停止规则", "按停止规则不追加种子、不换指标"),
    ("§4 唯一允许的行剔除要披露", "- 被判不可用的 J2 行："),
    ("§1 承诺的学习目标可核对性", "学习目标被规则拒绝率(%)"),
    ("§1 承诺的学习目标覆盖真值", "学习目标覆盖真值主题率(%)"),
    ("§5 风险 2 的求解器边界", "- 求解器可重复性的边界"),
    ("任务书 §四 要求的运行成本", "- J2 组：LLM 调用"),
    ("任务书 §四 要求的缓存复用", "- 缓存复用实测："),
    ("任务书 §四 要求的低分位效用", "- 低分位：p10 学生效用差"),
    ("§6 交付要求写明未验证", "- 未验证"),
)


def _read(path: str) -> str:
    if not os.path.exists(path):
        raise SystemExit(f"FAIL：文件不存在（{path}）。")
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _bullet(text: str, label: str) -> str:
    """Body of the `- **label**……` bullet, continuation lines joined (the doc wraps at ~100 chars)."""
    lines = [line.strip() for line in text.splitlines()]
    for i, line in enumerate(lines):
        if line.startswith(f"- **{label}"):
            body = [line]
            for nxt in lines[i + 1:]:
                if not nxt or nxt.startswith(("- ", "## ", "|", ">", "1.")):
                    break
                body.append(nxt)
            return " ".join(body)
    raise SystemExit(
        f"FAIL：预注册文档里找不到以「- **{label}」开头的条目。§4 的形状变了，本守卫的解析会退化成"
        "「什么都没承诺」的空转，所以拒绝执行——先按新形状改本文件，再谈判定。")


def parse_prereg(path: str) -> dict:
    text = _read(path)
    main = _bullet(text, "主判据")
    sec_body = _bullet(text, "次要判据").partition("**：")[2]
    if not sec_body:
        raise SystemExit("FAIL：「- **次要判据**」条目里没有「**：」，取不到承诺列表。")
    paren = re.search(r"（([^）]*)）", sec_body)
    fields = []
    if paren:
        fields = [item.strip(" 。") for item in paren.group(1).split("、") if item.strip(" 。")]
        sec_body = sec_body[:paren.start()] + sec_body[paren.end():]
    pointer = [item for item in sec_body.split("、") if item.strip().startswith("以及")]
    secondary = [item.strip(" 。") for item in sec_body.split("、")
                 if item.strip() and not item.strip().startswith("以及")]
    secondary, fields = _clean(secondary), _clean(fields)
    thr = re.search(r"至少 (\d+) 胜", main)
    seeds = re.search(r"独立测试情景：种子 (\d+)-(\d+)，共 (\d+) 个", text)
    if not thr or not seeds:
        raise SystemExit("FAIL：预注册 §4 取不到「至少 N 胜」门槛，或 §3 取不到「种子 A-B，共 N 个」——"
                         "这两条是主判据可核对的前提，解析不到就不许跑覆盖检查。")
    if len(secondary) < 4 or (pointer and len(fields) < 4):
        raise SystemExit(f"FAIL：§4 承诺列表只解析到 次要判据 {secondary} / 新字段 {fields}。"
                         f"少于既有的 4 + 4 项说明解析被换行或改字打断了，宁可拒绝执行，"
                         "也不能让「少承诺了几项」变成少检查几项。")
    return {"secondary": secondary, "fields": fields, "threshold": int(thr.group(1)),
            "seeds": seeds.groups()}


def _clean(items):
    out = []
    for item in items:
        item = re.sub(r"\s+", " ", item).strip(" 。；;，,")
        if item:
            out.append(item)
    return out


def test_head(text: str) -> str:
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- 判据（") and "独立测试种子" in s:
            return s
    raise SystemExit("FAIL：判定文件里找不到以「- 判据（独立测试种子」开头的主判据行——"
                     "生产者改了措辞，覆盖检查的落点就没了。")


def evaluate(doc: dict, text: str, verdict_name: str, prereg_name: str) -> int:
    """Coverage check on a verdict that is already in memory — a file on disk or `build()`'s return.

    Split out so a real-data dry run can reuse exactly these items without writing a partial verdict
    to disk under a name that could be mistaken for the deliverable.
    """
    head = test_head(text)
    missing, stale = [], []
    for phrase in doc["secondary"]:
        target = SECONDARY.get(phrase)
        if target is None:
            missing.append(f"§4 新增了次要判据「{phrase}」，映射表里没有它：要么补映射并确认已印出，"
                           "要么文档写错了简称")
        elif target not in text:
            missing.append(f"§4 承诺「{phrase}」，判定里没有「{target}」这一行")
    for phrase in doc["fields"]:
        targets = FIELDS.get(phrase)
        if targets is None:
            missing.append(f"§4 括号里新增了「{phrase}」，映射表里没有它")
        else:
            missing += [f"§4 承诺新字段「{phrase}」，判定里没有「{want}」" for want in targets
                        if want not in text]
    for phrase in list(SECONDARY) + list(FIELDS):
        if phrase not in doc["secondary"] + doc["fields"]:
            stale.append(f"映射表还写着「{phrase}」，但 §4 已经不再承诺它——删映射等于悄悄放弃一项承诺")
    lo, hi = doc["seeds"][0], doc["seeds"][1]
    label = f"独立测试种子 {lo}-{hi}"
    if label not in head:
        missing.append(f"§3 划定的测试种子范围 {label} 没出现在主判据行里")
    if f"门槛 ≥{doc['threshold']} 胜" not in head:
        missing.append(f"§4 冻结的显著门槛是至少 {doc['threshold']} 胜，主判据行里不是这个数")
    if f"测试种子 {doc['seeds'][2]} 个" not in text:
        missing.append(f"§3 要求 {doc['seeds'][2]} 个测试种子，判定里没写出这个数")
    for name, want in FIXED:
        if want not in text:
            missing.append(f"{name}：判定里找不到「{want}」")

    n_items = len(doc["secondary"]) + len(doc["fields"]) + len(FIXED) + 3
    for line in missing + stale:
        print("  FAIL " + line)
    if missing or stale:
        print(f"问题 = {len(missing) + len(stale)}：§4 承诺的指标与 {n_items} 条要求中，"
              f"有 {len(missing)} 条没印出、{len(stale)} 条映射已失效（{verdict_name}）")
        return 1
    print(f"问题 = 无：§4 承诺 {len(doc['secondary'])} 项次要判据 + {len(doc['fields'])} 项新字段指标，"
          f"连同主判据门槛与 {len(FIXED)} 条固定要求，共 {n_items} 条全部出现在 "
          f"{verdict_name}（承诺列表取自 {prereg_name} §4 的实时解析）")
    return 0


def check(prereg: str, verdict: str) -> int:
    return evaluate(parse_prereg(prereg), _read(verdict),
                    os.path.basename(verdict), os.path.basename(prereg))


def main() -> int:
    ap = argparse.ArgumentParser(description="核对预注册承诺的指标是否都印进了判定")
    ap.add_argument("--prereg", default=os.path.join(HERE, "PREREGISTRATION_NEED_V2.md"))
    ap.add_argument("--verdict", default=os.path.join(HERE, "FROZEN_VERDICT_V2.md"))
    args = ap.parse_args()
    if not os.path.exists(args.verdict):
        print(f"FAIL：判定文件还不存在（{args.verdict}）——批次跑完并执行 analyze_need_v2.py 之后再核对。")
        return 1
    return check(args.prereg, args.verdict)


if __name__ == "__main__":
    raise SystemExit(main())
