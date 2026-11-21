"""Разбор периодов SIAT.

В данных встречаются три формата:
    "2025"      → год        (type "Y"), 2025-01-01 … 2025-12-31
    "2025-Q3"   → квартал    (type "Q"), 2025-07-01 … 2025-09-30
    "2025-M09"  → месяц      (type "M"), 2025-09-01 … 2025-09-30
Всё остальное получает type "X" (неизвестный формат) — строку сохраняем как есть,
чтобы ничего не потерять и потом посмотреть, что это было.
"""

from __future__ import annotations

import calendar
import re
from dataclasses import dataclass
from datetime import date

_Y = re.compile(r"^(\d{4})$")
_Q = re.compile(r"^(\d{4})-Q([1-4])$")
_M = re.compile(r"^(\d{4})-M(0[1-9]|1[0-2])$")


@dataclass(frozen=True)
class Period:
    raw: str
    type: str  # "Y" | "Q" | "M" | "X"
    start: date | None
    end: date | None


def _month_end(y: int, m: int) -> date:
    return date(y, m, calendar.monthrange(y, m)[1])


def parse_period(raw: str) -> Period:
    s = raw.strip()
    if m := _Y.match(s):
        y = int(m[1])
        return Period(raw, "Y", date(y, 1, 1), date(y, 12, 31))
    if m := _Q.match(s):
        y, q = int(m[1]), int(m[2])
        first = 3 * (q - 1) + 1
        return Period(raw, "Q", date(y, first, 1), _month_end(y, first + 2))
    if m := _M.match(s):
        y, mo = int(m[1]), int(m[2])
        return Period(raw, "M", date(y, mo, 1), _month_end(y, mo))
    return Period(raw, "X", None, None)
