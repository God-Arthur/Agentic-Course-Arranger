import csv
import json
import os

PROJ = os.path.expanduser("~/ai_course_planner_mvp")
OUT = os.path.join(PROJ, "results_confirmatory_41_80")
CHUNKS = [os.path.join(OUT, f"chunk_{a}_{a + 9}") for a in (41, 51, 61, 71)]

rows = []
configs = []
for d in CHUNKS:
    path = os.path.join(d, "raw_rows.csv")
    if not os.path.exists(path):
        raise SystemExit(f"缺少 {path}，等所有分块跑完再合并")
    with open(path, encoding="utf-8-sig", newline="") as handle:
        chunk = list(csv.DictReader(handle))
    seeds = sorted({int(r["seed"]) for r in chunk})
    print(d.split("/")[-1], "rows", len(chunk), "seeds", seeds[0], "-", seeds[-1])
    rows.extend(chunk)
    configs.append(json.load(open(os.path.join(d, "summary.json"), encoding="utf-8"))["config"])

seeds = sorted({int(r["seed"]) for r in rows})
assert len(rows) == 360, len(rows)
assert seeds == list(range(41, 81)), seeds
for other in configs[1:]:
    for key in configs[0]:
        if key in ("seeds", "generated_at"):
            continue
        assert json.dumps(configs[0][key], sort_keys=True) == json.dumps(other[key], sort_keys=True), key

config = dict(configs[0])
config["seeds"] = seeds
started = min(c["generated_at"] for c in configs)
config["generated_at"] = started
config["batch_provenance"] = (
    "确认性复跑批次 3（种子 41-80），分四块运行：" +
    "；".join(f"{a}-{a + 9} 起于 {c['generated_at']}" for a, c in zip((41, 51, 61, 71), configs)) +
    "。各块的预算、情景数、求解器与 LLM 参数逐字段核对一致（由本脚本 assert 校验）。"
    "情景种子只由实验种子派生，41-80 与 1-40 无情景重叠。"
    "作废记录：2026-10-17 09:12-11:07 的首次尝试（种子 41-72 只留下日志中的效用）因 WSL 虚拟机 11:08 重启而中断，"
    "该批数据未被采用，日志保留为 results_confirmatory_41_80/batch3_attempt1_partial.log。"
)

with open(os.path.join(OUT, "raw_rows.csv"), "w", newline="", encoding="utf-8-sig") as handle:
    writer = csv.DictWriter(handle, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
with open(os.path.join(OUT, "summary.json"), "w", encoding="utf-8") as handle:
    json.dump({"config": config, "summary": {}}, handle, ensure_ascii=False, indent=2)
print("合并完成：", len(rows), "行 /", len(seeds), "个种子 →", os.path.join(OUT, "raw_rows.csv"))
