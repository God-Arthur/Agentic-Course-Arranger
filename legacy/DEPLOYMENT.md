# 部署文档｜AI 带回计划经济（课程预置与两轮选课集体规划仿真器）

本文里每一条命令都在本机实际执行过（WSL2 Ubuntu + Docker 29.5.0 + Docker Compose v5.1.3），
执行记录留在 `logs_v2/`（构建日志、验收日志、重启核对日志）。命令与仓库当前的文件名、
服务名、卷名逐字一致；没有占位符。

系统形态：单个 Streamlit 进程 + 本地文件状态（表格目录、语义缓存、运行日志、已保存运行），
容器化交付，四个命名卷承载需要跨重启存活的数据。没有数据库、没有独立前端、没有微服务拆分。

---

## 1. 系统要求

| 项目 | 要求 | 本机实测值 |
| --- | --- | --- |
| 操作系统 | Linux 或 WSL2，能跑 Docker Engine 与 Compose v2 | WSL2 Ubuntu，Docker 29.5.0（`docker --version`） |
| 编排 | 需要 Compose v2 的 `env_file` 长语法（本文件按 v2/v5 写法） | v5.1.3（`docker compose version`） |
| 镜像体积 | 约 911 MB 磁盘 | `docker images ai-course-planner` → 911MB（`1.0.0`、`local` 两个标签同一份内容） |
| 运行内存 | 建议 2 GB 可用；空闲态很轻 | 容器空闲 `docker stats --no-stream` → 52.55MiB / 23.47GiB，CPU 0.53% |
| CPU | 一次规划要跑 CP-SAT，单核即可；默认 `SOLVER_WORKERS=1` | 界面默认参数（72 名学生、3 个配对情景）一次运行 `duration_ms=96`（§6 那条实测记录） |
| 端口 | 宿主 8501（可改） | 映射为 `127.0.0.1:8501` |
| 网络 | **运行**不需要外网；**构建**需要能取 PyPI | 见 §2 的代理写法 |
| Python | 由镜像决定，宿主不需要装 | `python:3.12-slim` → 容器内 3.12.15 |
| GPU | 不需要 | — |

不需要的东西：数据库、Redis、反向代理、Node、宿主 Python 虚拟环境。
开发机的 `.venv` 只用于跑源码，不参与容器运行（`.dockerignore` 已排除 `.venv/`）。

## 2. 首次构建

```bash
cd /path/to/ai_course_planner_mvp          # 仓库根，含 Dockerfile 与 compose.yaml
cp .env.example .env                        # 可选：不配密钥也能构建与运行
docker compose build                        # 产物：ai-course-planner:${APP_VERSION}
```

构建产物核对：

```bash
docker images ai-course-planner
```

**本机的构建取包问题（已实测）**：默认桥接网络下 pip 拉 `files.pythonhosted.org` 会在 120 秒读超时
（`logs_v2/build_local_1400.log`）。走宿主代理 + host 网络即可通过，这也是本仓库交付时用的命令：

```bash
APP_VERSION=1.0.0 \
BUILD_PROXY=http://127.0.0.1:7890 \
BUILD_NETWORK=host \
docker compose build
```

`BUILD_PROXY`/`BUILD_NETWORK` 只在 `build:` 阶段生效，不影响运行期。两者都不设时，
compose 走默认桥接网络且不带代理（`compose.yaml` 里是 `${BUILD_NETWORK:-default}`）。
**在能直连 PyPI 的机器上这两个变量都不用设**；在不能直连的机器上，请把它们当成构建命令的固定前缀，
不要一次带、一次不带——原因见本节末尾的缓存说明。本仓库交付时已经把这三行
（`APP_VERSION`、`BUILD_PROXY`、`BUILD_NETWORK`）写进 `.env`，所以后续在同一个仓库根里
**直接 `docker compose build` 就够了**，不需要再重复前缀；实测这样一条命令 4 秒完成（
`logs_v2/build_final_100.log` 首尾两行自带 15:54:30 → 15:54:34 的时间戳，同一份日志里 pip 与 apt
两层标 `CACHED`）。

耗时请按这两个实测数字预期：`--no-cache` 的全量重建里 `pip install` 层 82.5 秒、apt 层 11.3 秒
（`logs_v2/build_proxy_nocache.log` 里 BuildKit 给这两层的自带计时）；命中缓存的重建只有
`COPY .` 之后的三步重跑，整条 compose 命令 4 秒。

其他构建开关（下面标了哪些是本机真跑过的）：

