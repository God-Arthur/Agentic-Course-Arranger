"""Guard for README §7B: the I/J/K development-set numbers must be recomputed from raw_rows.csv.

Same rule as readme_number_guard.py — nothing in the README body may be typed from memory.
Run without arguments to print the canonical markdown rows; pass --check to compare them with the
README §7B table and fail on any mismatch.
"""
from __future__ import annotations

import datetime
import json
import os
import statistics
import sys
from itertools import takewhile
from typing import Any, Dict, List

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from experiments import load_raw_rows

OUT_DIR = os.path.join(ROOT, "results_semantic_need")
DEV_SEEDS = set(range(1, 7))
ARMS = ("I_规则语义需求解析", "J_AI语义需求解析", "K_真实需求标签参照")
METRICS = (("total_preference_utility", "最终总效用"),
           ("need_f1_pct", "需求识别 F1(%)"),
           ("key_need_recall_pct", "重要需求识别召回(%)"),
           ("key_need_fulfillment_pct", "重要需求最终满足率(%)"),
           ("need_pairs_added", "进入优化器的推断课位"))
CONFIG_KEYS = ("n_students", "question_budget", "questions_per_round", "sim_replicates",
               "solver_workers", "solver_max_seconds", "llm_model", "need_llm_requested")
MARKER = "### 开发集实测（种子 1-6）"


def dev_rows() -> List[Dict[str, Any]]:
    rows, configs = [], {}
    for name in sorted(os.listdir(OUT_DIR)):
        chunk = os.path.join(OUT_DIR, name)
        if not name.startswith("dev_") or not os.path.exists(os.path.join(chunk, "raw_rows.csv")):
            continue
        chunk_rows = load_raw_rows(os.path.join(chunk, "raw_rows.csv"))
        seeds = {int(r["seed"]) for r in chunk_rows}
        if not seeds <= DEV_SEEDS:
            raise SystemExit(f"分块 {name} 含开发集以外的种子 {sorted(seeds)}，禁止混入开发观察。")
        configs[name] = json.load(open(os.path.join(chunk, "summary.json"), encoding="utf-8"))["config"]
        rows.extend(chunk_rows)
    if not rows:
        raise SystemExit("results_semantic_need/ 下没有 dev_* 分块，先跑 run_need_chunks.sh。")
    got = {int(r["seed"]) for r in rows}
    if got != DEV_SEEDS:
        raise SystemExit(f"开发集种子不全：期望 {sorted(DEV_SEEDS)}，实有 {sorted(got)}；"
                         "缺种子的均值不能当作完整开发集写进 README。")
    names = {r["mechanism"] for r in rows}
    dupes = sorted({s for s in got
                    if sum(1 for r in rows if int(r["seed"]) == s) != len(names)})
    if dupes:
        raise SystemExit(f"种子 {dupes} 行数异常（可能有分块被重跑覆盖）：{dupes}")
    ref = next(iter(configs.values()))
    for name, cfg in configs.items():
        for key in CONFIG_KEYS:
            if json.dumps(cfg.get(key), sort_keys=True, default=str) != \
                    json.dumps(ref.get(key), sort_keys=True, default=str):
                raise SystemExit(f"分块 {name} 的 {key} 与其它开发分块不一致，不能合并成一张表。")
    return rows


def means(rows: List[Dict[str, Any]], mech: str, key: str) -> float:
    vals = [r[key] for r in rows if r["mechanism"] == mech and r["status"] == "已运行"
            and isinstance(r.get(key), (int, float))]
    return round(statistics.fmean(vals), 2)


def paired(rows: List[Dict[str, Any]], key: str) -> Dict[str, Any]:
    by_seed = {int(r["seed"]): r for r in rows if r["mechanism"] == "J_AI语义需求解析"}
    base = {int(r["seed"]): r for r in rows if r["mechanism"] == "I_规则语义需求解析"}
    deltas = [(s, by_seed[s][key] - base[s][key]) for s in sorted(set(by_seed) & set(base))
              if isinstance(by_seed[s].get(key), (int, float))]
    wins = sum(1 for _, d in deltas if d > 0)
    losses = sum(1 for _, d in deltas if d < 0)
    return {"mean": round(statistics.fmean([d for _, d in deltas]), 2), "wins": wins,
            "losses": losses, "ties": len(deltas) - wins - losses,
            "cells": " / ".join(f"{d:+.2f}" for _, d in deltas)}


