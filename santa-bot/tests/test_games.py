from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from app import repo
from app.core import games
from app.core.clock import FakeClock
from app.core.games import GameDraft, JoinOutcome
from app.core.models import GameStatus, JoinVia, ParticipantStatus, Settings
from app.db import Database

TODAY = date(2026, 11, 20)
ORGANIZER = 100


@pytest.fixture
async def settings(db: Database) -> Settings:
    return await repo.get_settings(db)


@pytest.fixture
async def new_game(db: Database, clock: FakeClock, rng: random.Random, settings: Settings, make_user):
    async def create(organizer: int = ORGANIZER, *, participates: bool = True, source_game_id: int | None = None):
        if await repo.get_user(db, organizer) is None:
            await make_user(organizer, "Организатор Оля")
        draft = GameDraft("Отдел продаж", "до 1000 ₽", date(2026, 12, 25), participates, source_game_id)
        return await games.create_game(db, organizer, draft, settings, now=clock.now(), today=TODAY, rng=rng)

    return create


async def join_many(db: Database, clock: FakeClock, make_user, game_id: int, ids: range) -> None:
    for user_id in ids:
        await make_user(user_id, f"Участник {user_id}")
        clock.advance(1)
        await games.join_game(db, game_id, user_id, JoinVia.LINK, clock.now())


async def test_create_game_adds_participating_organizer(db: Database, new_game) -> None:
    game = await new_game()
    assert game.status == GameStatus.COLLECTING and game.participant_limit == 10 and len(game.code) == 6
    organizer = await repo.get_participant(db, game.id, ORGANIZER)
    assert organizer is not None and organizer.status == ParticipantStatus.ACTIVE
    assert organizer.via == JoinVia.ORGANIZER and organizer.display_name == "Организатор Оля"
    assert game.source == "direct"


async def test_create_game_only_organizing(db: Database, new_game) -> None:
    game = await new_game(participates=False)
    assert await repo.get_participant(db, game.id, ORGANIZER) is None


async def test_game_sources(db: Database, new_game, make_user, clock: FakeClock) -> None:
    first = await new_game()
    ref = await new_game(200, source_game_id=first.id)
    assert ref.source == "ref"
    await make_user(300, "Пётр", source="j:" + first.code)
    assert (await new_game(300)).source == "participant"
    await make_user(400, "Анна", source="s:yd")
    assert (await new_game(400)).source == "s:yd"
    await make_user(500, "Код", source="direct")
    await games.join_game(db, first.id, 500, JoinVia.CODE, clock.now())
    clock.advance(1)
    assert (await new_game(500)).source == "participant"


async def test_daily_game_limit(db: Database, new_game) -> None:
    for _ in range(games.DAILY_GAME_LIMIT):
        await new_game()
    with pytest.raises(games.DailyLimitReached):
        await new_game()


async def test_blocked_or_unconsented_user_cannot_create(db: Database, new_game, clock: FakeClock, settings) -> None:
    await new_game()
    await repo.set_blocked(db, ORGANIZER, True)
    with pytest.raises(games.UserBlocked):
        await new_game()
    await repo.insert_user(db, 555, "direct", clock.now())
    draft = GameDraft("", "до 500 ₽", None, True)
    with pytest.raises(games.PermissionDenied):
        await games.create_game(db, 555, draft, settings, now=clock.now(), today=TODAY, rng=random.Random(1))


async def test_join_fills_up_then_waits(db: Database, new_game, make_user, clock: FakeClock) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 10))
    counts = await games.game_counts(db, game.id)
    assert (counts.active, counts.waiting) == (10, 0)
    await make_user(11, "Одиннадцатый")
    result = await games.join_game(db, game.id, 11, JoinVia.LINK, clock.now())
    assert result.outcome == JoinOutcome.WAITING
    assert (await games.join_game(db, game.id, 11, JoinVia.LINK, clock.now())).outcome == JoinOutcome.ALREADY_IN


async def test_join_refusals(db: Database, new_game, make_user, clock: FakeClock, rng: random.Random) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 4))
    await games.remove_participant(db, game.id, ORGANIZER, 3, clock.now())
    assert (await games.join_game(db, game.id, 3, JoinVia.LINK, clock.now())).outcome == JoinOutcome.REMOVED
    await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng)
    await make_user(50, "Опоздавший")
    assert (await games.join_game(db, game.id, 50, JoinVia.LINK, clock.now())).outcome == JoinOutcome.DRAWN
    other = await new_game()
    await games.cancel_game(db, other.id, ORGANIZER, clock.now())
    assert (await games.join_game(db, other.id, 50, JoinVia.LINK, clock.now())).outcome == JoinOutcome.CANCELLED


