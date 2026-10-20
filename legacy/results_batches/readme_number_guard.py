"""Guard: every number in README §7 must be lifted from a generated report, not typed from memory."""
import os
import re

# 从脚本自身位置推出仓库根，别写死开发机目录：镜像里没有 /home/redaunt。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS = {"1-40": f"{ROOT}/results/PERFORMANCE_REPORT.md",
           "41-80": f"{ROOT}/results_confirmatory_41_80/PERFORMANCE_REPORT.md"}


def contrasts(path):
    out = {}
    for line in open(path, encoding="utf-8"):
        if not line.startswith("| ") or " - " not in line:
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 9 or "有效场景数" in cells[0]:
            continue
        parts = cells[0].split(" - ")
        out[(parts[0], parts[1])] = {"mean": cells[3], "wlt": cells[7], "p": cells[8]}
    return out


def arms(path):
    """Section-4 metric rows, keyed by mechanism name via the header column order."""
    lines = open(path, encoding="utf-8").read().splitlines()
    header = next(l for l in lines if l.startswith("| 指标 | "))
    names = [c.strip().split()[0] for c in header.strip().strip("|").split("|")[1:]]
    out = {}
    for key in ("最终总偏好效用（两轮后均值）", "低分位(p10)学生效用", "关键需求满足率(%)"):
        row = next(l for l in lines if l.startswith("| " + key))
        cells = [c.strip() for c in row.strip().strip("|").split("|")[1:]]
        out[key] = {n: c for n, c in zip(names, cells)}
    return out


report = {label: contrasts(path) for label, path in REPORTS.items()}
per_arm = {label: arms(path) for label, path in REPORTS.items()}
readme = open(f"{ROOT}/README.md", encoding="utf-8").read()

problems = []

# Walk README §7 contrast table rows and check the two batch cells.
section = readme.split("## 7.")[1].split("提案漏斗")[0]
label = {"A": "A_现行机制基线", "B0": "B0_一次性优化无交互", "B": "B_规则式集体规划",
         "C": "C_AI集体规划", "D": "D_完整信息参照", "E": "E_LLM协调提案",
         "F": "F_规则协调提案", "G": "G_等预算随机询问", "H": "H_获取信息不改预置"}
n_rows = 0
for raw in section.splitlines():
    if not raw.startswith("| ") or raw.startswith("| 比较 |") or raw.startswith("| ---"):
        continue
    cells = [c.strip() for c in raw.strip().strip("|").split("|")]
    left = cells[0].replace("*", "")
    m = re.match(r"([A-Z0-9]+) − ([A-Z0-9]+)（", left) or re.match(r"([A-Z0-9]+) − ([A-Z0-9]+)$", left)
    if not m:
        continue
    key = (label[m.group(1)], label[m.group(2)])
    n_rows += 1
    for batch, cell in (("1-40", cells[1]), ("41-80", cells[2])):
        got = report[batch].get(key)
        if got is None:
            problems.append(f"{batch} 报告里没有 {key}")
            continue
        nums = re.findall(r"-?\d+\.?\d*", cell.replace("−", "-"))
        if not nums:
            problems.append(f"{key} {batch} 单元格没有数字: {cell}")
            continue
        if abs(float(nums[0]) - float(got["mean"])) > 0.005:
            problems.append(f"{key} {batch} 均值不符 README={nums[0]} 报告={got['mean']}")
        if got["wlt"] not in cell:
            problems.append(f"{key} {batch} 胜负平不符 README={cell} 报告={got['wlt']}")
        if got["p"] not in cell:
            problems.append(f"{key} {batch} p 值不符 README={cell} 报告={got['p']}")

print(f"README §7 配对表核对行数 = {n_rows}（报告侧对比表 {len(report['1-40'])} 行）")

# Means table: | 臂 | n1/n2 | 1-40 均值±sd | 41-80 均值±sd | p10×2 | 必修%×2 | 违规 |
means_section = readme.split("最终总偏好效用 / p10 学生效用 / 必修满足率")[1].split("配对差值")[0]
n_means = 0
for raw in means_section.splitlines():
    cells = [c.strip() for c in raw.strip().strip("|").split("|")]
    if len(cells) != 9 or " | " not in raw or cells[0].startswith("臂") or set(cells[0]) <= {"-"}:
        continue
    tag = cells[0].split()[0]
    n_means += 1
    for batch, util_cell, p10_cell, req_cell in (
            ("1-40", cells[2], cells[4], cells[6]), ("41-80", cells[3], cells[5], cells[7])):
        for key, cell in (("最终总偏好效用（两轮后均值）", util_cell),
                          ("低分位(p10)学生效用", p10_cell),
                          ("关键需求满足率(%)", req_cell)):
            want = per_arm[batch][key][tag]
            if key.startswith("最终"):
                a = [float(x) for x in cell.replace("−", "-").split(" ± ")]
                b = [float(x) for x in want.replace("−", "-").split(" ± ")]
                if any(abs(x - y) > 0.005 for x, y in zip(a, b)):
                    problems.append(f"{tag} {batch} 均值表不符 README={cell} 报告={want}")
            else:
                got = want.split(" ± ")[0]
                if abs(float(cell) - float(got)) > 0.005:
                    problems.append(f"{tag} {batch} {key} 不符 README={cell} 报告={got}")
print(f"README §7 均值表核对行数 = {n_means}")
print("问题 =", problems if problems else "无：§7 每个数字都能在同一份报告里找到")