def _sum(rows: List[Dict[str, Any]], mech: str, key: str) -> int:
    return sum(int(r.get(key) or 0) for r in rows if r["mechanism"] == mech)


def _problems(rows: List[Dict[str, Any]], mech: str) -> int:
    """`need_problems` is the validator's semicolon-joined reason list, not a count."""
    total = 0
    for r in rows:
        if r["mechanism"] == mech:
            total += len([p for p in str(r.get("need_problems") or "").split(";") if p.strip()])
    return total


def table(rows: List[Dict[str, Any]]) -> List[str]:
    out = ["| 臂 | 最终总效用 | 需求识别 F1(%) | 重要需求识别召回(%) | 重要需求最终满足率(%) | 进入优化器的推断课位 |",
           "| --- | --- | --- | --- | --- | --- |"]
    for mech in ARMS:
        out.append("| " + mech[0] + " | " + " | ".join(str(means(rows, mech, k)) for k, _ in METRICS) + " |")
    for key, label in (("total_preference_utility", "最终总效用"), ("key_need_fulfillment_pct",
                                                                       "重要需求最终满足率(%)")):
        p = paired(rows, key)
        out.append(f"- J − I {label}：均值 {p['mean']}，{p['wins']} 胜 {p['losses']} 负 {p['ties']} 平"
                   f"（逐种子 {p['cells']}）")
    j = "J_AI语义需求解析"
    fallback = sum(1 for r in rows if r["mechanism"] == j and str(r.get("llm_fallback")).lower() == "true")
    unusable = sum(1 for r in rows if r["mechanism"] == j and str(r.get("valid_for_ai_claim")).lower() == "false")
    out.append(f"- J 成本（6 个开发种子合计）：LLM 调用 {_sum(rows, j, 'llm_calls')} 次，提示词 "
               f"{_sum(rows, j, 'llm_prompt_tokens')} / 补全 {_sum(rows, j, 'llm_completion_tokens')} token，"
               f"批次重试 {_sum(rows, j, 'llm_batch_retries')} 次，档案缓存命中 {_sum(rows, j, 'cache_hits')} / "
               f"未命中 {_sum(rows, j, 'cache_misses')}。")
    out.append(f"- J 可靠性：因回退被标记不可用作 AI 证据的行 {unusable} 行（其中 llm_fallback 为真 "
               f"{fallback} 行），校验器未采纳的推断 I {_problems(rows, 'I_规则语义需求解析')} 条 / "
               f"J {_problems(rows, j)} 条；硬约束违规 I {_sum(rows, 'I_规则语义需求解析', 'hard_violations')} 个、"
               f"J {_sum(rows, j, 'hard_violations')} 个。")
    return out


def cache_note() -> str:
    """Informational, deliberately not part of `--check`: the cache grows while batches run."""
    path = os.path.join(ROOT, "semantic_cache", "llm_relevance.jsonl")
    keys, bad = set(), 0
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if not line:
                continue
            try:
                keys.add(json.loads(line)["key"])
            except (json.JSONDecodeError, KeyError):
                bad += 1
    stamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    return (f"- 档案缓存 `semantic_cache/llm_relevance.jsonl`（截至 {stamp}）：{len(keys)} 个唯一档案解析记录"
            f"（键 = 解析器版本 + `.skill` 原文 + 课程目录 + 模型），损坏/截断 {bad} 行。")


def main() -> int:
    rows = dev_rows()
    lines = table(rows)
    if "--check" not in sys.argv:
        print("\n".join(lines + [cache_note()]))
        return 0
    readme = open(os.path.join(ROOT, "README.md"), encoding="utf-8").read()
    # Cut the section at the next horizontal rule line. Splitting on the substring "---" would stop
    # at the markdown table's own `| --- | --- |` separator and drop the rows from the comparison.
    after = readme.split(MARKER, 1)[1].splitlines()
    body = "\n".join(takewhile(lambda line: line.strip() != "---", after))
    problems = [line for line in lines if line not in body]
    print(f"README §7B 开发集核对：{len(lines) - len(problems)} / {len(lines)} 行一致")
    if problems:
        for line in problems:
            print("  不符：", line)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
