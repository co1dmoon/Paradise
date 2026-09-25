"""§10 data retention and the end of a game's life, on explicit timestamps."""

from __future__ import annotations

import random
from datetime import date, datetime, timedelta, timezone

from app import repo
from app.core import games, retention, users
from app.core.clock import FakeClock, to_iso
from app.core.analytics import Event, record
from app.core.games import GameDraft
from app.core.models import GameStatus, JoinVia, PaymentProvider, PaymentStatus, RelayDirection, StateKind, Tier
from app.db import Database
from app.max_api import OutMessage, Target
from app.outbox import Outbox
from tools.fake_max import FakeMaxApi

NOW = datetime(2027, 7, 1, 1, 0, tzinfo=timezone.utc)  # 04:00 in Moscow
TODAY = date(2027, 7, 1)


def ago(days: float) -> datetime:
    return NOW - timedelta(days=days)


async def person(db: Database, user_id: int, *, days_ago: float, consent: bool = True) -> None:
    await users.ensure_user(db, user_id, "direct", ago(days_ago))
    if consent:
        await users.give_consent(db, user_id, f"Имя {user_id}", None, "2026-10", ago(days_ago))


async def new_game(db: Database, organizer: int, *, days_ago: float, exchange: date | None,
                   status: GameStatus = GameStatus.COLLECTING, members: tuple[int, ...] = ()) -> int:
    created = ago(days_ago)
    draft = GameDraft("Игра", "до 500 ₽", exchange, True)
    game = await games.create_game(db, organizer, draft, await repo.get_settings(db), now=created,
                                   today=created.date(), rng=random.Random(organizer * 1000 + int(days_ago)))
    for user_id in members:
        await games.join_game(db, game.id, user_id, JoinVia.LINK, created)
    if status != GameStatus.COLLECTING:
        await repo.update_game(db, game.id, status=status)
    return game.id


async def count(db: Database, sql: str, *params: object) -> int:
    return int(await db.fetchval(f"SELECT COUNT(*) FROM {sql}", params))


async def test_old_games_are_deleted_with_all_their_rows(db: Database) -> None:
    for user_id in (1, 2, 3, 4, 5):
        await person(db, user_id, days_ago=400)
    old = await new_game(db, 1, days_ago=250, exchange=date(2026, 12, 25), members=(2, 3, 5))
    await games.add_exclusion(db, old, 1, 2, 3)
    await games.run_draw(db, old, 1, now=ago(200), rng=random.Random(1))
    await repo.update_game(db, old, status=GameStatus.FINISHED)
    relay = await repo.insert_relay(db, old, 2, 3, RelayDirection.TO_RECEIVER, "Привет", ago(1))
    await repo.insert_report(db, relay.id, old, 3, 2, relay.text, ago(1))
    payment = await repo.insert_payment(db, old, 2, Tier.S, 490, PaymentStatus.PAID, PaymentProvider.ROBOKASSA, ago(1))
    await Outbox(db, FakeMaxApi(), FakeClock(NOW)).enqueue(Target.user(2), OutMessage("пара"), game_id=old)
    recent = await new_game(db, 4, days_ago=250, exchange=date(2027, 1, 10), status=GameStatus.FINISHED)
    undated = await new_game(db, 4, days_ago=181, exchange=None, status=GameStatus.CANCELLED)
    abandoned = await new_game(db, 4, days_ago=300, exchange=None)

    report = await retention.purge(db, NOW, TODAY)

    assert report.games == 2
    remaining = {row[0] for row in await db.fetchall("SELECT id FROM games")}
    assert remaining == {recent, abandoned} and undated not in remaining
    for table in ("participants", "exclusions", "assignments", "relay_messages", "reports", "outbox"):
        assert await count(db, f"{table} WHERE game_id = ?", old) == 0, table
    kept = await repo.get_payment(db, payment.inv_id)
    assert kept is not None and kept.game_id is None and kept.amount_rub == 490
    for user_id in (1, 2, 3, 5):
        assert await db.fetchval("SELECT last_game_at FROM users WHERE user_id = ?", (user_id,)) == (
            "2026-12-25T00:00:00.000+00:00")


async def test_relays_after_30_days_and_closed_reports(db: Database) -> None:
    for user_id in (1, 2, 3):
        await person(db, user_id, days_ago=40)
    game_id = await new_game(db, 1, days_ago=40, exchange=None, members=(2, 3))
    stale = await repo.insert_relay(db, game_id, 2, 3, RelayDirection.TO_RECEIVER, "старое", ago(31))
    fresh = await repo.insert_relay(db, game_id, 3, 2, RelayDirection.TO_SANTA, "новое", ago(29))
    closed = await repo.insert_report(db, stale.id, game_id, 3, 2, stale.text, ago(31))
    await repo.resolve_report(db, closed.id)
    still_open = await repo.insert_report(db, stale.id, game_id, 1, 2, stale.text, ago(31))

    report = await retention.purge(db, NOW, TODAY)

    assert (report.relays, report.reports) == (1, 1)
    assert await repo.get_relay(db, stale.id) is None and await repo.get_relay(db, fresh.id) is not None
    assert await repo.get_report(db, closed.id) is None
    waiting = await repo.get_report(db, still_open.id)
    assert waiting is not None and waiting.relay_id is None


