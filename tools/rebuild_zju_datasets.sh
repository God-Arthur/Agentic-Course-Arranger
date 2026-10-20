#!/bin/bash
# 重建数据集：整量 + 容量÷10 微缩。数据只读，输出写到 /tmp 再由人工落地。
set -e
cd /home/redaunt/ai_course_planner_mvp
CAP=/home/redaunt/ai_course_planner_mvp/zju_catalog/2026-2027_1/classes_capacity.csv
NOTE='快照 2026-10-19 10:25-10:37；模拟从空班起算，未计已选人数/余量'
COMMON=(--capacity "$CAP" --capacity-note "$NOTE"
        --year-indexes 一,二,三
        --language-per-student 0.33 --pe-per-student 1
        --exclude-grade 四 --exclude-grade 五
        --courses-per-student-cap 8)
.venv/bin/python tools/build_courses_from_zju.py "${COMMON[@]}" \
    --out /tmp/courses_full.json --report /tmp/report_full.json
echo '--- k10 ---'
.venv/bin/python tools/build_courses_from_zju.py "${COMMON[@]}" --capacity-divisor 10 \
    --out /tmp/courses_k10.json --report /tmp/report_k10.json
