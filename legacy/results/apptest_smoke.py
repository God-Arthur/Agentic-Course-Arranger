"""Reproducible headless UI check for the paired-contrast table and the significance caption.

    cd <repo> && .venv/bin/python results/apptest_smoke.py

Rule controller only: C/E are excluded from the means, which is also the path that proves
the app refuses to present unrun LLM arms as evidence. Prints one line per stage.
"""
from __future__ import annotations

import os
import sys

APP = os.path.dirname(os.path.abspath(__file__))
REPO = os.path.dirname(APP)
sys.path.insert(0, REPO)
os.chdir(REPO)

from streamlit.testing.v1 import AppTest


def _counts(at, label):
    print(f"[{label}] exceptions={len(at.exception)} dataframes={len(at.dataframe)} "
          f"metrics={len(at.metric)} warnings={len(at.warning)} errors={len(at.error)}")
    for exc in at.exception:
        print("EXC:", exc.value)


def _click(at, *, key=None, label=None):
    for button in at.button:
        if (key is not None and button.key == key) or (label is not None and button.label == label):
            button.click()
            return True
    return False


def main() -> int:
    at = AppTest.from_file(os.path.join(REPO, "app.py"), default_timeout=900)
    at.run()
    _counts(at, "initial")

    _click(at, label="运行集体规划仿真")
    at.run()
    _counts(at, "run B")

    _click(at, key="run_mechanisms")
    at.run()
    _counts(at, "all mechanisms (rule only)")

    contrast = None
    for frame in at.dataframe:
        columns = list(frame.value.columns)
        if "比较" in columns and "符号检验 p(双侧)" in columns:
            contrast = columns
    print("[contrast columns]", "OK" if contrast and "非平局场景数" in contrast else f"MISSING: {contrast}")

    caveat = [c.value for c in at.caption if "符号检验" in c.value and "非平局场景" in c.value]
    print("[caption]", caveat[0] if caveat else "MISSING")
    excluded = [w.value for w in at.warning if "未纳入均值" in w.value]
    print("[excluded rows warning]", "OK" if excluded else "MISSING")
    ok = bool(contrast and caveat)
    print("HEADLESS_APP_OK" if ok else "HEADLESS_APP_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
