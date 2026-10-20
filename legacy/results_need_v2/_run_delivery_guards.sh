#!/usr/bin/env bash
# 交付前把每一条"能失败的核对"重跑一遍，输出进 logs_v2/。
# 本脚本只读数据、只写日志，不生成判定、不写 README——判定正文永远由 analyze_need_v2.py 产出。
# 任何一条非 0 就记进失败清单，最后原样打印；不因为"多半没问题"跳过任何一条。
set -u
cd "$(dirname "$0")/.." || exit 1
# 开发机默认用仓库里的 venv；容器里没有 .venv，用 PYTHON=python 覆盖，其余行为不变。
PY="${PYTHON:-.venv/bin/python}"
LOG="logs_v2/delivery_guards_$(date +%H%M).log"
: > "$LOG"
fail=0
run() {
  echo "=== $*" | tee -a "$LOG"
  if "$PY" "$@" >> "$LOG" 2>&1; then
    echo "PASS（末行：$(tail -1 "$LOG")）"
  else
    echo "FAIL rc≠0：$*（末行：$(tail -1 "$LOG")）"
    fail=$((fail + 1))
  fi
}
run results_need_v2/need_v2_number_guard.py
run results_need_v2/_selftest_number_guard.py
run results_semantic_need/verdict_readme_guard.py
run results_batches/readme_number_guard.py
run results_need_v2/prereg_coverage_guard.py
run results_need_v2/report_field_probe.py
run results_need_v2/_selftest_field_probe_constant_mutant.py
run results_need_v2/pair_health_v2.py
# 缓存产出度量读的是开发机上那份真实的 LLM 缓存；镜像里 semantic_cache/ 被 .dockerignore 排除，
# 容器内这个文件天然不存在。缺文件时按"本环境不适用"跳过，而不是伪装成通过或记成失败。
CACHE_FILE="${PLANNER_CACHE_DIR:-semantic_cache}/llm_relevance_v2.jsonl"
if [ -f "$CACHE_FILE" ]; then
  run results_need_v2/cache_yield_v2.py
else
  echo "SKIP-HOST-ONLY：$CACHE_FILE 不在，缓存产出度量只在跑过 J/J2 批次的开发机上才有意义。"
fi
# 这一条是设计成"真判定已落盘就拒绝运行"的：它只在批次期间用合成样本预跑搬运链。
# 非 0 且报出那句拒绝理由，算按设计跳过；换成别的失败原因仍然计一条失败。
echo "=== results_need_v2/_selftest_splice_chain.py（批次期间的合成样本预跑）" | tee -a "$LOG"
if "$PY" results_need_v2/_selftest_splice_chain.py >> "$LOG" 2>&1; then
  echo "PASS" | tee -a "$LOG"
elif grep -q '判定已落盘，本自检不再往 README 副本里拼合成样本' "$LOG"; then
  echo "SKIP-BY-DESIGN：真判定已在盘上，搬运链此刻由 verdict_readme_guard 用真判定核对（上面那条 PASS 就是它）。"
else
  echo "FAIL rc≠0 且不是那句设计性拒绝"
  fail=$((fail + 1))
fi
run results_need_v2/_selftest_analyze_contract.py
run results_need_v2/_selftest_prereg_coverage.py
# 等待循环的两条核对性质不同：前者读实时 /proc（批次不在跑时总体为空，只算"当前无污染"），
# 后者不依赖任何进程，用合成的命令行证明括号类既没写瞎也不再自我命中。两条都要跑。
run results_need_v2/_selftest_waiter_bracket.py
run results_need_v2/waiter_pattern_probe.py
# 全套单测：全部离线（不碰 LLM、不碰网络，例数以 unittest 自己打印的为准），批次期间不能跑，交付前必须跑。
run -m unittest discover -s tests -t .
echo "汇总：失败 $fail 条；原始输出 $LOG"
[ "$fail" -eq 0 ] && echo ALL-GUARDS-PASS || echo SOME-GUARDS-FAILED
