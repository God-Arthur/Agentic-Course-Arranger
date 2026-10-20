#!/bin/sh
# 只读采样器：每 120 秒记录一次"缓存行数 + 日志里已打印的种子行数"，用于事后算实测吞吐。
# 不做任何求解、不调用 LLM、不写业务文件；批次进程一消失就自己退出，不留孤儿进程。
cd /home/redaunt/ai_course_planner_mvp || exit 1
OUT=results_need_v2/cost_samples_v2.csv
[ -f "$OUT" ] || printf 'local_time,llm_cache_rows,seed_lines_printed\n' > "$OUT"
while :; do
  rows=$(grep -c . semantic_cache/llm_relevance_v2.jsonl 2>/dev/null)
  seeds=$(grep -c '^\[seed ' logs_v2/v2_chunks.log 2>/dev/null)
  printf '%s,%s,%s\n' "$(date +%H:%M:%S)" "${rows:-0}" "${seeds:-0}" >> "$OUT"
  pgrep -f 'run_need_v2\.py' >/dev/null 2>&1 || exit 0
  sleep 120
done
