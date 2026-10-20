import re
from pathlib import Path

from planner.timegrid import DAYS, DAY_LABELS

PAGE = Path(__file__).resolve().parents[1] / "demo" / "index.html"


def _page_js(name):
    text = PAGE.read_text(encoding="utf-8")
    match = re.search(rf"const {name} = (\[.*?\]|\{{.*?\}});", text, re.S)
    assert match, f"页面里找不到 const {name}"
    return match.group(1)


def test_demo_page_shows_every_planner_day():
    """页面自己维护一份 DAYS：漏了周末就会静默不画周末的课。"""
    days = re.findall(r'"(\w+)"', _page_js("DAYS"))
    assert days == list(DAYS)


def test_demo_page_labels_every_day_in_chinese():
    labels = dict(re.findall(r'(\w+):"([^"]+)"', _page_js("DAY_CN")))
    assert [labels.get(d) for d in DAYS] == [DAY_LABELS[d] for d in DAYS]


def test_student_card_template_closes_its_details():
    """少一个 </details> 就会让 S2 嵌进 S1、S1 嵌进 S0，页面看起来像卡片叠在一起。"""
    text = PAGE.read_text(encoding="utf-8")
    assert text.count("<details") == text.count("</details>") == 1
