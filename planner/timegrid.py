"""时间网格：周一至周五，上午 5 节、下午 5 节、晚上 3 节（发起人 2026-10-19 定：不考虑周末）。"""
from __future__ import annotations

DAYS = ("Mon", "Tue", "Wed", "Thu", "Fri")
DAY_LABELS = {"Mon": "周一", "Tue": "周二", "Wed": "周三", "Thu": "周四", "Fri": "周五"}
_CN_TO_EN = {v: k for k, v in DAY_LABELS.items()}

MORNING_PERIODS = tuple(range(1, 6))
AFTERNOON_PERIODS = tuple(range(6, 11))
EVENING_PERIODS = tuple(range(11, 14))
ALL_PERIODS = MORNING_PERIODS + AFTERNOON_PERIODS + EVENING_PERIODS

Slot = tuple[str, int]


def period_label(period: int) -> str:
    if period in MORNING_PERIODS:
        return f"上午{period}"
    if period in AFTERNOON_PERIODS:
        return f"下午{period - 5}"
    if period in EVENING_PERIODS:
        return f"晚上{period - 10}"
    raise ValueError(f"非法节次：{period}")


def slot_label(slot: Slot) -> str:
    return f"{DAY_LABELS[slot[0]]}{period_label(slot[1])}"


def slot_key(slot: Slot) -> str:
    return f"{slot[0]}-{slot[1]}"


def parse_slot(text: str) -> Slot:
    raw = str(text).strip()
    if "-" not in raw:
        raise ValueError(f"时间槽格式应为 'Mon-3' 或 '周一-3'：{text!r}")
    day_raw, period_raw = raw.split("-", 1)
    day = _CN_TO_EN.get(day_raw.strip(), day_raw.strip().capitalize())
    if day not in DAYS:
        raise ValueError(f"非法星期：{text!r}")
    period = int(period_raw)
    if period not in ALL_PERIODS:
        raise ValueError(f"非法节次：{text!r}（上午1-5、下午1-5、晚上1-3）")
    return (day, period)


def has_conflict(slots_a, slots_b) -> bool:
    return not set(slots_a).isdisjoint(slots_b)


def all_slots() -> list[Slot]:
    return [(day, period) for day in DAYS for period in ALL_PERIODS]