async def test_leaving_frees_a_place_for_the_first_waiting(db: Database, new_game, make_user, clock) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 12))  # 10 active + waiting 10, 11
    departure = await games.leave_game(db, game.id, 1, clock.now())
    assert [p.user_id for p in departure.activated] == [10]
    left = await repo.get_participant(db, game.id, 1)
    assert left is not None and left.status == ParticipantStatus.LEFT
    rejoined = await games.join_game(db, game.id, 1, JoinVia.LINK, clock.now())
    assert rejoined.outcome == JoinOutcome.WAITING


async def test_only_the_organizer_can_remove(db: Database, new_game, make_user, clock) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 4))
    with pytest.raises(games.PermissionDenied):
        await games.remove_participant(db, game.id, 1, 2, clock.now())
    with pytest.raises(games.PermissionDenied):
        await games.remove_participant(db, game.id, ORGANIZER, ORGANIZER, clock.now())
    departure = await games.remove_participant(db, game.id, ORGANIZER, 2, clock.now())
    assert departure.participant.user_id == 2


async def test_names_and_wishes(db: Database, new_game, make_user, clock) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 2))
    await games.set_display_name(db, game.id, 1, "Маша, 5Б")
    await games.set_wishes(db, game.id, 1, "Книга\nНастольная игра", clock.now())
    participant = await repo.get_participant(db, game.id, 1)
    assert participant is not None and participant.display_name == "Маша, 5Б"
    assert participant.wishes == "Книга\nНастольная игра"
    with pytest.raises(games.PermissionDenied):
        await games.set_wishes(db, game.id, 77, "чужие", clock.now())
    with pytest.raises(ValueError):
        await games.set_wishes(db, game.id, 1, "x" * 1001, clock.now())


async def test_exclusions(db: Database, new_game, make_user, clock) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 4))
    assert await games.add_exclusion(db, game.id, ORGANIZER, 2, 1)
    assert not await games.add_exclusion(db, game.id, ORGANIZER, 1, 2)
    assert await repo.exclusions(db, game.id) == [(1, 2)]
    with pytest.raises(games.ExclusionRejected) as same:
        await games.add_exclusion(db, game.id, ORGANIZER, 1, 1)
    assert same.value.reason == games.ExclusionProblem.SAME_PERSON
    with pytest.raises(games.ExclusionRejected):
        await games.add_exclusion(db, game.id, ORGANIZER, 1, 999)
    with pytest.raises(games.PermissionDenied):
        await games.add_exclusion(db, game.id, 1, 2, 3)
    assert await games.remove_exclusion(db, game.id, ORGANIZER, 1, 2)
    assert (await games.game_counts(db, game.id)).exclusions == 0


async def test_exclusion_limit(db: Database, new_game, make_user, clock) -> None:
    game = await new_game()
    await repo.update_game(db, game.id, participant_limit=30)
    await join_many(db, clock, make_user, game.id, range(1, 12))
    pairs = [(a, b) for a in range(1, 12) for b in range(a + 1, 12)][: games.MAX_EXCLUSIONS]
    for a, b in pairs:
        await games.add_exclusion(db, game.id, ORGANIZER, a, b)
    with pytest.raises(games.ExclusionRejected) as limit:
        await games.add_exclusion(db, game.id, ORGANIZER, 10, ORGANIZER)
    assert limit.value.reason == games.ExclusionProblem.LIMIT


async def test_draw_requires_organizer_and_three(db: Database, new_game, make_user, clock, rng) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 2))
    with pytest.raises(games.NotEnoughParticipants):
        await games.draw_preview(db, game.id, ORGANIZER)
    await join_many(db, clock, make_user, game.id, range(2, 3))
    with pytest.raises(games.PermissionDenied):
        await games.run_draw(db, game.id, 1, now=clock.now(), rng=rng)
    preview = await games.draw_preview(db, game.id, ORGANIZER)
    assert (preview.active, preview.without_wishes, preview.waiting) == (3, 3, 0)


async def test_draw_writes_one_cycle_respecting_exclusions(db: Database, new_game, make_user, clock, rng) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 8))
    await games.add_exclusion(db, game.id, ORGANIZER, 1, 2)
    result = await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng)
    assert result.game.status == GameStatus.DRAWN and result.game.drawn_at is not None
    stored = await repo.assignments(db, game.id)
    assert stored == result.pairs and len(stored) == 8
    assert stored.get(1) != 2 and stored.get(2) != 1
    receiver = await games.my_receiver(db, game.id, 1)
    assert receiver is not None and receiver.user_id == stored[1]
    with pytest.raises(games.WrongStatus):
        await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng)


async def test_impossible_draw_keeps_collecting(db: Database, new_game, make_user, clock, rng) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 3))
    await games.add_exclusion(db, game.id, ORGANIZER, 1, 2)
    with pytest.raises(games.DrawImpossible):
        await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng, time_budget=0.1)
    assert (await repo.get_game(db, game.id)).status == GameStatus.COLLECTING


