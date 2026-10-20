#!/bin/sh
# Container entrypoint: seed the mounted data volume, make sure the writable dirs exist, then run
# Streamlit in the foreground so the container's lifecycle is the app's lifecycle.
set -eu

echo "[entrypoint] AI 带回计划经济 ${APP_VERSION:-local}"
if [ "${APP_VERSION:-local}" = "local" ]; then
    echo "[entrypoint] 提示：这次构建没有标版本，镜像标签是 ai-course-planner:local；" \
         "回滚时无法只凭标签区分新旧镜像，正式部署请设 APP_VERSION。"
fi
echo "[entrypoint] PLANNER_DATA_DIR=${PLANNER_DATA_DIR:-/app/data}"
echo "[entrypoint] PLANNER_CACHE_DIR=${PLANNER_CACHE_DIR:-/app/semantic_cache}"
echo "[entrypoint] PLANNER_LOG_DIR=${PLANNER_LOG_DIR:-/app/logs}"
echo "[entrypoint] PLANNER_OUTPUT_DIR=${PLANNER_OUTPUT_DIR:-/app/outputs}"
echo "[entrypoint] PLANNER_DATA_ROOT=${PLANNER_DATA_ROOT:-/app}"

DATA_DIR="${PLANNER_DATA_DIR:-/app/data}"

if [ ! -f "${DATA_DIR}/courses.csv" ]; then
    if mkdir -p "${DATA_DIR}" 2>/dev/null \
       && cp -rn /app/data_ship/. "${DATA_DIR}/" 2>/dev/null; then
        echo "[entrypoint] 已把镜像内置示例数据集播种到 ${DATA_DIR}"
    else
        echo "[entrypoint] 警告：${DATA_DIR} 不可写，导入表格目录的功能将不可用（合成生成不受影响）"
    fi
elif [ ! -f "${DATA_DIR}/simulation_truth.csv" ]; then
    echo "[entrypoint] 警告：${DATA_DIR} 里有 courses.csv 但缺 simulation_truth.csv（隐藏真值），" \
         "导入模式会在数据契约检查处直接报错；镜像里的示例真值不会被自动补进已有目录，需要人工确认。"
fi

for dir in "${PLANNER_CACHE_DIR:-/app/semantic_cache}" "${PLANNER_LOG_DIR:-/app/logs}" \
           "${PLANNER_OUTPUT_DIR:-/app/outputs}"; do
    mkdir -p "$dir" 2>/dev/null || echo "[entrypoint] 警告：无法创建 $dir，对应功能会降级并给出可读错误"
done

if [ -n "${OPENAI_API_KEY:-}" ]; then
    echo "[entrypoint] LLM 密钥已配置：是（来自环境变量，模型 ${OPENAI_MODEL:-默认}，限流 ${LLM_REQUESTS_PER_MINUTE:-20} 次/分钟）"
elif [ -f "${OPENAI_API_KEY_FILE:-}" ]; then
    echo "[entrypoint] LLM 密钥已配置：是（来自只读文件 ${OPENAI_API_KEY_FILE}，模型 ${OPENAI_MODEL:-默认}，限流 ${LLM_REQUESTS_PER_MINUTE:-20} 次/分钟）"
else
    echo "[entrypoint] LLM 密钥已配置：否（规则规划、规则提案与两轮模拟照常运行；C/E/J 组会明确报错而不是假装成功）"
fi
echo "[entrypoint] 求解器 SOLVER_WORKERS=${SOLVER_WORKERS:-1} SOLVER_MAX_SECONDS=${SOLVER_MAX_SECONDS:-6}"
echo "[entrypoint] 容器内监听 ${PLANNER_BIND_ADDRESS:-0.0.0.0}:${PLANNER_PORT:-8501}；" \
     "对外暴露到哪个网卡由 compose 的端口映射决定，默认只映射宿主 127.0.0.1"

exec streamlit run app.py \
    --server.headless=true \
    --server.address="${PLANNER_BIND_ADDRESS:-0.0.0.0}" \
    --server.port="${PLANNER_PORT:-8501}" \
    "$@"
