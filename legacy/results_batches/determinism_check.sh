#!/usr/bin/env bash
set -uo pipefail
cd "$(dirname "$0")/.."
OUT=results_batches/determinism_rerun
mkdir -p "$OUT"
for SEED in 41 42 43; do
  .venv/bin/python run_experiments.py --seeds $SEED --n-students 72 \
    --question-budget 30 --questions-per-round 4 --sim-replicates 3 \
    --solver-workers 1 --solver-max-seconds 6 --out "$OUT/seed_$SEED" > "$OUT/seed_$SEED.log" 2>&1
  echo "[rerun seed $SEED] exit=$?"
done
.venv/bin/python - "$OUT" <<'PY'
import csv, sys, os
base = "/home/redaunt/ai_course_planner_mvp/results_confirmatory_41_80/raw_rows.csv"
with open(base, encoding="utf-8-sig") as f:
    ref = {(r["seed"], r["mechanism"]): r for r in csv.DictReader(f)}
DET = ("A_现行机制基线", "B0_一次性优化无交互", "B_规则式集体规划",
       "G_等预算随机询问", "H_获取信息不改预置", "D_完整信息参照", "F_规则协调提案")
mismatch = []
checked = 0
for seed in ("41", "42", "43"):
    p = os.path.join(sys.argv[1], f"seed_{seed}", "raw_rows.csv")
    with open(p, encoding="utf-8-sig") as f:
        rows = {(r["mechanism"], r["seed"]): r for r in csv.DictReader(f)}
    for m in DET:
        r = rows.get((m, seed))
        o = ref.get((seed, m))
        if r is None or o is None:
            mismatch.append((seed, m, "missing"))
            continue
        checked += 1
        for col in ("total_preference_utility", "sim_min_utility", "sim_max_utility",
                    "student_p10_utility", "required_course_fulfillment_pct",
                    "questions", "informative_questions", "rounds", "hard_violations"):
            if (r[col] or "") != (o[col] or ""):
                mismatch.append((seed, m, col, r[col], o[col]))
print(f"逐字段比对的 (臂,种子) 组合数 = {checked}")
print("不一致项 =", mismatch if mismatch else "无（确定性臂重跑完全复现）")
PY
