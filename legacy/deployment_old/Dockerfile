# 刻意不写 `# syntax=docker/dockerfile:1`：那会让 BuildKit 先从 Docker Hub 拉前端镜像，
# 而本文件没有用到需要新前端的语法，内置前端就能离线构建。
#
# 默认基础镜像用 Docker Hub 的 python:3.12-slim。网络取不到时可用任意同版本镜像顶替：
#   docker build --build-arg BASE_IMAGE=<本地已有镜像> -t ai-course-planner .
ARG BASE_IMAGE=python:3.12-slim
FROM ${BASE_IMAGE}

ARG PIP_ADDITIONAL_ARGS=""

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DEFAULT_TIMEOUT=120 \
    PIP_RETRIES=5 \
    PLANNER_DATA_DIR=/app/data \
    PLANNER_CACHE_DIR=/app/semantic_cache \
    PLANNER_LOG_DIR=/app/logs \
    PLANNER_OUTPUT_DIR=/app/outputs \
    PLANNER_DATA_ROOT=/app \
    PLANNER_PORT=8501

WORKDIR /app

# 依赖先装：这一层最贵，所以 APP_VERSION 这种"每次发布都会变"的构建参数刻意放到文件末尾，
# 否则改一次版本号就会把重装依赖的缓存一起打掉，而重装需要外网。
# 版本钉死在 requirements.txt，实测与开发机跑通全部单测的那组版本一致。
COPY requirements.txt /app/requirements.txt
RUN pip install ${PIP_ADDITIONAL_ARGS} --no-cache-dir -r /app/requirements.txt \
 && python -c "import ortools, streamlit, pandas, requests, dotenv; print('deps-ok')"

# 系统包做成可选：python:3.12-slim 已经自带 tzdata 与 ca-certificates，而 ortools 9.15 的 .so
# 经 ldd 实测不依赖 libgomp（CP-SAT 静态链进轮子），所以完全离线的机器可以
# --build-arg APT_PACKAGES="" 跳过这一步。APT_MIRROR 给取不到 deb.debian.org 的网络用。
ARG APT_PACKAGES="libgomp1 tzdata ca-certificates"
ARG APT_MIRROR=""
RUN set -eux; \
    if [ -n "$APT_PACKAGES" ]; then \
      if [ -n "$APT_MIRROR" ]; then \
        sed -i "s|http://deb.debian.org/debian-security|${APT_MIRROR}/debian-security|g; \
                s|http://deb.debian.org/debian|${APT_MIRROR}/debian|g" /etc/apt/sources.list.d/debian.sources; \
      fi; \
      apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=20 update; \
      apt-get -o Acquire::Retries=5 -o Acquire::http::Timeout=20 install -y --no-install-recommends $APT_PACKAGES; \
      rm -rf /var/lib/apt/lists/*; \
    fi

# .dockerignore 已经把 .venv、实验数据分块、日志、.env 排除在外；这里进来的是代码 + 两份冻结判定 + 预注册文档。
COPY . .
COPY data/ /app/data_ship/

RUN useradd --create-home --uid 1000 --shell /bin/bash planner \
 && mkdir -p /app/data /app/semantic_cache /app/logs /app/outputs \
 && cp -rn /app/data_ship/. /app/data/ \
 && chmod +x /app/docker-entrypoint.sh \
 && find /app -name __pycache__ -type d -prune -exec rm -rf {} + \
 && chown -R planner:planner /app

USER planner
EXPOSE 8501

# 放在最后：只改版本号的重新构建会命中前面所有层的缓存，不需要外网。
ARG APP_VERSION=local
ENV APP_VERSION=${APP_VERSION}

# Streamlit 自带 /_stcore/health，返回 ok 才算活着；不额外装 curl。
HEALTHCHECK --interval=30s --timeout=10s --start-period=25s --retries=3 \
  CMD python -c "import os,sys,urllib.request as r; p=os.environ.get('PLANNER_PORT','8501'); sys.exit(0 if r.urlopen('http://127.0.0.1:'+p+'/_stcore/health', timeout=5).read()==b'ok' else 1)"

ENTRYPOINT ["/app/docker-entrypoint.sh"]
