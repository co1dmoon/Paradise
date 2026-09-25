"""Exchange-date suggestions, parsing and Russian formatting (§5.2).

Every function takes ``today`` explicitly: the caller computes it in Moscow time
with ``local_today`` so tests stay deterministic.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from enum import StrEnum
from zoneinfo import ZoneInfo

MOSCOW = ZoneInfo("Europe/Moscow")
MAX_DAYS_AHEAD = 180
SEASON_DATES = ((12, 20), (12, 25), (12, 26), (12, 27), (12, 28))
SEASON_FIRST = (10, 1)
SEASON_LAST = (12, 25)
OFF_SEASON_OFFSETS = (7, 14, 21)

_MONTHS_GENITIVE = (
    "января", "февраля", "марта", "апреля", "мая", "июня",
    "июля", "августа", "сентября", "октября", "ноября", "декабря",
)
_WEEKDAYS_SHORT = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")
_DATE = re.compile(r"^\s*(\d{1,2})[./\-\s](\d{1,2})(?:[./\-\s](\d{4}|\d{2}))?\s*\.?\s*$")


class DateError(StrEnum):
    FORMAT = "format"
    TOO_EARLY = "too_early"
    TOO_LATE = "too_late"


def local_today(now: datetime, tz: ZoneInfo = MOSCOW) -> date:
    return now.astimezone(tz).date()


def suggest_dates(today: date) -> list[date]:
    """Oct 1 – Dec 25: the future dates among 20.12 and 25–28.12; otherwise today +7/+14/+21."""
    if date(today.year, *SEASON_FIRST) <= today <= date(today.year, *SEASON_LAST):
        candidates = (date(today.year, month, day) for month, day in SEASON_DATES)
        return [candidate for candidate in candidates if candidate > today]
    return [today + timedelta(days=offset) for offset in OFF_SEASON_OFFSETS]


def parse_user_date(text: str, today: date) -> date | DateError:
    """Parse ДД.ММ or ДД.ММ.ГГГГ (also '/', '-' or a space, and 2-digit years).

    Without a year the nearest future occurrence is used. The date must lie
    between tomorrow and ``MAX_DAYS_AHEAD`` days ahead.
    """
    match = _DATE.match(text)
    if match is None:
        return DateError.FORMAT
    day, month = int(match.group(1)), int(match.group(2))
    year_text = match.group(3)
    try:
        if year_text is None:
            parsed = date(today.year, month, day)
            if parsed <= today:
                parsed = date(today.year + 1, month, day)
        else:
            year = int(year_text)
            parsed = date(year + 2000 if year < 100 else year, month, day)
    except ValueError:
        return DateError.FORMAT
    if parsed <= today:
        return DateError.TOO_EARLY
    if parsed > today + timedelta(days=MAX_DAYS_AHEAD):
        return DateError.TOO_LATE
    return parsed


def format_date(value: date) -> str:
    """'20 декабря' — used in messages."""
    return f"{value.day} {_MONTHS_GENITIVE[value.month - 1]}"


def format_date_button(value: date) -> str:
    """'20.12, сб' — short form for buttons."""
    return f"{value:%d.%m}, {_WEEKDAYS_SHORT[value.weekday()]}"


def in_season_window(day: date, first: str, last: str) -> bool:
    """Whether ``day`` lies between two 'MM-DD' marks, inclusive; the window may wrap the new year
    (DIGEST_FROM=11-01, DIGEST_TO=01-10 covers November to January 10)."""
    mark = (day.month, day.day)
    start, end = _month_day(first), _month_day(last)
    if start <= end:
        return start <= mark <= end
    return mark >= start or mark <= end


def _month_day(text: str) -> tuple[int, int]:
    month, day = text.split("-")
    return int(month), int(day)
