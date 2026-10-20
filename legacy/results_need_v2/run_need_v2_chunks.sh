#!/usr/bin/env bash
# 完整语义需求模型（v2）的预注册批次：判据、种子划分与全部参数冻结于
# results_need_v2/PREREGISTRATION_NEED_V2.md；启动后不得改参数，改一个字都算新的处理。
#
# 分块计划、种子合法性与"哪些块已在盘上"全部由 need_v2_chunk_plan.py 决定；
# 本脚本不解析任何种子列表（v1 那次词分割把 6 种子开发块覆盖成单种子，教训写在
# need_chunk_plan.py 的文件头里）。计划一旦写错，覆盖是即时的。
set -uo pipefail
cd "$(dirname "$0")/.."

# 传输路径修正（不改任何实验参数）：本机代理到该网关的 TLS 握手会失败，表现为
# SSLError/ProxyError，整批 J2 会静默回退成规则解析——回退行不是 AI 证据。
GATEWAY_HOST=apihub.agnes-ai.com
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$GATEWAY_HOST"
export no_proxy="${no_proxy:+$no_proxy,}$GATEWAY_HOST"

# 通道自检：一次真实调用（临时缓存目录，必定未命中）。不通就不要开跑。
if ! .venv/bin/python llm_preflight.py; then
  echo "=== aborted by preflight ==="
  exit 1
fi

pending=0
while IFS=$'\t' read -r action tag seeds; do
  case "$action" in
    SKIP) echo "=== $tag 已在盘上，跳过"; continue ;;
    RUN) ;;
    *) echo "=== 计划输出异常：$action / $tag ==="; exit 1 ;;
  esac
  if [ -z "$tag" ] || [ -z "$seeds" ]; then
    echo "=== 拒绝启动：分块 $tag 的种子为空（覆盖目标目录会毁掉已有数据） ==="
    exit 1
  fi
  pending=$((pending + 1))
  echo "=== $tag start seeds=$seeds 其它求解进程=$(pgrep -f run_experiments.py | tr '\n' ' ')"
  .venv/bin/python run_need_v2.py \
    --seeds $seeds --n-students 72 \
    --question-budget 30 --questions-per-round 4 --sim-replicates 3 \
    --solver-workers 1 --solver-max-seconds 6 \
    --include-need-llm \
    --out "results_need_v2/$tag"
  rc=$?
  echo "=== $tag rc=$rc done 其它求解进程=$(pgrep -f run_experiments.py | tr '\n' ' ')"
  if [ $rc -ne 0 ]; then
    echo "=== 中止：$tag 返回 $rc，已完成的分块保留在盘上，重跑本脚本会从这里继续 ==="
    exit $rc
  fi
done < <(.venv/bin/python need_v2_chunk_plan.py)

# 完成标记只看盘上事实，不看本脚本自认为跑了多少：全部块齐全才打印。
if .venv/bin/python need_v2_chunk_plan.py --inspect | grep -q '^RUN'; then
  echo "=== INCOMPLETE 本次补了 $pending 块，仍有分块缺失"
  exit 2
fi
echo "=== ALL_CHUNKS_COMPLETE_V2 种子 1-26 全部落盘"
