import csv, collections

def summarize(path, label):
    with open(path, encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    print("==", label, "rows", len(rows), "seeds", len({r["seed"] for r in rows}))
    agg = collections.defaultdict(lambda: collections.Counter())
    calls = collections.Counter()
    ptok = collections.Counter()
    ctok = collections.Counter()
    fb = collections.defaultdict(list)
    solver = collections.Counter()
    gap = collections.Counter()
    viol = collections.Counter()
    for r in rows:
        m = r["mechanism"]
        if r["status"] != "已运行" or r["valid_for_ai_claim"] == "":
            pass
        for k in ("proposals_generated", "proposals_valid", "proposals_pruned",
                  "actions_rejected", "proposals_confirmed"):
            if r[k] != "":
                agg[m][k] += float(r[k])
        if r["llm_calls"] != "":
            calls[m] += int(float(r["llm_calls"]))
        if r.get("llm_prompt_tokens", "") != "":
            ptok[m] += int(float(r["llm_prompt_tokens"]))
        if r.get("llm_completion_tokens", "") != "":
            ctok[m] += int(float(r["llm_completion_tokens"]))
        if r.get("llm_fallback", "") == "True":
            fb[m].append(r["seed"])
        if r["solver_status"] != "":
            solver[r["solver_status"]] += 1
            gap[r["solver_bound_gap"]] += 1
        viol[m] += float(r["hard_violations"])
    for m in sorted(agg):
        c = agg[m]
        print(f"  {m}: 提案 {c['proposals_generated']:.0f} / 通过 {c['proposals_valid']:.0f}"
              f" / 修剪 {c['proposals_pruned']:.0f} / 丢弃动作 {c['actions_rejected']:.0f}"
              f" / 确认 {c['proposals_confirmed']:.0f} | llm_calls {calls[m]}"
              f" | prompt {ptok[m]} completion {ctok[m]} | fallback {fb[m]}")
    print("  solver:", dict(solver), "bound_gap:", dict(gap))
    print("  hard_violations by arm:", dict(viol))
    print("  excluded rows:", [(r["seed"], r["mechanism"]) for r in rows if r["status"] != "已运行"])

summarize("/home/redaunt/ai_course_planner_mvp/results/raw_rows.csv", "批次1-2 (种子1-40)")
summarize("/home/redaunt/ai_course_planner_mvp/results_confirmatory_41_80/raw_rows.csv", "批次3 (种子41-80)")