```bash
APT_PACKAGES= docker compose build           # 完全跳过 apt 层；deps-ok、tzdata、150 张内置 CA 均在
APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn docker compose build   # deb.debian.org 取不到时
BASE_IMAGE=<本地已有的 python:3.12 镜像> docker compose build          # 拉不到 Docker Hub 时
```

取包源本身也可以换：`PIP_ADDITIONAL_ARGS=--index-url https://pypi.tuna.tsinghua.edu.cn/simple`
（`.env.example` 里就是这一行；本机的 `APT_MIRROR` 与 `PIP_ADDITIONAL_ARGS` 两条只实测了
`APT_PACKAGES=` 跳层与代理两条，换源那条是按 pip 通用参数写的，没有在本机断网环境验证过）。

`APT_PACKAGES=`（空值）这一条实测通过：apt 步骤被跳过，镜像仍能 `import ortools, streamlit,
pandas, requests, dotenv`，`zoneinfo.ZoneInfo("Asia/Shanghai")` 可解析，`ssl` 有 150 张 CA。
记录见 `logs_v2/build_aptless_1520.log`。注意 compose 用的是 `${APT_PACKAGES-default}`（不带冒号），
所以"设成空值"才会真的跳过，留成未设置则用默认包列表。

缓存行为（两条都用 `--progress=plain` 实跑过，见下面引用的两份日志）：

- **只改版本号不碰依赖层**。`APP_VERSION=probe-9.9 docker compose build --progress=plain`：平铺日志里
  `#9 [4/8] RUN pip install …` 与 `#10 [5/8] RUN apt-get …` 两层都标 `CACHED`，只有 `COPY .` 之后的
  三步重跑（0.1 s / 0.1 s / 1.6 s，1.6 s 那步是导出镜像层），命令几乎立刻返回
  （`logs_v2/build_probe_version.log`）。这是 `Dockerfile` 刻意把 `ARG APP_VERSION` 与 apt 层的
  `ARG APT_PACKAGES` 排在 pip 层之后的结果，由
  `tests/test_deployment.py::test_build_args_do_not_reinstall_dependencies` 钉住这个顺序。
- **但换构建期代理会把 pip 层一起打掉，而且这条不是"猜想"**。实测的两份平铺日志正好构成对照：
  - 只换版本号（代理值与上次相同）：`#7`、`#8`、`#9`、`#10` 全部 `CACHED`
    （`logs_v2/build_probe_version.log`）。
  - 只把 `BUILD_PROXY`/`BUILD_NETWORK` 从"带"变成"不带"，`Dockerfile` 一个字节没改、也不是
    `--no-cache`：`#7`、`#8` 仍 `CACHED`，**`#9` 那层 `RUN pip install` 重跑**，147 秒后
    `ReadTimeoutError: HTTPSConnectionPool(host='files.pythonhosted.org', port=443)` 并以 exit code 2
    失败（`logs_v2/build_local_1400.log`）。
  机制：`HTTP_PROXY`/`HTTPS_PROXY` 是 Docker 的**保留**构建参数，不是 `Dockerfile` 里写的 `ARG`。
  BuildKit 把"某一层看到的构建参数值"算进缓存键，保留参数从第一层起就对每一层可见，而显式 `ARG`
  只在声明它的那一层及之后生效——所以改 `ARG APP_VERSION` 只打掉末尾，改代理打掉全部。
  **结论：在一台机器上请把 `BUILD_PROXY`/`BUILD_NETWORK` 当成固定前缀（本仓库把它们写进 `.env`），
  别一次带、一次不带。**
- **换 `BASE_IMAGE` 或 `--no-cache` 不是离线动作**，即使基础镜像已在本地。实测
  `APP_VERSION=local docker compose build --no-cache`（不带代理）：`load metadata for
  docker.io/library/python:3.12-slim` 这一层 0.0 秒就过了（本机的镜像确实够用），但 `pip install`
  层重跑 137 秒后失败，报 `Could not find a version that satisfies the requirement
  protobuf<6.34,>=6.33.1 (from versions: none)`——那是取不到 PyPI 索引的形状，不是版本冲突
  （`logs_v2/build_nocache_local.log`）。同一份 `--no-cache` 带上代理则一路走通：`pip install` 层
  82.5 秒到 `deps-ok`，apt 层 11.3 秒（`logs_v2/build_proxy_nocache.log` 里 BuildKit 给这两层打的
  自带计时；整条命令的总墙钟我没有单独计时，所以只报这两层的时间）。
  所以"重建只要 1.6 秒"指的是**命中缓存**的重建，`--no-cache` 或换基础镜像时请按 82 秒 + 网络来预期。

## 3. 配置环境变量

密钥与运行参数都来自仓库根的 `.env`（已被 `.dockerignore` 与 `.gitignore` 排除，不进镜像、
不进日志、不进导出）。模板是 `.env.example`。

