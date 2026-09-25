"""PROMO_SPEC §2: per-source metrics on a seeded database, the 3+ definition and the cohort cutoff."""

from __future__ import annotations

import itertools
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from app import repo
from app.core import retention, users
from app.core.clock import to_iso
from app.core.models import JoinVia, ParticipantStatus, PaymentProvider, PaymentStatus, Tier
from app.db import Database
from app.promo.attribution import SourceMetrics, cohort_cutoff, source_metrics, total

MSK = ZoneInfo("Europe/Moscow")
_codes = (f"G{n:05d}" for n in itertools.count(1))
_people = itertools.count(5000)


def at(day: int, month: int = 11) -> datetime:
    return datetime(2026, month, day, 12, tzinfo=timezone.utc)


async def person(db: Database, source: str, seen: datetime, *, consent: bool = True) -> int:
    user_id = next(_people)
    await users.ensure_user(db, user_id, source, seen)
    if consent:
        await users.give_consent(db, user_id, "Имя", None, "2026-10", seen)
    return user_id


async def game(db: Database, organizer: int, *, active: int, drawn: bool = False, left: int = 0,
               source_game: int | None = None) -> int:
    created = at(25)
    game_id = await repo.insert_game(db, {
        "code": next(_codes), "title": "Игра", "organizer_id": organizer, "budget_text": "до 500 ₽",
        "status": "drawn" if drawn else "collecting", "tier": "free", "participant_limit": 10,
        "source": "participant", "created_at": to_iso(created), "source_game_id": source_game,
        "drawn_at": to_iso(created) if drawn else None,
    })
    for status, count in ((ParticipantStatus.ACTIVE, active), (ParticipantStatus.LEFT, left)):
        for _ in range(count):
            member = await person(db, "direct", created)
            await repo.upsert_participant(db, game_id, member, "Гость", status, JoinVia.LINK, created)
    return game_id


async def test_metrics_per_source(db: Database) -> None:
    cutoff = cohort_cutoff(date(2026, 11, 30), 2, MSK)
    assert cutoff == datetime(2026, 11, 28, tzinfo=MSK)
    anna = await person(db, "s:yd701", at(20))
    await person(db, "s:yd701", at(21))  # came, never organized
    late = await person(db, "s:yd701", at(29))  # first seen after the cutoff
    unconsented = await person(db, "s:yd701", at(20), consent=False)
    drawn = await game(db, anna, active=2, left=2, drawn=True)  # 4 at the draw, 2 left since
    await game(db, anna, active=3)
    await game(db, anna, active=2)
    await game(db, late, active=4)
    for status, amount in ((PaymentStatus.PAID, 490), (PaymentStatus.GRANTED, 990), (PaymentStatus.REFUNDED, 2490)):
        await repo.insert_payment(db, drawn, anna, Tier.S, amount, status, PaymentProvider.ROBOKASSA, at(26))
    outsider = await person(db, "direct", at(20))
    await game(db, outsider, active=3, source_game=drawn)  # created from an ad-sourced game
    vk_user = await person(db, "s:vk55", at(22))
    await game(db, vk_user, active=1)

    # retention deletes the unconsented person after 7 days; the source still counts them
    await retention.purge(db, at(30), date(2026, 11, 30))
    assert await repo.get_user(db, unconsented) is None

    metrics = await source_metrics(db, ["yd701", "vk55", "p404"], cutoff)
    assert metrics["yd701"] == SourceMetrics(users=4, organizers=2, games=4, games3=3, games3_matured=2,
                                             paid_rub=1480, downstream_games=1)
    assert metrics["vk55"] == SourceMetrics(users=1, organizers=1, games=1)
    assert metrics["p404"] == SourceMetrics()
    assert total(metrics.values()) == SourceMetrics(users=5, organizers=3, games=5, games3=3, games3_matured=2,
                                                    paid_rub=1480, downstream_games=1)


async def test_no_sources_no_queries(db: Database) -> None:
    assert await source_metrics(db, [], datetime(2026, 11, 28, tzinfo=MSK)) == {}
