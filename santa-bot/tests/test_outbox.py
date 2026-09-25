from __future__ import annotations

import random
from collections import defaultdict
from datetime import date
from pathlib import Path

import pytest

from app import repo
from app.context import AppContext
from app.core import games
from app.core.clock import FakeClock, to_iso
from app.core.games import GameDraft
from app.core.models import JoinVia
from app.db import Database
from app.max_api import BadRequest, OutMessage, RateLimited, Target, Transient, Unauthorized
from app.outbox import PURPOSE_DRAW_RESULT, RETRY_DELAYS, Outbox, RateLimiter
from tests.helpers import consented_user
from tools.fake_max import FakeMaxApi


def assert_rate_limits(api: FakeMaxApi, kinds: tuple[str, ...] = ("send", "edit")) -> None:
    calls = [call for call in api.calls if call.kind in kinds]
    times = [call.at for call in calls]
    for i in range(len(times)):
        window = [t for t in times[i:] if t < times[i] + 1.0 - 1e-6]
        assert len(window) <= 25, f"{len(window)} calls within one second from t={times[i]:.3f}"
    by_target: dict[str, list[float]] = defaultdict(list)
    for call in calls:
        by_target[call.target_key].append(call.at)
    for target, stamps in by_target.items():
        gaps = [b - a for a, b in zip(stamps, stamps[1:])]
        assert all(gap >= 1.0 - 1e-9 for gap in gaps), f"{target} got two messages within a second"


async def test_limiter_spacing_with_fake_clock() -> None:
    clock = FakeClock()
    limiter = RateLimiter(clock)
    stamps = []
    for i in range(50):
        await limiter.acquire(f"user:{i}")
        stamps.append(clock.monotonic())
    assert stamps[-1] == pytest.approx(49 / 25)
    await limiter.acquire("user:0")
    assert limiter.key_wait("user:0") == pytest.approx(1.0)


async def test_300_queued_sends_respect_global_and_per_target_limits(ctx: AppContext, api: FakeMaxApi) -> None:
    for i in range(300):
        await ctx.outbox.enqueue(Target.user(1000 + i % 40), OutMessage(f"Сообщение {i}"))
    await ctx.outbox.drain()
    assert len(api.sent) == 300
    assert_rate_limits(api)
    first_target = [m.text for m in api.messages_to(1000)]
    assert first_target == [f"Сообщение {i}" for i in range(0, 300, 40)], "per-target order is kept"
    duration = api.calls[-1].at - api.calls[0].at
    assert duration >= 299 / 25 - 1e-6


async def test_direct_sends_share_the_limiter(ctx: AppContext, api: FakeMaxApi) -> None:
    for i in range(5):
        await ctx.outbox.send_now(Target.user(7), OutMessage(f"ответ {i}"))
    await ctx.outbox.enqueue(Target.user(7), OutMessage("из очереди"))
    await ctx.outbox.drain()
    assert_rate_limits(api)
    assert api.texts_to(7)[-1] == "из очереди"


async def test_forbidden_sets_dm_ok_and_kills_the_message(ctx: AppContext, api: FakeMaxApi, clock: FakeClock) -> None:
    await repo.insert_user(ctx.db, 42, "direct", clock.now())
    api.block_user(42)
    await ctx.outbox.enqueue(Target.user(42), OutMessage("привет"))
    await ctx.outbox.drain()
    user = await repo.get_user(ctx.db, 42)
    assert user is not None and not user.dm_ok
    assert await ctx.db.fetchval("SELECT status FROM outbox") == "dead"
    api.unblock_user(42)
    assert await ctx.outbox.send_now(Target.user(42), OutMessage("снова")) is not None


async def test_transient_errors_retry_with_backoff_then_die(ctx: AppContext, api: FakeMaxApi, clock) -> None:
    api.fail_next(Transient(503, "unavailable"), times=len(RETRY_DELAYS) + 1)
    start = clock.monotonic()
    await ctx.outbox.enqueue(Target.user(5), OutMessage("важное"))
    await ctx.outbox.drain()
    row = await ctx.db.fetchone("SELECT status, attempts, last_error FROM outbox")
    assert (row["status"], row["attempts"]) == ("dead", len(RETRY_DELAYS) + 1)
    assert clock.monotonic() - start >= sum(RETRY_DELAYS)


async def test_rate_limited_message_is_delivered_after_retry(ctx: AppContext, api: FakeMaxApi) -> None:
    api.fail_next(RateLimited(429, "too many"), times=2)
    await ctx.outbox.enqueue(Target.user(5), OutMessage("дойдёт"))
    await ctx.outbox.drain()
    assert api.texts_to(5) == ["дойдёт"]
    assert await ctx.db.fetchval("SELECT attempts FROM outbox") == 3


async def test_bad_request_is_dead_and_alerts_admin(ctx: AppContext, api: FakeMaxApi) -> None:
    api.fail_next(BadRequest(400, "text too long"))
    await ctx.outbox.enqueue(Target.user(5), OutMessage("плохое"))
    await ctx.outbox.drain()
    assert await ctx.db.fetchval("SELECT status FROM outbox WHERE target_id = 5") == "dead"
    assert "отклонил" in api.last_text(9000)