| 变量 | 默认值 | 作用 | 不设会怎样 |
| --- | --- | --- | --- |
| `OPENAI_BASE_URL` | `https://api.openai.com/v1` | Chat Completions 端点 | 用默认端点 |
| `OPENAI_MODEL` | `gpt-4.1-mini` | 模型名 | 用默认模型名 |
| `OPENAI_API_KEY` | 空 | LLM 通道密钥 | **规则机制全部照常**；C/E/J 组明确报错，不伪造成功 |
| `OPENAI_API_KEY_FILE` | 空 | 从只读文件读密钥（先环境变量、后文件） | 不读文件 |
| `LLM_REQUESTS_PER_MINUTE` | 20 | 本地限流，按上游配额 | 按 20 次/分钟限流 |
| `SOLVER_WORKERS` | 1 | CP-SAT 并行 worker | 单 worker（可复现配置） |
| `SOLVER_MAX_SECONDS` | 6 | 求解墙钟上限 | 6 秒；撞上上限交出的是**未证优**的可行解 |
| `TZ` | `Asia/Shanghai` | 容器时区 | 东八区 |
| `PLANNER_DATA_DIR` | 容器内 `/app/data` | 表格目录（courses/students/simulation_truth/manifest） | 用镜像内置示例数据集播种 |
| `PLANNER_CACHE_DIR` | `/app/semantic_cache` | LLM 语义解析缓存 | 空缓存 |
| `PLANNER_LOG_DIR` | `/app/logs` | 追加式运行日志 `runs.jsonl` | 日志落在卷里 |
| `PLANNER_OUTPUT_DIR` | `/app/outputs` | 保存的运行 | 空 |
| `PLANNER_DATA_ROOT` | `/app` | 表格目录允许越出的根；越界直接被拒 | 只允许 `/app` 之内 |
| `PLANNER_HOST_PORT` | 8501 | 宿主端口 | 8501 |
| `PLANNER_BIND_IP` | `127.0.0.1` | 宿主暴露网卡 | 只绑回环 |
| `APP_VERSION` | `local` | 镜像标签，并烤进镜像的 `ENV`，写进启动日志、运行日志与保存运行的 `config` | 标签 `local`，启动时打印提醒 |
| `SKILL_INPUT_DIR` | `./inputs` | 宿主目录，只读挂到容器 `/app/inputs` | 用仓库里的 `inputs/` |
| `LLM_KEY_FILE` | `./secrets/openai_api_key.txt` | 上表那行挂载注释里的宿主文件路径 | 只在你改用文件挂密钥时才用到 |
| `BUILD_PROXY` | 空 | **只在构建期**：喂给 `HTTP_PROXY`/`HTTPS_PROXY` 两个保留构建参数 | 构建不带代理（能直连 PyPI 时就这样） |
| `BUILD_NETWORK` | `default` | **只在构建期**：compose 的构建网络；只有设成 `host` 时 `BUILD_PROXY` 里的 `127.0.0.1` 才指得到宿主代理 | 走默认桥接网络 |

`BUILD_PROXY`/`BUILD_NETWORK` 只在构建期被 `build.args` 与 `build.network` 读取，**但它们仍然会进入
容器环境**：compose 的 `env_file` 把 `.env` 里每一行都灌进容器，实测容器里 `printenv` 能读到
`BUILD_PROXY` 与 `BUILD_NETWORK`（对运行没有影响：应用与 `requests` 都不读这两个名字）。
由此有一条必须记住的规矩：**不要把 `HTTP_PROXY`/`HTTPS_PROXY` 写进 `.env`**——那会经由 `env_file`
进入容器，让运行期的 LLM 请求悄悄走宿主代理，表现为"界面上配了正确端点却连不上"。要代理就只用
`BUILD_` 前缀这一组名字。**本仓库交付时把 `APP_VERSION=1.0.0`、`BUILD_PROXY`、`BUILD_NETWORK`
三行写进了 `.env`**，原因就是 §2 末尾那条缓存机制：这两个值一变就要重装依赖，写进文件里可以让每次
构建取到同一组值。**这三行都不是密钥**，但 `docker compose config` 会把 `.env` 的内容整个渲染出来，
贴出去之前先看一眼。

密钥的两种放法：

1. 直接写 `.env` 的 `OPENAI_API_KEY=`（本机现在就是这一种）。这条的代价要说在前面：
   `docker compose config` 会把 `env_file` 的值**原样渲染**出来（实测如此，本会话里就出过一次），
   所以任何贴到 issue / 截图 / CI 日志里的 `compose config` 都会连着密钥一起贴出去。
   **本机就发生过这样一次泄露**，密钥值没有留在仓库任何文件里，但服务商侧的轮换请自行完成——
   这一步只能由密钥的所有者做，交付方无法代做。
