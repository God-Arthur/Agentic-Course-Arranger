#!/usr/bin/env bash
set -uo pipefail
cd /home/redaunt/ai_course_planner_mvp
{
  echo "=============================================================="
  echo "re-run 2026-10-17 (README 批次 3 改写后：新增未复现 caption、配对表破折号、H≡B0 种子数现算)"
  date '+%F %T'
} >> results/headless_apptest_smoke.log
.venv/bin/python results/apptest_smoke.py >> results/headless_apptest_smoke.log 2>&1
echo "exit=$?" >> results/headless_apptest_smoke.log
tail -8 results/headless_apptest_smoke.log
