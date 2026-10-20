# AI 预置课表规划系统

一句话：**预置课表由 LLM 生成**，再用 LLM 模拟两轮选课（填志愿 / 筛选中签）、打满意度，并按满意度复盘改写预置。
整条链路只有 AI 一个通路，代码只做事实校验（班是否存在、有没有空位、是否重复持有、时间冲突、志愿超份数上限），
**不存在规则兜底**。课程、教学班、时间、教师、容量来自教务系统抓取（2026-2027 秋冬，2173 门 / 4291 班 / 160 专业），
只有学生 `.skill` 是合成的。

本文是这个仓库唯一的说明入口；制度口径的机器可读版本在 `planner/policy.py` 与 `data/courses.json`
的 `plan.need_rules` 里，实验细节见下方「文档索引」。

## 先读这三条边界

1. **结果不可复现**：谁中签由模型决定，同一份输入两次跑出的名单可能不同；`seed` 只固定合成学生那部分。
2. **轮间分差不是改进证据**：正式跑的 30.8 → 35.7 里混着模型自身的抽样波动，判定它需要配对多种子重跑。
3. **两轮机制未被证明起作用**（诚实负结果）：n=1800 正式跑里第 1 轮与第 2 轮的事件流 sha256 完全相同，
   全程 `drops` 0、预置保留率 100%——筛选器面对的是没有竞争的情形。

## 用 Docker 跑

```bash
cd /home/redaunt/ai_course_planner_mvp
docker compose up -d          # 打开 http://127.0.0.1:8765
```

镜像只有 179 MB：运行期一行 pip 都不装（纯标准库），非 root 用户，`/api/state` 做健康检查，
默认只绑宿主回环（这个应用没有身份认证）。

验收构建（假客户端、不联网、不消耗任何配额）：

```bash
docker build --target test -f Dockerfile .   # 构建阶段直接跑 pytest，109 项失败即构建失败
```

### 想把 `.env` 里的密钥带进容器

现行代码只读**仓库根的 `.env` 文件**、不读环境变量（[planner/env_config.py](planner/env_config.py)），
所以密钥的唯一进容器方式是把文件只读挂进去：

```bash
docker compose -f compose.yaml -f compose.dotenv.yaml up -d
```

不带这个 overlay 时容器照样起，页面里逐次填 Base URL / 模型 / Key（密钥只活在那一次请求的内存里），
这是更常用的通路。`.env` 被 `.gitignore` 与 `.dockerignore` 双向排除，因此不进提交、不进构建上下文、
不进任何镜像层；实测 `docker run --rm <image> python -c "os.path.exists('/app/.env')"` 为 False。

### compose 可调参数（都走宿主环境变量或 `.env` 的插值）

| 变量 | 默认 | 用途 |
|---|---|---|
| `PLANNER_HOST_PORT` | `8765` | 宿主端口 |
| `PLANNER_BIND_IP` | `127.0.0.1` | 改成 `0.0.0.0` 前请先确认网络边界（无认证） |
| `APP_VERSION` | `local` | 镜像标签 |
| `BASE_IMAGE` | `python:3.12-slim` | 取不到 Docker Hub 时换成本地已有镜像 |
| `PIP_ADDITIONAL_ARGS` | 空 | 国内源装 pytest，例：`--index-url https://pypi.tuna.tsinghua.edu.cn/simple` |
| `BUILD_PROXY` + `BUILD_NETWORK=host` | 空 | 构建期代理；只给 BuildKit 用，不进运行环境 |

## 本机跑

```bash
.venv/bin/python server/demo_server.py --port 8765      # 浏览器打开 http://localhost:8765
.venv/bin/python -m planner.cli --n 10 --seed 7 --courses data/courses.json --out /tmp/result.json
.venv/bin/python -m pytest tests/ -q                    # 109 项离线测试
```

`.venv` 在 WSL 内创建，只装 pytest（唯一开发依赖）；核心与服务器只用标准库，LLM 调用走 `urllib`。
依赖钉在 [requirements.txt](requirements.txt)。

### AI 配置（项目根 `.env`）

`cp .env.example .env` 后填三项：`OPENAI_BASE_URL`、`OPENAI_MODEL`、`OPENAI_API_KEY`（可选
`LLM_REQUESTS_PER_MINUTE`，换算成请求最小间隔，20 → 3 秒）。优先级：页面输入 / CLI 参数 > `.env`。
API Key 只在内存中透传：不打印、不写日志、不进任何 API 响应，页面只显示"来自 .env：是/否"。
上游限额 20 请求/分，超配额只会排队变慢、严重时整次演示以错误码 2 退出——没有兜底。

