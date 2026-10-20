#!/usr/bin/env bash
# Block until the v2 chunk runner is gone, then run the full suite the batch made impossible.
#
# ``unittest discover`` collects test_planner_core / test_experiments / test_app_semantic_tab, all of
# which call CP-SAT under a 6 s wall clock; running them beside the batch would steal that clock and
# change later seeds' solutions. So this waits instead of running now.
#
# It deliberately does NOT generate FROZEN_VERDICT_V2.md or splice it into the README — publishing
# the verdict stays a reviewed step. The log is written inside the repo: a WSL reclaim wipes /tmp and
# the evidence with it.
#
# Start: setsid nohup results_need_v2/wait_batch_then_fulltests.sh >/dev/null 2>&1 &
set -u
cd "$(dirname "$0")/.." || exit 1
LOG=logs_v2/wait_batch_then_fulltests.log
# The bracket classes keep ``pgrep -f`` from matching this script's own command line: an unbracketed
# literal appears verbatim in the loop above, so the waiter would see a "batch" process and never exit.
while pgrep -f '[r]un_need_v2_chunks.sh' >/dev/null 2>&1 || pgrep -f '[r]un_need_v2\.py' >/dev/null 2>&1; do
  sleep 60
done
{
  date '+%F %T 批次进程已退出，开始跑全套单测'
  echo "已打印 [seed N] 行数 = $(grep -c '^\[seed ' logs_v2/v2_chunks.log)（齐的话是 26 减去重复运行）"
  ls -d results_need_v2/*/ | tr '\n' ' '
  echo
  .venv/bin/python -m unittest discover -s tests -t .
} >> "$LOG" 2>&1
