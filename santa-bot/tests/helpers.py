"""Test helpers shared by several modules."""

from __future__ import annotations

from app.core import users
from app.core.clock import FakeClock
from app.core.models import User
from app.db import Db


async def consented_user(db: Db, clock: FakeClock, user_id: int, name: str, source: str = "direct") -> User:
    """A user who has passed the consent gate."""
    await users.ensure_user(db, user_id, source, clock.now())
    return await users.give_consent(db, user_id, name, None, "2026-10", clock.now())
