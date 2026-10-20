#!/usr/bin/env bash
# 批次结束后的自动收尾：只调用已经冻结的脚本，不改 README、不写正文、不做任何判断。
#
# 为什么要挂这个：批次还要三小时以上，而主判据必须由 analyze_need_v2.py 直接从落盘的 CSV 生成。
# 中间只要留人工环节，就存在"先看到数字再回头改东西"的空间。所以生成与核对交给脚本，
# "要不要发布、怎么写进正文"仍然留给人。
#
# 顺序：
#   1) 等批次进程退出；
#   2) 等 wait_batch_then_fulltests.sh 退出（它会先把全套单测跑完）；
#   3) analyze_need_v2.py —— 种子不齐时非零退出且不写文件（半批状态已实测）；
#   4) 判定文件真落到盘上时，重跑离线 v2 界面测试，走"判定已在盘上"那条分支；
#   5) 四个守卫逐个打印退出码。
#
# 输出全部进 logs_v2/post_batch_pipeline.log。本脚本不发起 LLM 请求，也不进求解器。
#
# 手动运行：bash results_need_v2/post_batch_pipeline.sh
set -u
cd "$(dirname "$0")"/.. || exit 1
LOG=logs_v2/post_batch_pipeline.log
: > "$LOG"
say() { echo "$(date '+%F %T') $*" >> "$LOG"; }
run() { say "== $*"; "$@" >> "$LOG" 2>&1; say "rc=$?"; }

# 匹配串一律写成字符类形式：`pgrep -f` 匹配的是整条命令行，而监控本批次的**任何**进程
# （包括另开一个终端来查看进度的那条命令行，也包括本文件所在的目录名 results_need_v2/）
# 只要原样含有 run_need_v2.py 就会被当成"批次还在跑"。2026-10-18 01:57 就真这样死锁过一次：
# 一个挂着等本脚本收尾的观察者，命令行里带着字面量 run_need_v2.py，于是本脚本的等待循环
# 每轮都看见"批次进程"，而观察者又在等本脚本打印结束标记。'run_need_v2[.]py' 能匹配真实的
# run_need_v2.py，却匹配不到写成本形的字面量（那个位置真实文本是 '['）。
while pgrep -f 'run_need_v2_[c]hunks.sh' >/dev/null 2>&1 || pgrep -f 'run_need_v2[.]py' >/dev/null 2>&1; do
  sleep 120
done
say "批次进程已退出。logs_v2/v2_chunks.log 里 [seed N] 行数 = $(grep -c '^\[seed ' logs_v2/v2_chunks.log)。"

while pgrep -f 'wait_batch_then_fulltests[.]sh' >/dev/null 2>&1; do sleep 60; done
say "等待器已退出，全套单测结果见 logs_v2/wait_batch_then_fulltests.log。"

run .venv/bin/python analyze_need_v2.py
if [ -f results_need_v2/FROZEN_VERDICT_V2.md ]; then
  say "判定文件已产出，重跑离线 v2 界面测试以覆盖「已在盘上」分支。"
  run .venv/bin/python -m unittest tests.test_app_need_v2_tab -v
else
  say "判定文件没有产出：种子不齐或批次失败。跳过该分支，也不得由本脚本补写。"
fi

run .venv/bin/python results_need_v2/prereg_coverage_guard.py
run .venv/bin/python results_need_v2/need_v2_number_guard.py
run .venv/bin/python results_semantic_need/verdict_readme_guard.py
run .venv/bin/python results_batches/readme_number_guard.py
say "=== pipeline end ==="
