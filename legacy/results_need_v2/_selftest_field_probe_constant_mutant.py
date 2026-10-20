"""证明 `report_field_probe.py` 的"整列退化为同一常数"核对**会失败**，不是空转。

只在内存里清空那张解释清单，不复制文件、不改 CSV：
- 负对照：原样 → 期望 rc 0（真探针通过，且确实核对了 solver_seconds）。
- 变异：`CONSTANT_EXPLAINED = {}` → 期望 rc 非 0，且退回原因里同时点名三种性质各自的样本。

只要求"退出码变了"是不够的：随便一条无关失败也能让 rc 非 0，那样这台变异机核对的是别的分支。
所以这里还要求被点名的**列**覆盖到未测量、构造、结果三种，缺一种就说明有一类实际没在被核对。

    .venv/bin/python results_need_v2/_selftest_field_probe_constant_mutant.py
"""
from __future__ import annotations

import contextlib
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
for _path in (ROOT, HERE):
    if _path not in sys.path:
        sys.path.insert(0, _path)

import report_field_probe as probe  # noqa: E402


def run() -> tuple[int, str]:
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = probe.main()
    return rc, buf.getvalue()


def main() -> int:
    if not os.path.isdir(os.path.join(probe.OUT_DIR, "test_15_18")):
        print("SKIP：还没有已收尾分块，这条核对无从跑起。")
        return 2
    rc_real, out_real = run()
    if rc_real != 0:
        print(f"FAIL：真探针在未改动状态下就退回（rc={rc_real}），先把它的输出读完再谈变异：")
        print(out_real)
        return 1
    if "solver_seconds" not in out_real:
        print("FAIL：真探针没有核对 solver_seconds 这一项，变异机要改的东西根本不在核对范围内。")
        return 1

    ledger = dict(probe.CONSTANT_EXPLAINED)
    probe.CONSTANT_EXPLAINED = {}
    try:
        rc_mut, out_mut = run()
    finally:
        probe.CONSTANT_EXPLAINED = ledger
    if rc_mut == 0:
        print("FAIL：清空解释清单后探针仍然通过——那条常数核对是空转。")
        return 1
    problems = [line for line in out_mut.splitlines() if "全是" in line and "而这张清单里没有它" in line]
    named = "\n".join(problems)
    missing = [key for key in ("solver_seconds", "llm_calls", "key_need_recall_pct")
               if key not in named]
    if missing:
        print(f"FAIL：变异让探针退回了（rc={rc_mut}），但退回原因里没有点名 {missing}，"
              "说明这三类常数里有哪一类实际没被核对到。")
        return 1
    print(f"OK：真探针 rc=0 且核对了常数项；清空清单后 rc={rc_mut}，{len(problems)} 项整列常数"
          "全部被退回，未测量/构造/结果三种各有点名样本。")
    print(f"  {problems[0].strip()[:160]}…")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
