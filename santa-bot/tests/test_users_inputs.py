from __future__ import annotations

import pytest

from app import repo
from app.core import users
from app.core.clock import FakeClock
from app.core.inputs import MAX_NAME, clean_limited, clean_text, shorten
from app.db import Database


async def test_before_consent_only_id_time_and_source_are_stored(db: Database, clock: FakeClock) -> None:
    user = await users.ensure_user(db, 5, "j:ABC234", clock.now())
    assert (user.user_id, user.first_source, user.max_name, user.username, user.consent_at) == (
        5, "j:ABC234", None, None, None)
    again = await users.ensure_user(db, 5, "s:yd", clock.now())
    assert again.first_source == "j:ABC234", "first_source is set once"
    events = await db.fetchall("SELECT type FROM events")
    assert [row[0] for row in events] == ["user_first_seen"]


async def test_consent_stores_profile_and_version(db: Database, clock: FakeClock) -> None:
    await users.ensure_user(db, 5, "direct", clock.now())
    user = await users.give_consent(db, 5, "  Ольга​ Петрова ", "olga", "2026-10", clock.now())
    assert user.has_consent and user.consent_version == "2026-10" and user.max_name == "Ольга Петрова"
    await users.refresh_profile(db, user, "Ольга П.", "olga")
    refreshed = await repo.get_user(db, 5)
    assert refreshed is not None and refreshed.max_name == "Ольга П."


def test_display_name_fallback_and_length() -> None:
    assert users.display_name_from_profile(None) == users.FALLBACK_NAME
    assert len(users.display_name_from_profile("Я" * 100)) == MAX_NAME


@pytest.mark.parametrize(
    ("raw", "multiline", "expected"),
    [
        ("  Привет,\tмир  ", False, "Привет, мир"),
        ("строка 1\r\nстрока 2", False, "строка 1 строка 2"),
        ("строка 1\r\n\n\n\nстрока 2", True, "строка 1\n\nстрока 2"),
        ("a\x00b\x1b[31mc‮d", False, "ab[31mcd"),
        ("👨‍👩‍👧 семья", False, "👨‍👩‍👧 семья"),
    ],
)
def test_clean_text(raw: str, multiline: bool, expected: str) -> None:
    assert clean_text(raw, multiline=multiline) == expected


def test_clean_limited_and_shorten() -> None:
    assert clean_limited("  ok ", 2) == "ok"
    assert clean_limited("   ", 10) is None
    assert clean_limited("x" * 61, 60) is None
    assert shorten("abcdef", 4) == "abc…" and shorten("abc", 4) == "abc"