2. 不想让它出现在 `compose config`：把密钥单独放一个文件，取消 `compose.yaml` 里
   `# - ${LLM_KEY_FILE:-...}:/run/secrets/openai_api_key:ro` 那行的注释，并设
   `OPENAI_API_KEY_FILE=/run/secrets/openai_api_key`。应用先读环境变量、再读该文件；启动日志会写明
   密钥来自哪里（只写来源，不写值）。
   **这一条本机只测到"解析顺序"，没测到"挂载"**：`tests/test_deployment.py::SecretResolution`
   测过环境变量优先、文件回退、文件不存在时返回空串三种取值，界面也确实经由同一个解析函数读密钥；
   但"只读 bind mount + `.env` 里不写密钥"这条完整部署路径本机没有实跑过（本机用的是第 1 种放法）。
   要用第 2 种，请先在自己的机器上按 §4 起来一次、确认启动日志那行读的是文件而不是环境变量。

页面侧栏"LLM 连接设置"里的 API Key 输入框只活在那一次浏览器会话内存里，不写文件；
它的初值来自上面两个来源之一，所以容器部署时不需要在界面上粘密钥。

## 4. 启动服务

```bash
docker compose up -d          # 后台启动；镜像不存在时会自动构建
docker compose ps             # STATUS 应为 Up ... (healthy)
```

启动即验收：镜像里有 `HEALTHCHECK`，打的是 Streamlit 自带的 `/_stcore/health`，
返回体必须是 `ok` 才算健康（`start_period=25s`）。首次 `up` 时入口脚本会把镜像内置的示例数据集
**不覆盖地**播种进 `PLANNER_DATA_DIR`（`cp -rn`），已有同名文件一律不动。

真实启动日志（`docker compose logs planner` 的收尾几行，逐字照抄，仅去掉了每行前面的
`planner-1  |` 前缀；模型名与容器内网地址按当次运行原样出现）：

```
[entrypoint] AI 带回计划经济 1.0.0
[entrypoint] PLANNER_DATA_DIR=/app/data
[entrypoint] PLANNER_CACHE_DIR=/app/semantic_cache
[entrypoint] PLANNER_LOG_DIR=/app/logs
[entrypoint] PLANNER_OUTPUT_DIR=/app/outputs
[entrypoint] PLANNER_DATA_ROOT=/app
[entrypoint] LLM 密钥已配置：是（来自环境变量，模型 agnes-3.0-flash，限流 20 次/分钟）
[entrypoint] 求解器 SOLVER_WORKERS=1 SOLVER_MAX_SECONDS=6
[entrypoint] 容器内监听 0.0.0.0:8501； 对外暴露到哪个网卡由 compose 的端口映射决定，默认只映射宿主 127.0.0.1
2026-10-18 15:47:10.366 Uvicorn server started on 0.0.0.0:8501

  You can now view your Streamlit app in your browser.

  Local URL: http://localhost:8501
  Network URL: http://172.20.0.2:8501
  External URL: http://222.205.120.7:8501
```

最后那行 `External URL: http://<宿主出口IP>:8501` 是 Streamlit 自己猜的显示文本，
**不代表端口被公开了**：实际发布映射由 `docker compose ps` / `docker port` 决定，默认只有
`127.0.0.1:8501`。

## 5. 访问地址与端口

| 从哪里访问 | 地址 | 实测 |
| --- | --- | --- |
| 宿主浏览器（Windows/WSL） | `http://127.0.0.1:8501/` | `curl http://127.0.0.1:8501/` → 200；`curl http://127.0.0.1:8501/_stcore/health` → `ok` |
| 健康检查/探针 | `http://127.0.0.1:8501/_stcore/health` | 返回 `ok` |
| 容器内 | `http://127.0.0.1:8501`（`PLANNER_PORT` 默认 8501） | 同上 |
| 同局域网其他机器 | 默认**不通**，因为只绑回环 | 需要显式改，见下 |

改端口：`PLANNER_HOST_PORT=8600 docker compose up -d`。

给局域网开放（要明确承担后果）：`PLANNER_BIND_IP=0.0.0.0 docker compose up -d`。
**这个应用没有身份认证、没有多租户、没有访问控制**，公开监听等于把"谁都能改侧栏参数并触发一次
求解"交给整个网段。当前形态不具备公网生产部署所需的认证与传输保护，**不应对外网提供服务**；
确实要给同事看时，请放在受控网段，或用 SSH 隧道：`ssh -L 8501:127.0.0.1:8501 <宿主>`。