## 数据与实验

```bash
bash tools/rebuild_zju_datasets.sh        # zju_catalog 抓取原始件 → data/courses.json + 缩尺档
```

长跑实验走独立入口（不受演示 2–60 人护栏约束，逐轮增量落盘 + 检查点续跑）：`tools/run_experiment.py`，
参数看 `--help`。n=1800 正式跑的完整口径、逐类缺口与逐进程事故账在
[docs/实验披露_n1800.md](docs/实验披露_n1800.md)，结果本体在 `results/exp_k10_n1800_seed7.json`。

## 目录

| 路径 | 内容 |
|---|---|
| `planner/` | 核心包（纯标准库）：模型、时间网格、制度口径、AI 排课/行为/筛选/打分/复盘、LLM 客户端 |
| `server/` `demo/` | 标准库 HTTP 演示服务器（8765）+ 单页 HTML |
| `tools/` | 教务数据抓取转换、人数推算、规模档长跑实验、文书转 PDF |
| `tests/` | 109 项离线测试（假客户端，不消耗配额）；`pytest.ini` 已排除 `legacy/` |
| `data/` | `courses.json`（真实目录 + 培养方案目标 + need_rules）与三档缩尺、人数推测 |
| `docs/` | 作品介绍文档、实验披露_n1800（各含 PDF）——其余文书的缺失见下方「文档缺口」 |
| `files/` | 申报附件（原创承诺书 PDF/docx、申报书 docx）与 `演示视频.mp4`（143 MB）——**整体不入库** |
| `results/` | 正式跑结果 JSON + 检查点 + 日志（**2026-10-20 起进 git 追踪**，此前只有单份副本） |
| `zju_catalog/` | 教务抓取原始件，转换器的输入，**数据的唯一真源**（107 MB，不入库、不进镜像） |
| `legacy/` | 旧版 Streamlit 系统的记录与实验输出（只读归档，驱动脚本已删，**不可重跑**） |
| `backups/` | 口径修正前的数据快照（不入库） |
| `Dockerfile` `compose.yaml` `compose.dotenv.yaml` `.dockerignore` | 容器化交付 |

## 文档索引

| 文档 | 讲什么 |
|---|---|
| [docs/实验披露_n1800.md](docs/实验披露_n1800.md) | 正式跑的完整口径、逐类缺口与逐进程事故账 |
| [docs/作品介绍文档.md](docs/作品介绍文档.md) | 作品介绍 |
| [data/README.md](data/README.md) | `courses.json` 字段格式与需求口径对照 |
| [legacy/ARCHIVE_NOTE.md](legacy/ARCHIVE_NOTE.md) | 归档边界：哪些还能读、哪些已经跑不起来 |
| [legacy/DEPLOYMENT.md](legacy/DEPLOYMENT.md) | 旧 Streamlit 形态的部署记录：端口、镜像名与 `PLANNER_*` 变量都已失效，只作构建参数取舍的史料 |

### 文档缺口（2026-10-20，必须披露）

本文原先有一份 3.2 万字符的长叙述（九类课程与两轮选课的口径原文、数据来源与重建方法、需求口径的四条更正
及其对照表、标定档吞吐、n=1800 终值表、边界与诚实声明全文）。它在本文件拆分移入 `docs/` 的当天，
被同目录里另一次改动删除，未恢复；同批消失的还有 `docs/申报书.md`、`docs/技术路线.md`、
`docs/演示视频脚本.md` 与它们的 PDF（截至本次改动，`docs/` 只剩上面表格里那两个）。

现在还能查到的口径来源只有代码与数据本身：

- `planner/policy.py`：`MAX_PREFS_PER_COURSE` / `MAX_PREFS_PER_CATEGORY` / `PRIORITY_TIERS`（志愿上限与优先级分组）。
- `planner/timegrid.py`：周一至周五 × 每天 13 节的网格。
- `data/courses.json` 的 `plan.need_rules`、`_meta.dropped`、`_meta.capacity_source`、`_meta.year_proxy_assumption`：
  四条需求口径更正、剔除统计、容量快照口径与跨届代理假设，理由逐条写在数据里。
- `data/README.md` 与 `docs/实验披露_n1800.md`：需求/供给对照与正式跑的全部数字。

**这份缺失本身要算进可信度**：上面「先读这三条边界」只是摘要，不是完整披露。
