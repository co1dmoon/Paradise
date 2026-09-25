"""Seeding for the ad autopilot tests: people who came from a source and the games they organized."""

from __future__ import annotations

import itertools
from datetime import datetime
from zoneinfo import ZoneInfo

from app import repo
from app.core import users
from app.core.clock import FakeClock, to_iso
from app.core.models import JoinVia, ParticipantStatus
from app.db import Db

MSK = ZoneInfo("Europe/Moscow")
_codes = (f"P{n:05d}" for n in itertools.count(1))
_people = itertools.count(70_000)


def msk(day: int, hour: int = 10, minute: int = 0, month: int = 11) -> datetime:
    return datetime(2026, month, day, hour, minute, tzinfo=MSK)


def travel(clock: FakeClock, moment: datetime) -> None:
    clock.advance((moment - clock.now()).total_seconds())


async def person(db: Db, source: str, seen: datetime, *, consent: bool = True) -> int:
    user_id = next(_people)
    await users.ensure_user(db, user_id, source, seen)
    if consent:
        await users.give_consent(db, user_id, "Имя", None, "2026-10", seen)
    return user_id


async def game(db: Db, organizer: int, *, active: int, created: datetime, drawn: bool = False,
               source_game: int | None = None) -> int:
    game_id = await repo.insert_game(db, {
        "code": next(_codes), "title": "Игра", "organizer_id": organizer, "budget_text": "до 500 ₽",
        "status": "drawn" if drawn else "collecting", "tier": "free", "participant_limit": 10,
        "source": "direct", "created_at": to_iso(created), "drawn_at": to_iso(created) if drawn else None,
        "source_game_id": source_game,
    })
    for _ in range(active):
        member = await person(db, "direct", created)
        await repo.upsert_participant(db, game_id, member, "Гость", ParticipantStatus.ACTIVE, JoinVia.LINK, created)
    return game_id


async def games_from(db: Db, source: str, count: int, *, seen: datetime) -> None:
    """``count`` organizers who came from ``source`` at ``seen``, each with a game of 3 people."""
    for _ in range(count):
        organizer = await person(db, f"s:{source}", seen)
        await game(db, organizer, active=3, created=seen)
