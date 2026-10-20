# 现行系统只有 Python 标准库：排课、LLM 客户端（urllib）、演示服务器都不需要第三方包。
# 因此 runtime 阶段一行 pip 都不装；pytest 只活在 test 阶段。
#
# 刻意不写 `# syntax=docker/dockerfile:1`：那会让 BuildKit 先从 Docker Hub 拉前端镜像，
# 而本文件没用到需要新前端的语法，内置前端就能在离线机器上构建。
#
# 默认基础镜像 python:3.12-slim。取不到 Docker Hub 时用任意同版本镜像顶替：
#   docker build --build-arg BASE_IMAGE=<本地已有镜像> -t ai-course-planner-demo .
ARG BASE_IMAGE=python:3.12-slim

# ── deps：唯一开发依赖是 pytest（版本钉在 requirements.txt）────────────────────────
FROM ${BASE_IMAGE} AS deps
# 国内源：--build-arg PIP_ADDITIONAL_ARGS="--index-url https://pypi.tuna.tsinghua.edu.cn/simple"
ARG PIP_ADDITIONAL_ARGS=""
COPY requirements.txt /tmp/requirements.txt
RUN pip install ${PIP_ADDITIONAL_ARGS} --no-cache-dir -r /tmp/requirements.txt \
 && python -c "import pytest; print('deps-ok', pytest.__version__)"

# ── test：离线回归（假客户端，不联网、不消耗任何 LLM 配额）─────────────────────────
# 构建这条阶段就是完整验收：RUN 失败则镜像构建失败。
#   docker build --target test .
FROM ${BASE_IMAGE} AS test
WORKDIR /app
COPY --from=deps /usr/local /usr/local
COPY planner/ ./planner/
COPY server/ ./server/
COPY demo/ ./demo/
COPY tools/ ./tools/
COPY tests/ ./tests/
COPY data/courses.json ./data/courses.json
COPY pytest.ini requirements.txt ./
RUN python -m pytest -q

# ── runtime：评委演示服务器 ────────────────────────────────────────────────────────
FROM ${BASE_IMAGE} AS runtime
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 非 root 运行。演示是只读工作负载：服务器不写任何文件，唯一的写入口是 planner.cli --out。
RUN useradd --create-home --uid 1000 --shell /bin/bash planner

WORKDIR /app
COPY --chown=planner:planner planner/ ./planner/
COPY --chown=planner:planner server/ ./server/
COPY --chown=planner:planner demo/ ./demo/
# 只带整量目录：三档缩尺文件、合成留档与人数推测都是实验输入，不进演示镜像。
COPY --chown=planner:planner data/courses.json ./data/courses.json

USER planner
EXPOSE 8765

# /api/state 返回 200 且 course_data_loaded 为真才算活着（课程数据缺失时它是 false + error 文本）。
HEALTHCHECK --interval=30s --timeout=10s --start-period=10s --retries=3 \
  CMD python -c "import json,sys,urllib.request as r; d=json.load(r.urlopen('http://127.0.0.1:8765/api/state', timeout=5)); sys.exit(0 if d.get('course_data_loaded') else 1)"

# 端口由 compose 映射；镜像内固定 8765，因为 demo_server 只认 --port。
CMD ["python", "server/demo_server.py", "--port", "8765"]
