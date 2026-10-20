# 这里放的是旧 ai_course_planner_mvp（2026-10-18 之前的 Streamlit 版本）

2026-10-20 重组：根目录现在的代码是纯 AI 通路的那套（原 `/home/redaunt/redo`）。
旧版**脚本已删除**（24 个 .py + 入口 shell），本目录只保留不是脚本的东西：

- `README_mvp_v1.md`：旧版研究记录，含预登记的研究问题与**已经失败的结论**（E−F 未复现、需求解析两批均为负）。
  这些是文字结论的唯一副本，所以留着。
- `results*/ logs*/ semantic_cache/ archive_*/`：旧实验输出与日志，单份副本，无版本控制兜底。
- `tests_old/`：旧版测试。它们 import 的模块已删除，因此不可运行，仅作行为规格留档；
  `pytest.ini` 已把 `legacy/` 排除在收集范围之外。
- `data_old/`：旧版合成样例 CSV（不是真实开课目录，真实目录在 ../zju_catalog）。
- `deployment_old/`：旧版 Docker/compose 部署件，对应的 app.py 已删。
- `env_old/`：旧版 .env 与 .gitignore 原件（**未读取内容**，仅改名存放）。

复算说明：旧实验的驱动脚本已删，本目录的数据**只能读、不能重跑**。
