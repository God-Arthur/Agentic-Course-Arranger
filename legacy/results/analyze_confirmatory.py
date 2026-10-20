"""判读确认性复跑（种子 41-80）：主判据只用这批数据，合并 1-80 只作探索性附注。
预注册见 results/PREREGISTRATION_CONFIRMATORY_41_80.md。"""
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PROJ = os.path.dirname(HERE)
sys.path.insert(0, PROJ)

from experiments import _sign_p_two_sided, _sign_threshold, load_raw_rows, summarize  # noqa: E402

B3 = os.path.join(PROJ, "results_confirmatory_41_80", "raw_rows.csv")
B40 = os.path.join(PROJ, "results_batches", "merged_1_40", "raw_rows.csv")
KEY = "E_LLM协调提案 - F_规则协调提案"


def show(label, contrast):
    m, k = contrast["sign_test_pairs"], contrast["sign_test_wins"]
    thr = _sign_threshold(m)
    print(f"{label}: 平均差 {contrast['mean_delta_total_utility']:+.2f}  "
          f"sd {contrast['sd_delta']}  se {contrast['se_delta']}  t {contrast['t_paired']}  "
          f"胜/负/平 {contrast['seeds_target_better']}/{contrast['seeds_ref_better']}/{contrast['tie']}  "
          f"有效 n {contrast['n']}  非平局 {m}  门槛 {thr} 胜  p = {contrast['sign_test_p_two_sided']}")
    if m:
        loo = _sign_p_two_sided(m - 1, k - 1)
        print(f"    留一法（剔掉一个 E 胜场）：{m - 1} 局 {k - 1} 胜 → p = {round(loo, 4)}"
              f" → {'仍显著' if loo < 0.05 else '不再显著'}")


def main() -> int:
    if not os.path.exists(B3):
        print(f"还没有批次 3 结果：{B3}")
        return 2
    rows_b3 = load_raw_rows(B3)
    summary = summarize(rows_b3)
    print(f"批次 3 行数 {len(rows_b3)}，种子 "
          f"{sorted({int(r['seed']) for r in rows_b3})[0]}-"
          f"{sorted({int(r['seed']) for r in rows_b3})[-1]}")
    dropped = [e for e in summary["excluded_rows"] if "E_LLM" in e["mechanism"] or "F_规则" in e["mechanism"]]
    print(f"E/F 被剔除的行（LLM 回退）：{[(e['seed'], e['note']) for e in dropped] or '无'}")
    print("\n== 主判据（只用种子 41-80）==")
    c = summary["paired_contrasts"][KEY]
    show("E − F", c)
    verdict = "复现" if c["sign_test_p_two_sided"] < 0.05 else "未复现"
    print(f"预注册判定：{verdict}")

    print("\n== 次要（只报告方向，不作宣称）==")
    for name in ("E_LLM协调提案 - B_规则式集体规划", "E_LLM协调提案 - B0_一次性优化无交互",
                 "F_规则协调提案 - B_规则式集体规划", "B_规则式集体规划 - B0_一次性优化无交互"):
        if name in summary["paired_contrasts"]:
            show(name.split(" - ")[0] + " − " + name.split(" - ")[1], summary["paired_contrasts"][name])

    if os.path.exists(B40):
        pooled = summarize(load_raw_rows(B40) + rows_b3)
        print("\n== 附注：合并 1-80（探索性，不作为主判据）==")
        show("E − F", pooled["paired_contrasts"][KEY])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
