"""User lifecycle: first touch and the consent gate (§5.1)."""

from __future__ import annotations

from datetime import datetime

from app import repo
from app.core.analytics import Event, record
from app.core.inputs import MAX_NAME, clean_text, shorten
from app.core.models import User
from app.db import Db

FALLBACK_NAME = "Участник"


def display_name_from_profile(name: str | None) -> str:
    """A MAX profile name cleaned for display, at most 40 characters."""
    return shorten(clean_text(name or ""), MAX_NAME) or FALLBACK_NAME


async def ensure_user(db: Db, user_id: int, first_source: str, now: datetime) -> User:
    """Return the user, creating the minimal pre-consent row on first touch.

    Before consent only user_id, first_seen_at and first_source are stored.
    """
    async with db.transaction() as tx:
        if await repo.insert_user(tx, user_id, first_source, now):
            await record(tx, Event.USER_FIRST_SEEN, now, user_id=user_id, source=first_source)
        user = await repo.get_user(tx, user_id)
    assert user is not None
    return user


async def give_consent(
    db: Db, user_id: int, profile_name: str | None, username: str | None, version: str, now: datetime
) -> User:
    async with db.transaction() as tx:
        await repo.set_consent(tx, user_id, display_name_from_profile(profile_name), username, version, now)
        await record(tx, Event.CONSENT, now, user_id=user_id, version=version)
        user = await repo.get_user(tx, user_id)
    assert user is not None
    return user


async def refresh_profile(db: Db, user: User, profile_name: str | None, username: str | None) -> None:
    """Keep max_name current for consented users; a no-op when nothing changed."""
    name = display_name_from_profile(profile_name)
    if user.has_consent and (name != user.max_name or username != user.username):
        await repo.update_profile(db, user.user_id, name, username)