## 6. 查看日志

进程日志（含入口脚本的环境自检）：

```bash
docker compose logs -f planner
docker compose logs --tail 100 planner
```

应用自身的结构化运行日志——追加式 JSONL，一行一个事件，落在 `PLANNER_LOG_DIR/runs.jsonl`：

```bash
docker compose exec -T planner tail -n 20 /app/logs/runs.jsonl
docker compose exec -T planner python -c "import runlog; [print(r) for r in runlog.recent(20)]"
```

事件类型：`run_start`、`run_end`、`run_failed`、`run_saved`、`run_save_failed`、`log_write_error`。
真实的一对开始/结束记录（逐字照抄，仅为排版折行；模型名不是密钥，按当次运行原样出现）：

```json
{"ts": "2026-10-18T15:47:52", "event": "run_start", "run_id": "20261018T154752-57f900", "source": "ui",
 "mode": "B 规则式集体规划（选问题）", "data_source": "合成生成", "n_students": 72, "seed": 7, "table_dir": "",
 "question_budget": 30, "questions_per_round": 4, "sim_replicates": 3, "preset_occupies_seat": false,
 "round2_enabled": true, "llm_model": "agnes-3.0-flash", "llm_key_present": true, "solver_workers": "1",
 "solver_max_seconds": "6", "app_version": "1.0.0"}
{"ts": "2026-10-18T15:47:53", "event": "run_end", "run_id": "20261018T154752-57f900", "source": "ui",
 "mode": "B 规则式集体规划（选问题）", "duration_ms": 96, "questions": 5, "llm_calls": 0,
 "llm_fallback": false, "llm_failure_count": 0, "solver_status": "OPTIMAL", "recheck_violations": 0,
 "hard_violations": 0, "final_total_utility": 12955.0}
```

要点：`app_version` 让"这批结果是哪一版软件跑出来的"可追溯（§8 的探针实测过它的取值跟着构建走）；
`llm_calls` 与 `llm_fallback` 是**分开计数**的——回退不是把失败藏起来，而是另记一笔，
页面上那一条会显示"回退"，导出 CSV 里 `valid_for_ai_claim` 为 `False`。日志写入前会做密钥脱敏，
写入失败也不会打断业务（降级成 stderr 并记 `log_write_error`）。

导出目录（保存的运行）与语义缓存的落点：

```bash
docker compose exec -T planner sh -c 'ls /app/outputs; ls /app/semantic_cache'
```

## 7. 停止与重启

```bash
docker compose stop                # 停止，容器与卷都保留
docker compose start               # 再用同一个容器起来
docker compose restart             # 原地重启（配置未变时用这个）
docker compose down                # 删除容器与网络；【卷保留】——数据不会丢
docker compose down -v             # 危险：连四个命名卷一起删（表格、缓存、日志、已保存运行全没）
```

`docker compose down` 之后 `up -d` 会重新创建容器，数据全部回来，这一点是实测过的，见 §9。
入口脚本每次启动都会重跑一遍自检并打印目录、密钥来源、求解器参数，
所以"重启后到底用什么配置"永远能从 `docker compose logs` 第一屏读出来。

## 8. 更新版本

版本号进三处：镜像标签、容器启动日志、每一次运行日志与保存运行的 `config`
（`app_version` 字段），所以"这批结果是哪一版软件跑出来的"永远可追溯。

compose 的变量插值优先级是 **shell 环境变量 > 点文件（项目 `.env`）> `${VAR:-默认}`**，三条都用
`--env-file` 探针实测过（文件里只写 `APP_VERSION=from-dotfile`）：只给点文件 →
`docker compose --env-file /tmp/probe.env config --images` 输出 `ai-course-planner:from-dotfile`；
同时给 shell 变量 → 输出 `ai-course-planner:from-shell`；两者都不给 → 输出
`ai-course-planner:local`（本机 `.env` 里当时没有 `APP_VERSION` 这一行，所以走默认）。
把版本号写进 `.env` 就不用每次重复传：

```bash
cd /path/to/ai_course_planner_mvp
# 0. 取新代码（.env 不在版本控制里，拉代码不会动它）
# 1. 改 .env：APP_VERSION=1.1.0，并把下面两个代理值保持与上次构建一致
# 2. 重建：版本号进标签、启动日志、运行日志与每次保存运行的 config
docker compose build
# 3. 用新镜像重建容器（卷不动）；compose 检测到镜像 ID 变了会自动 Recreate，不需要手动 down
docker compose up -d
# 4. 起来后立刻验收
docker compose ps
docker compose exec -T planner python deployment_acceptance.py
```

