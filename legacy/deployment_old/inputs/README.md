# 真实数据投放目录（容器里只读挂在 `/app/inputs`）

放一个子目录进去，例如 `inputs/2026秋-软院3班/`，里面是：

| 文件 | 必需 | 字段 |
| --- | --- | --- |
| `courses.csv` | 是 | `course_id, course_name, category, time_slot, capacity, topics, prerequisites` |
| `students.csv` | 是 | `student_id, year, priority_group, priority_tier, interests, target_courses, required_courses, completed_courses, declared_courses, initial_scores` |
| `simulation_truth.csv` | 是 | `student_id, true_scores`（仿真隐藏真值，只用于模拟学生应答与离线评价，**永不进入任何规划控制器或 LLM 提示词**） |
| `manifest.json` | 否 | `{"seed": <整数>}`，记录这份表格对应的随机种子 |
| `skills/<student_id>.skill` | 否 | 每名学生一份纯文本 `.skill`。存在就以它为准，不存在时正文由表格字段**合成**——界面会按来源分别标注，不混着当真实数据 |

列表类字段写成 JSON 数组（`["C01","C02"]`）或用 `|` 分隔；分数类字段写成 JSON 对象（`{"C01": 72}`）。
学生 ID 若要配 `skills/` 目录，只能以字母数字开头、其后接 `字母数字 . _ -`，否则会被拒绝而不是当成路径。

在界面侧边栏选“导入表格目录”，表格目录填 `inputs/2026秋-软院3班`（相对允许的数据根，默认是应用目录 `/app`）。
