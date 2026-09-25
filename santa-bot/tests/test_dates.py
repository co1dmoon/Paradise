from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app.core.dates import DateError, format_date, format_date_button, local_today, parse_user_date, suggest_dates


def test_season_suggestions_are_future_december_dates() -> None:
    assert suggest_dates(date(2026, 10, 1)) == [
        date(2026, 12, 20), date(2026, 12, 25), date(2026, 12, 26), date(2026, 12, 27), date(2026, 12, 28)]
    assert suggest_dates(date(2026, 12, 21)) == [
        date(2026, 12, 25), date(2026, 12, 26), date(2026, 12, 27), date(2026, 12, 28)]
    assert suggest_dates(date(2026, 12, 25)) == [date(2026, 12, 26), date(2026, 12, 27), date(2026, 12, 28)]


@pytest.mark.parametrize("today", [date(2026, 9, 30), date(2026, 12, 26), date(2027, 2, 10)])
def test_off_season_suggestions_are_weeks_ahead(today: date) -> None:
    assert [(d - today).days for d in suggest_dates(today)] == [7, 14, 21]


TODAY = date(2026, 11, 20)


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("25.12", date(2026, 12, 25)),
        (" 5.1 ", date(2027, 1, 5)),
        ("25/12", date(2026, 12, 25)),
        ("25-12-2026", date(2026, 12, 25)),
        ("25.12.26", date(2026, 12, 25)),
        ("21.11", date(2026, 11, 21)),
        ("20.11", DateError.TOO_LATE),  # next year's 20.11 is beyond 180 days
        ("20.11.2026", DateError.TOO_EARLY),
        ("19.11.2026", DateError.TOO_EARLY),
        ("20.05.2027", DateError.TOO_LATE),
        ("31.02", DateError.FORMAT),
        ("завтра", DateError.FORMAT),
        ("12", DateError.FORMAT),
        ("1.2.3.4", DateError.FORMAT),
    ],
)
def test_parse_user_date(text: str, expected: date | DateError) -> None:
    assert parse_user_date(text, TODAY) == expected


def test_last_allowed_day_is_180_days_ahead() -> None:
    assert parse_user_date("19.05.2027", TODAY) == date(2027, 5, 19)
    assert parse_user_date("20.05.2027", TODAY) == DateError.TOO_LATE


def test_formatting() -> None:
    assert format_date(date(2026, 12, 20)) == "20 декабря"
    assert format_date(date(2027, 1, 5)) == "5 января"
    assert format_date_button(date(2026, 12, 20)) == "20.12, вс"


def test_local_today_uses_moscow_time() -> None:
    late_utc = datetime(2026, 12, 31, 21, 30, tzinfo=timezone.utc)
    assert local_today(late_utc) == date(2027, 1, 1)
