#!/usr/bin/env python3
"""Does the cache actually save calls? Cost census over landed v2 rows — read-only.

The task brief asks for one specific thing: "对相同学生档案缓存 AI 的解析结果，避免在不同随机种子下
反复调用模型，减少时间和 API 成本." Cache rows on disk prove the write-through works; they do not
prove reuse. If every seed regenerates 72 fresh profiles, the key is new every time, so the cache
never serves a second seed and the cost grows linearly with the number of seeds.

Reuse is therefore measured directly, three ways that must agree:
  1. the arm's own counters: ``cache_hits`` vs ``cache_misses`` (via ``_require_column``, so a
     renamed column aborts instead of being summed as zero);
  2. ``llm_calls`` total against profiles attempted (seeds x n_students) — if the cache were
     serving repeats, calls would fall well below profiles;
  3. distinct cache keys on disk vs profiles attempted — one row per profile means nothing was
     ever asked twice.

This prints cost only: no means, no diffs, no win counts, no direction. Cost is not the pre-
registered outcome, so reading it mid-batch cannot bias the verdict.

    .venv/bin/python results_need_v2/cache_yield_v2.py
"""
from __future__ import annotations

import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (HERE, ROOT):
    if _path not in sys.path:
        sys.path.insert(0, _path)

from analyze_need_v2 import J2, OUT_DIR, _require_column, _usable, discover_chunks, load_raw_rows  # noqa: E402
from need_v2_number_guard import batch_scale  # noqa: E402

CACHE = os.path.join(ROOT, "semantic_cache", "llm_relevance_v2.jsonl")
COUNTERS = ("llm_calls", "cache_hits", "cache_misses", "llm_prompt_tokens", "llm_completion_tokens")


def main() -> int:
    problems = []
    dev, test = discover_chunks()
    chunks = dev + test
    if not chunks:
        print("FAIL：还没有已收尾分块，本探针无事可核。")
        return 1
    total_seeds, students = batch_scale()

    rows, seeds = [], set()
    for name in chunks:
        for row in load_raw_rows(os.path.join(OUT_DIR, name, "raw_rows.csv")):
            if row.get("mechanism") == J2 and _usable(row):
                rows.append(row)
                seeds.add(int(row["seed"]))
    if not rows:
        print("FAIL：J2 臂没有可用行——那正是『整列为空』要靠这条挡住的情形。")
        return 1

    print(f"已收尾分块 {len(chunks)} 个（{', '.join(chunks)}），种子 {min(seeds)}-{max(seeds)} 共 {len(seeds)} 颗，"
          f"J2 可用行 {len(rows)} 行；整批计划 {total_seeds} 颗种子 × {students} 人。")
    sums = {}
    for key in COUNTERS:
        try:
            _require_column(rows, J2, key)
        except SystemExit as exc:
            problems.append(f"{key}: {exc}")
            continue
        vals = [r.get(key) for r in rows]
        numeric = [v for v in vals if isinstance(v, (int, float))]
        sums[key] = sum(int(v) for v in numeric)
        if len(numeric) != len(rows):
            problems.append(f"{key} 只有 {len(numeric)}/{len(rows)} 行是数值，合计 {sums[key]} 不能当成本结论")
        print(f"  {key} 合计 = {sums[key]}（{len(numeric)}/{len(rows)} 行有数值）")

    profiles_attempted = len(seeds) * students
    hits, misses, calls = sums.get("cache_hits", 0), sums.get("cache_misses", 0), sums.get("llm_calls", 0)
    pool = hits + misses
    print(f"\n复用率 = cache_hits ÷ (hits+misses) = {hits} ÷ {pool} = "
          f"{round(100.0 * hits / pool, 2) if pool else 'n/a'}%")
    print(f"对照 2：档案数 = {len(seeds)} 种子 × {students} 人 = {profiles_attempted}；"
          f"实际请求 {calls} 次 ⇒ 请求/档案 = {round(calls / profiles_attempted, 3) if profiles_attempted else 'n/a'}")

    keys = set()
    if os.path.exists(CACHE):
        import json
        with open(CACHE, encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    keys.add(json.loads(line)["key"])
                except Exception as exc:
                    problems.append(f"缓存行解析失败：{exc}")
                    break
        print(f"对照 3：缓存里不同 key = {len(keys)} 个，档案数 = {profiles_attempted} ⇒ "
              f"key/档案 = {round(len(keys) / profiles_attempted, 3) if profiles_attempted else 'n/a'}")
        # 这一条只能证"缓存里存的是按档案去重的条目"，不能证复用：多出来的 key 是本批之前的试跑
        # 与冒烟留下的。复用到底发生了没有，只由上面的 cache_hits 计数说话。
        if len(keys) > profiles_attempted:
            print(f"  （key 比档案数多 {len(keys) - profiles_attempted} 个：本批之前的试跑/冒烟也写过条目，"
                  "不能据此说'每份档案只被问了一次'）")
    else:
        problems.append(f"读不到 {CACHE}")

    print()
    if problems:
        print(f"问题 = {len(problems)} 处：")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("问题 = 无：三条路径都读到了数，复用率可核对。本脚本不判断这个复用率好不好，只把它算出来。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
