#!/usr/bin/env bash
# 只等两件事：判定落盘，然后收尾日志停止增长（连续 3 分钟字节数不变）。
# 本脚本的命令行里不含任何批次/等待器/收尾器的匹配串，所以它不会把 pgrep 循环污染成"批次还在跑"。
set -o pipefail
cd "$(dirname "$0")/.." || exit 2
VERDICT=results_need_v2/FROZEN_VERDICT_V2.md
PIPE=logs_v2/post_batch_pipeline.log

echo "[watch] $(date +%H:%M:%S) 开始等待判定；此刻 $VERDICT 存在=$([ -f "$VERDICT" ] && echo yes || echo no)"
for _ in $(seq 1 120); do
  [ -f "$VERDICT" ] && break
  sleep 60
done
echo "[watch] $(date +%H:%M:%S) 判定循环结束，存在=$([ -f "$VERDICT" ] && echo yes || echo no)"
[ -f "$VERDICT" ] || exit 3

prev=-1
for _ in $(seq 1 40); do
  cur=$(wc -c < "$PIPE" 2>/dev/null || echo 0)
  if [ "$cur" = "$prev" ] && [ "$cur" -gt 0 ]; then
    echo "[watch] $(date +%H:%M:%S) 收尾日志连续 3 分钟停在 $cur 字节，判定链已结束"
    break
  fi
  prev=$cur
  sleep 180
done
echo "[watch] $(date +%H:%M:%S) 收尾日志 $PIPE 全文："
cat "$PIPE"