**这一步的坑在代理参数，不在版本号**：`BUILD_PROXY`/`BUILD_NETWORK` 变了会把 pip 层打掉、
重装依赖需要外网（机制与实测对照见 §2 末尾）。所以本仓库把这两个值与 `APP_VERSION` 一起写进 `.env`，
`docker compose build` 单独执行时行为与带前缀时一致。**换机器或删过 `.env` 时，第一次构建请按 §2
那条带代理的命令走，别先跑不带代理的 `docker compose build`。**

版本号的传递链实测过一整条（用一个故意不像版本号的探针值 `probe-9.9`，这样读到它就不可能是默认值）：

```bash
APP_VERSION=probe-9.9 docker compose build --progress=plain   # pip 与 apt 两层都 CACHED
APP_VERSION=probe-9.9 docker compose up -d
docker compose ps --format "{{.Image}}"                          # → ai-course-planner:probe-9.9
docker compose logs --tail 40 planner | grep "AI 带回计划经济"     # → [entrypoint] AI 带回计划经济 probe-9.9
docker compose exec -T planner printenv APP_VERSION              # → probe-9.9
docker compose exec -T planner python deployment_acceptance.py   # → 通过 9 / 失败 0 / 未执行 0
docker compose exec -T planner sh -c 'grep -o "app_version[^,]*" /app/logs/runs.jsonl | tail -2'
# → app_version": "probe-9.9"  ← 验收里那一次真运行的日志也带上它
```

原始输出留在 `logs_v2/build_probe_version.log` 与 `logs_v2/acceptance_probe_version.log`。
**注意版本号的来源只有一个：构建参数**。`APP_VERSION` 由 `Dockerfile` 末尾的
`ENV APP_VERSION=${APP_VERSION}` 烤进镜像，容器读的就是烤进去的那个值；compose 的
`environment:` 里**没有** `APP_VERSION`，所以 `docker compose up -d` 时临时传一个不同的
`APP_VERSION` 只会改变 compose 去找哪个**标签**，不会改变镜像内部报的版本。这是刻意的：
软件的版本由构建它的那次 `build` 决定，运行时改标签不改代码，就不该让日志里的版本号说谎。
要换版本，请按上面的顺序 build 完再 up。

回滚：旧镜像还在本机时，直接 `APP_VERSION=<旧号> docker compose up -d` 即可回到那一版（标签与镜像内
版本号本来就是一致构建的）；这也是正式部署要打真实版本号而不是默认 `local` 的原因——`local` 每次重建
都会覆盖同名标签，回滚时无法只凭标签区分新旧。清理不再使用的镜像：`docker image prune -f`
（只删悬空层，不动有标签的）。探针版本用完是这么清的：`APP_VERSION=local docker compose up -d`
把容器换回交付镜像，再 `docker rmi ai-course-planner:probe-9.9`。

配置变更（不改代码）不需要重建镜像：编辑 `.env` 后 `docker compose up -d`（compose 会重建容器），
或 `docker compose restart`。

## 9. 数据备份与恢复

需要跨重启存活的状态全部在四个命名卷 + 一个只读绑定目录里：

| 内容 | 位置 | 说明 |
| --- | --- | --- |
| 学生与课程表格、`.skill` 正文 | 卷 `ai-course-planner_planner_data`（容器内 `/app/data`） | 首次启动从镜像内置示例**不覆盖地**播种 |
| 真实输入（`.skill` + 课程表） | 宿主 `./inputs` → 容器 `/app/inputs`（只读） | 界面"表格目录"填 `inputs/<目录>`；越出 `/app` 被拒 |
| LLM 语义解析缓存 | 卷 `ai-course-planner_planner_cache` | 丢了只是重新计费，不影响正确性 |
| 运行日志 | 卷 `ai-course-planner_planner_logs` | 追加式 JSONL |
| 保存的运行 | 卷 `ai-course-planner_planner_outputs` | 每个运行一个目录：`config.json`、`run_summary.json`、若干 CSV |

备份（不需要额外镜像，用应用镜像自带的 tar；在项目目录里执行）。这条备份/恢复闭环实测过：
把 `logs` 卷备份成 tgz，再在容器内恢复到临时目录，恢复出的 `runs.jsonl` 与卷内文件 `cmp` 逐字节一致
（脚本 `results_batches/_selftest_backup_restore.sh`，原始输出 `logs_v2/backup_restore_100.log`）：

```bash
for v in data semantic_cache logs outputs; do
  docker compose exec -T planner tar czf - -C /app "$v" > "backup-${v}.tgz"
done
```

恢复：