async def test_dedupe_key(ctx: AppContext) -> None:
    first = await ctx.outbox.enqueue(Target.user(1), OutMessage("a"), dedupe_key="once")
    second = await ctx.outbox.enqueue(Target.user(1), OutMessage("a"), dedupe_key="once")
    assert first is not None and second is None
    assert await ctx.outbox.pending_count() == 1


async def test_pending_messages_survive_a_restart(tmp_path: Path, clock: FakeClock) -> None:
    path = tmp_path / "restart.db"
    db = await Database.open(path)
    await db.migrate()
    first_api = FakeMaxApi(clock=clock)
    await Outbox(db, first_api, clock).enqueue(Target.user(3), OutMessage("после перезапуска"))
    await db.close()

    reopened = await Database.open(path)
    second_api = FakeMaxApi(clock=clock)
    await Outbox(reopened, second_api, clock).drain()
    await reopened.close()
    assert first_api.sent == [] and second_api.texts_to(3) == ["после перезапуска"]


async def test_direct_send_failure_is_queued_for_retry(ctx: AppContext, api: FakeMaxApi) -> None:
    api.fail_next(Transient(0, "timeout"))
    assert await ctx.outbox.send_now(Target.user(8), OutMessage("ответ")) is None
    await ctx.outbox.drain()
    assert api.texts_to(8) == ["ответ"]


async def test_answer_falls_back_to_a_message(ctx: AppContext, api: FakeMaxApi) -> None:
    api.reject_notifications = True
    await ctx.outbox.answer(Target.user(8), "cb.1", notification="Готово")
    assert api.answers[-1].callback_id == "cb.1"
    assert api.texts_to(8) == ["Готово"]


async def test_edit_is_queued_and_applied(ctx: AppContext, api: FakeMaxApi) -> None:
    mid = await ctx.outbox.send_now(Target.chat(-5), OutMessage("карточка"))
    assert mid is not None
    await ctx.outbox.enqueue_edit(Target.chat(-5), mid, OutMessage("карточка v2"))
    await ctx.outbox.drain()
    assert api.messages_in_chat(-5)[0].text == "карточка v2"


async def test_draw_results_are_tracked_and_summarized(ctx: AppContext, api: FakeMaxApi, clock) -> None:
    rng = random.Random(3)
    for user_id, name in ((100, "Ольга"), (1, "Иван"), (2, "Мария"), (3, "Пётр")):
        await consented_user(ctx.db, clock, user_id, name)
    settings = await repo.get_settings(ctx.db)
    draft = GameDraft("Семья", "до 1000 ₽", None, True)
    game = await games.create_game(ctx.db, 100, draft, settings, now=clock.now(), today=date(2026, 11, 20), rng=rng)
    for user_id in (1, 2, 3):
        await games.join_game(ctx.db, game.id, user_id, JoinVia.LINK, clock.now())
    result = await games.run_draw(ctx.db, game.id, 100, now=clock.now(), rng=rng)
    api.block_user(2)
    for giver in result.pairs:
        await ctx.outbox.enqueue(Target.user(giver), OutMessage("пара"), purpose=PURPOSE_DRAW_RESULT, game_id=game.id)
    await ctx.outbox.drain()
    assert api.last_text(100).startswith("Готово! Пары отправлены 3 из 4.")
    assert "Мария" in api.last_text(100)
    participant = await repo.get_participant(ctx.db, game.id, 2)
    assert participant is not None and participant.result_dm_ok is False


async def test_a_rejected_token_never_kills_queued_messages(ctx: AppContext, api: FakeMaxApi, clock) -> None:
    """Fixing MAX_BOT_TOKEN takes the owner longer than the retry schedule; nothing may be lost meanwhile."""
    await ctx.outbox.enqueue(Target.user(5), OutMessage("Ваша пара"))
    api.fail_next(Unauthorized(401, "invalid token"), times=30)
    start = clock.monotonic()
    await ctx.outbox.drain()
    assert clock.monotonic() - start > 3 * sum(RETRY_DELAYS), "the rows outlived the retry schedule"
    assert await ctx.db.fetchval("SELECT COUNT(*) FROM outbox WHERE status = 'dead'") == 0
    assert "Ваша пара" in api.texts_to(5)


async def test_messages_waiting_for_the_token_go_out_once_it_works(
    ctx: AppContext, api: FakeMaxApi, clock: FakeClock
) -> None:
    await ctx.outbox.enqueue(Target.user(5), OutMessage("Ваша пара"))
    api.fail_next(Unauthorized(401, "invalid token"))
    await ctx.outbox.run_once()
    row = await ctx.db.fetchone("SELECT status, attempts, not_before FROM outbox WHERE target_id = 5")
    assert (row["status"], row["attempts"]) == ("pending", 0) and row["not_before"] > to_iso(clock.now())
    assert await ctx.outbox.retry_unauthorized_now() == 1
    clock.advance(1)  # the per-dialog limit, not the retry delay
    await ctx.outbox.run_once()
    assert api.texts_to(5) == ["Ваша пара"]