async def test_redraw_at_most_twice(db: Database, new_game, make_user, clock, rng) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 5))
    await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng)
    for expected in (1, 2):
        result = await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng, redraw=True)
        assert result.game.redraw_count == expected
    with pytest.raises(games.RedrawLimitReached):
        await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng, redraw=True)


async def test_leaving_after_draw_splices_the_cycle(db: Database, new_game, make_user, clock, rng) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 6))
    pairs = (await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng)).pairs
    leaving = 3
    giver = next(g for g, r in pairs.items() if r == leaving)
    departure = await games.leave_game(db, game.id, leaving, clock.now())
    assert departure.splice is not None
    assert (departure.splice.giver_id, departure.splice.receiver_id) == (giver, pairs[leaving])
    assert not departure.splice.needs_redraw
    stored = await repo.assignments(db, game.id)
    assert leaving not in stored and leaving not in stored.values() and stored[giver] == pairs[leaving]


async def test_splice_down_to_two_asks_for_redraw(db: Database, new_game, make_user, clock, rng) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 3))
    await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng)
    departure = await games.remove_participant(db, game.id, ORGANIZER, 1, clock.now())
    assert departure.splice is not None and departure.splice.too_few


async def test_result_delivery_summary_fires_once(db: Database, new_game, make_user, clock, rng) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 3))
    await games.run_draw(db, game.id, ORGANIZER, now=clock.now(), rng=rng)
    assert await games.record_result_delivery(db, game.id, ORGANIZER, True, clock.now()) is None
    assert await games.record_result_delivery(db, game.id, 1, False, clock.now()) is None
    summary = await games.record_result_delivery(db, game.id, 2, True, clock.now())
    assert summary is not None and (summary.sent, summary.total, summary.failed_names) == (2, 3, ["Участник 1"])
    assert await games.record_result_delivery(db, game.id, 2, True, clock.now()) is None
    assert [p.user_id for p in await games.undelivered_results(db, game.id, ORGANIZER)] == [1]


async def test_wish_reminder_once_per_day(db: Database, new_game, make_user, clock) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 3))
    await games.set_wishes(db, game.id, 1, "Шарф", clock.now())
    recipients = await games.start_wish_reminder(db, game.id, ORGANIZER, clock.now())
    assert sorted(p.user_id for p in recipients) == [2, ORGANIZER]
    with pytest.raises(games.TooSoon):
        await games.start_wish_reminder(db, game.id, ORGANIZER, clock.now() + timedelta(hours=23))
    assert await games.start_wish_reminder(db, game.id, ORGANIZER, clock.now() + timedelta(hours=25))


async def test_settings_and_cancel(db: Database, new_game, make_user, clock) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 3))
    updated = await games.update_game_settings(db, game.id, ORGANIZER, title="Семья", anon_chat=False,
                                               exchange_date=date(2026, 12, 27))
    assert (updated.title, updated.anon_chat, updated.exchange_date) == ("Семья", False, date(2026, 12, 27))
    with pytest.raises(games.PermissionDenied):
        await games.update_game_settings(db, game.id, 1, title="Чужая")
    with pytest.raises(ValueError):
        await games.update_game_settings(db, game.id, ORGANIZER, status="drawn")
    notified = await games.cancel_game(db, game.id, ORGANIZER, clock.now())
    assert sorted(p.user_id for p in notified) == [1, 2]
    assert (await repo.get_game(db, game.id)).status == GameStatus.CANCELLED


async def test_organizer_participation_toggle(db: Database, new_game, make_user, clock) -> None:
    game = await new_game()
    await join_many(db, clock, make_user, game.id, range(1, 11))  # 10 active (with organizer) + 1 waiting
    activated = await games.set_organizer_participation(db, game.id, ORGANIZER, False, clock.now())
    assert [p.user_id for p in activated] == [10]
    await games.set_organizer_participation(db, game.id, ORGANIZER, True, clock.now())
    organizer = await repo.get_participant(db, game.id, ORGANIZER)
    assert organizer is not None and organizer.status == ParticipantStatus.WAITING


async def test_games_of_user(db: Database, new_game, make_user, clock) -> None:
    mine = await new_game()
    other = await new_game(200)
    await make_user(1, "Иван")
    await games.join_game(db, other.id, 1, JoinVia.LINK, clock.now())
    listed = await repo.games_of_user(db, ORGANIZER)
    assert [g.id for g, _ in listed] == [mine.id]
    as_participant = await repo.games_of_user(db, 1)
    assert [(g.id, p.user_id if p else None) for g, p in as_participant] == [(other.id, 1)]