async def test_unconsented_after_7_days_and_users_without_games_for_a_year(db: Database) -> None:
    await person(db, 10, days_ago=8, consent=False)
    await person(db, 11, days_ago=6, consent=False)
    await person(db, 12, days_ago=366)  # never played
    await person(db, 13, days_ago=366)  # played 100 days ago, the game already purged
    await db.execute("UPDATE users SET last_game_at = ? WHERE user_id = 13", (to_iso(ago(100)),))
    await person(db, 14, days_ago=366)  # organizes a live game
    live = await new_game(db, 14, days_ago=10, exchange=None)
    await person(db, 15, days_ago=366)  # takes part in it
    await games.join_game(db, live, 15, JoinVia.LINK, ago(10))
    await person(db, 16, days_ago=2)
    await record(db, Event.JOIN, ago(3), user_id=12)
    payment = await repo.insert_payment(db, live, 12, Tier.S, 490, PaymentStatus.PAID, PaymentProvider.ROBOKASSA, ago(5))

    report = await retention.purge(db, NOW, TODAY)

    left = {row[0] for row in await db.fetchall("SELECT user_id FROM users")}
    assert left == {11, 13, 14, 15, 16} and report.users == 2
    assert await count(db, "events WHERE user_id IN (10, 12)") == 0
    assert await count(db, "events WHERE user_id IS NULL AND type = 'join'") == 1
    kept = await repo.get_payment(db, payment.inv_id)
    assert kept is not None and kept.payer_id is None and kept.amount_rub == 490


async def test_payments_and_events_lose_user_ids_after_180_days(db: Database) -> None:
    await person(db, 1, days_ago=200)
    game_id = await new_game(db, 1, days_ago=10, exchange=None)
    old = await repo.insert_payment(db, game_id, 1, Tier.S, 490, PaymentStatus.PAID, PaymentProvider.ROBOKASSA, ago(181))
    new = await repo.insert_payment(db, game_id, 1, Tier.M, 500, PaymentStatus.PAID, PaymentProvider.ROBOKASSA, ago(179))
    await record(db, Event.PAY_SUCCESS, ago(181), user_id=1, game_id=game_id, amount=490)
    await record(db, Event.PAY_SUCCESS, ago(179), user_id=1, game_id=game_id, amount=500)
    await repo.set_state(db, 1, StateKind.WISHES, ago(1), game_id=game_id)

    report = await retention.purge(db, NOW, TODAY)

    assert (await repo.get_payment(db, old.inv_id)).payer_id is None  # type: ignore[union-attr]
    assert (await repo.get_payment(db, new.inv_id)).payer_id == 1  # type: ignore[union-attr]
    rows = await db.fetchall("SELECT user_id FROM events WHERE type = 'pay_success' ORDER BY ts")
    assert [row[0] for row in rows] == [None, 1]
    assert report.expired_inputs == 1 and await count(db, "user_state") == 0
    assert await count(db, "users") == 1 and await count(db, "games") == 1


async def test_end_games(db: Database) -> None:
    await person(db, 1, days_ago=400)
    finished = await new_game(db, 1, days_ago=20, exchange=TODAY - timedelta(days=3), status=GameStatus.DRAWN)
    still_drawn = await new_game(db, 1, days_ago=20, exchange=TODAY - timedelta(days=2), status=GameStatus.DRAWN)
    abandoned = await new_game(db, 1, days_ago=60, exchange=TODAY - timedelta(days=30))
    late = await new_game(db, 1, days_ago=60, exchange=TODAY - timedelta(days=29))
    undated_drawn = await new_game(db, 1, days_ago=180, exchange=None, status=GameStatus.DRAWN)
    undated_open = await new_game(db, 1, days_ago=181, exchange=None)
    young = await new_game(db, 1, days_ago=100, exchange=None)

    ended = await retention.end_games(db, NOW, TODAY)

    assert (ended.finished, ended.cancelled) == (2, 2)
    statuses = {row[0]: row[1] for row in await db.fetchall("SELECT id, status FROM games")}
    assert statuses == {
        finished: "finished", still_drawn: "drawn", abandoned: "cancelled", late: "collecting",
        undated_drawn: "finished", undated_open: "cancelled", young: "collecting",
    }
    game = await repo.get_game(db, finished)
    assert game is not None and game.finished_at == "2027-07-01T01:00:00.000+00:00"