```bash
docker compose up -d
for v in data semantic_cache logs outputs; do
  cat "backup-${v}.tgz" | docker compose exec -T planner sh -c "cat | tar xzf - -C /app"
done
docker compose restart
```

卷级备份（停机窗口内做，更彻底）：`docker run --rm -v ai-course-planner_planner_outputs:/from \
-v "$PWD":/to alpine tar czf /to/outputs.tgz -C /from .`——这条需要先本机有 `alpine` 镜像，
本机 Docker 守护进程取不到 Docker Hub，所以**上面的 tar 方案才是实测过的那条**。

恢复之后的核对（实测过，见 `logs_v2/restart_persistence_100.log`）：

```bash
docker compose exec -T planner python deployment_acceptance.py --stage save   # 存一份探针运行并打印每个文件的 sha256
docker compose down && docker compose up -d                                   # 容器被删除重建，卷保留
docker compose exec -T planner python deployment_acceptance.py --stage verify # 再读一遍并打印同样的 sha256
docker compose exec -T planner python deployment_acceptance.py --stage clean  # 核对完删掉探针
```

两次 `DIGEST …` 行逐字相同，就是"重建容器后内容没变、仍可读取"的证据；`verify` 还会用
`run_store.read_frame()` 把 CSV 按列名解析回来。这一条同时覆盖了"页面刷新或服务重启后已保存结果
仍可按设计访问"的要求——界面上的"已保存的运行"下拉框读的就是同一份索引。

备份要点：`.env` **不在**这些卷里（它在项目目录，且被 `.dockerignore`/`.gitignore` 排除），
迁移机器时要单独带走；`results*` 目录里的实验数据同样不在卷内，它们是研究产物，
只有一个副本，请按 README §8 的记录方式另行归档。

## 10. 常见错误及排查

| 现象 | 原因 | 处理 |
| --- | --- | --- |
| 构建时 `pip … Read timed out`（`files.pythonhosted.org`） | 默认桥接网络里取包不稳 | 用 §2 的 `BUILD_PROXY` + `BUILD_NETWORK=host`，或 `PIP_ADDITIONAL_ARGS=--index-url <可达源>` |
| 构建时 apt 报 `502` / `Connection failed` | 代理挡在 apt 前面或 deb.debian.org 不可达 | `APT_MIRROR=https://mirrors.tuna.tsinghua.edu.cn`，或 `APT_PACKAGES=` 整层跳过（实测可用） |
| 构建报 `"/data": not found` | `data/` 被 `.dockerignore` 排除，`COPY data/` 就取不到 | 不要把 `data/` 放回 `.dockerignore`；镜像要靠它播种示例数据集 |
| 构建报拉 `docker.io/docker/dockerfile:1` 前端失败 | `# syntax=` 行强制联网取前端 | 本文件刻意不写该行；不要加回去 |
| 每次 `build` 都重装依赖（很慢且要外网） | 换了 `BUILD_PROXY`/`BUILD_NETWORK` 的值，或改了 `requirements.txt` 之前的层 | 同一台机器保持一致；依赖层没变时应看到 `CACHED` |
| `docker compose up` 起不来，端口占用 | 宿主 8501 已被别的进程占用 | `PLANNER_HOST_PORT=8600 docker compose up -d` |
| 容器一直 `unhealthy` | 应用没起来（看日志）或 `start_period` 内被判定 | `docker compose logs --tail 80 planner`；在容器内测 `python -c "import urllib.request as r; print(r.urlopen('http://127.0.0.1:8501/_stcore/health').read())"` |
| 界面"导入表格目录"报 `表格目录 … 超出允许的数据根` | 防越界设计：路径解析后必须在 `PLANNER_DATA_ROOT` 之内 | 把表格放进 `inputs/`（相对根），或设 `PLANNER_DATA_ROOT` 指向真正的数据根 |
| 报 `数据契约校验失败：… 缺少必需列 […]` | 表头/字段不符合契约 | 按错误里列出的列名补齐；`data/` 就是可对照的样例 |
| 有 `courses.csv` 但缺 `simulation_truth.csv` | 隐藏真值不在部署数据里 | 入口脚本会在启动日志明确警告；导入模式会在契约检查处报错，不会静默 |
| 保存运行时报 `… 已保存过一次运行，拒绝覆盖` | 防静默覆盖设计（与实验纪律一致） | 换运行编号，或确认要覆盖时按 `run_store.save_run(..., overwrite=True)` 的显式语义处理 |
| C/E/J 组报 `未配置 API key` | 这些臂必须要 LLM | 配 `.env` 或密钥文件；不需要 AI 时用 B/F 组，其余机制离线照常 |
| LLM 那一条显示"回退"、`llm_fallback=true` | 通道超时/被限流/返回不合规 | 这不是成功也不是崩溃：该行 `valid_for_ai_claim=False`，不进入任何 AI 效果结论；查 `logs/runs.jsonl` 里的 `llm_failure_count` |
| 求解状态是 `FEASIBLE` 而非 `OPTIMAL` | 撞了 `SOLVER_MAX_SECONDS=6` 的墙钟上限 | 交出的仍是满足硬约束的可行解但**未证优**；要更优解就调大时限或降 `SOLVER_WORKERS` 争用 |
| 界面刷新后结果没了 | 页面状态在浏览器会话里，不在盘上 | 用"保存本次运行到导出目录"，重启后从"已保存的运行"下拉框读回 |
| 硬约束是否被求解器之外验证过 | 每次运行都用 `selection_sim.check_final_constraints()` 对预置与最终课表**各自独立复核** | 复核结果记在日志的 `recheck_violations` 与页面的异常清单；不为 0 时页面会报出来 |
| `.env` 里写了 `HTTP_PROXY`/`HTTPS_PROXY` 后，容器里的 LLM 请求连不通 | `env_file` 把 `.env` 每一行都灌进容器，运行期流量被代理劫走 | 把代理改名成 `BUILD_PROXY`（只在构建期被读取），重建容器后再验证 |
| 表格、缓存、日志、已保存运行全部消失 | 执行过 `docker compose down -v`：它连四个命名卷一起删 | 卷内容无从恢复，只能重新播种示例数据；所以日常停止/重启/升级都用不带 `-v` 的命令（§7） |

