#!/usr/bin/env bash
# 语义需求位点的预注册批次（分块可续跑版）。
# 判据、种子划分与全部参数冻结于 PREREGISTRATION_SEMANTIC_NEED.md；启动后不得改参数。
#
# 分块计划、种子合法性与"哪些块已在盘上"全部由 need_chunk_plan.py 决定。
# 本脚本不解析任何种子列表：17:43 的那一版把计划写在 bash 字符串里，
# 词分割把 dev_1_6:1 2 3 4 5 6 拆成六个残缺 spec，于是用单种子的结果覆盖了
# 已完成的 6 种子开发块，并建出 results_semantic_need/2 这种目录。计划一旦写错，
# 覆盖是即时的——所以解析必须只有一份，且放在能拒绝非法划分的 Python 里。
#
# 每块约 45 分钟，跑完即落盘；WSL 虚拟机被回收后重跑本脚本只补缺失块。
set -uo pipefail
cd "$(dirname "$0")/.."

# 传输路径修正（不改任何实验参数）：本机代理 127.0.0.1:7890 到该网关的 TLS 握手会失败，
# 表现为 SSLError/ProxyError，整批 J 组会静默回退成规则解析。直连同网关返回 401（可达）。
GATEWAY_HOST=apihub.agnes-ai.com
export NO_PROXY="${NO_PROXY:+$NO_PROXY,}$GATEWAY_HOST"
export no_proxy="${no_proxy:+$no_proxy,}$GATEWAY_HOST"

# 通道自检：一次真实调用（临时缓存目录，必定未命中）。不通就不要开跑——
# 缺这一步，J 组会在整批里静默回退成规则解析，跑完的数据一条 AI 证据都不是。
if ! .venv/bin/python llm_preflight.py; then
  echo "=== aborted by preflight $(date '+%F %T') ==="
  exit 1
fi

pending=0
while IFS=$'\t' read -r action tag seeds; do
  case "$action" in
    SKIP) echo "=== $tag 已在盘上，跳过 $(date '+%F %T') ==="; continue ;;
    RUN) ;;
    *) echo "=== 计划输出异常：$action / $tag ==="; exit 1 ;;
  esac
  if [ -z "$tag" ] || [ -z "$seeds" ]; then
    echo "=== 拒绝启动：分块 $tag 的种子为空（覆盖目标目录会毁掉已有数据） ==="
    exit 1
  fi
  pending=$((pending + 1))
  echo "=== $tag start $(date '+%F %T') seeds=$seeds ==="
  .venv/bin/python run_experiments.py \
    --seeds $seeds --n-students 72 \
    --question-budget 30 --questions-per-round 4 --sim-replicates 3 \
    --solver-workers 1 --solver-max-seconds 6 \
    --include-need-llm \
    --out "results_semantic_need/$tag"
  rc=$?
  echo "=== $tag rc=$rc done $(date '+%F %T') ==="
  if [ $rc -ne 0 ]; then
    echo "=== 中止：$tag 返回 $rc，已完成的分块保留在盘上，重跑本脚本会从这里继续 ==="
    exit $rc
  fi
done < <(.venv/bin/python need_chunk_plan.py)

# 完成标记只看盘上事实，不看本脚本自认为跑了多少：全部块齐全才打印。
if .venv/bin/python need_chunk_plan.py --inspect | grep -q '^RUN'; then
  echo "=== INCOMPLETE 本次补了 $pending 块，仍有分块缺失 $(date '+%F %T') ==="
  exit 2
fi
echo "=== ALL_CHUNKS_COMPLETE 种子 1-26 全部落盘 $(date '+%F %T') ==="
