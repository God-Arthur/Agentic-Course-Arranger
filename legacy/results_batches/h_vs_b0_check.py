import csv

def load(path):
    with open(path, encoding="utf-8-sig") as f:
        return {(r["seed"], r["mechanism"]): r for r in csv.DictReader(f)}

for label, path in (("1-40", "/home/redaunt/ai_course_planner_mvp/results/raw_rows.csv"),
                    ("41-80", "/home/redaunt/ai_course_planner_mvp/results_confirmatory_41_80/raw_rows.csv")):
    d = load(path)
    seeds = sorted({k[0] for k in d}, key=int)
    bad_h = [s for s in seeds
             if d[(s, "H_获取信息不改预置")]["total_preference_utility"]
             != d[(s, "B0_一次性优化无交互")]["total_preference_utility"]]
    bad_min = [s for s in seeds
               if d[(s, "H_获取信息不改预置")]["sim_min_utility"]
               != d[(s, "B0_一次性优化无交互")]["sim_min_utility"]]
    print(f"{label}: H≡B0 总效用不等的种子 {bad_h} / 最差情景不等的种子 {bad_min} / 共 {len(seeds)} 个种子")