---

## 附：交付时的验收执行记录（全部为本机真实结果，2026-10-18 15:47–15:55，交付镜像 `ai-course-planner:1.0.0`）

- 容器内全套单测：`docker compose exec -T planner python -m unittest discover -s tests -t .`
  → `Ran 181 tests in 27.625s ... OK`（`logs_v2/unittests_container_100.log`）；开发机同日
  `Ran 181 tests in 22.360s ... OK`（`logs_v2/unittests_host_100.log`）。
- 容器内交付守卫：`docker compose exec -T planner sh -c 'cd /app && PYTHON=python bash
  results_need_v2/_run_delivery_guards.sh'` → `ALL-GUARDS-PASS`、「失败 0 条」
  （`logs_v2/delivery_guards_container_100.log`）；开发机同一套也是 `ALL-GUARDS-PASS`
  （`logs_v2/delivery_guards_host_100.log`）。
- 部署验收九项：`docker compose exec -T planner python deployment_acceptance.py`
  → 通过 9 条、失败 0 条、未执行 0 条（`logs_v2/acceptance_container_100.log`；同日更早的两轮
  9/9 分别是 `acceptance_container_1415.log` 与 `acceptance_probe_version.log`——后者是版本号
  传递链探针那一次，见 §8）。
- 重启不丢状态：`--stage save` → `docker compose down` → `up -d` → `--stage verify` → `--stage clean`
  → rc=0，三份文件的 sha256 前后各出现两次且逐字相同（`logs_v2/restart_persistence_100.log`，
  更早一轮在 `restart_persistence_1435.log`）。
- 版本号传递链：`APP_VERSION=probe-9.9 build → up → ps/logs/printenv/运行日志` 四处读到的都是
  `probe-9.9`（§8，`logs_v2/build_probe_version.log`、`logs_v2/acceptance_probe_version.log`）。
- 镜像内不含密钥：`results_batches/secret_scan.py` 在容器里扫 257 个文本文件，敏感变量名单为空
  （`logs_v2/secret_scan_container_100.log`）；镜像内没有 `.env`（只有 `.env.example`）；
  `docker image inspect` 的 Env 里唯一带 KEY 的是基础镜像自带的 `GPG_KEY`。运行时的
  `OPENAI_API_KEY` 由 `.env` 经 `env_file` 注入，不落镜像、不落日志。
- 从 Windows 侧验证回环可达：`Invoke-WebRequest http://127.0.0.1:8501/_stcore/health` → `200 ok`，
  根路径 → `200`（6467 字节）；镜像内 `.dockerignore` 存在，`test_deployment.py` 的
  `compose_ports_mapping()` 在容器里同样读到 `ports:` 块（随全套单测通过）。
- 界面点击路径未做自动化：本环境的浏览器前端给出 0×0 视口，Streamlit 不渲染侧栏，
  所以"真人点按钮"这一条按环境限制记为未执行；HTTP 层可达（200 + `ok`）与界面逻辑
  都由无头 `AppTest` 在**交付镜像内**跑通。
