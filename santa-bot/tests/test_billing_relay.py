from __future__ import annotations

import random
from datetime import date, timedelta

import pytest

from app import repo
from app.core import billing, games, relay
from app.core.clock import FakeClock
from app.core.games import GameDraft
from app.core.models import JoinVia, ParticipantStatus, PaymentStatus, RelayDirection, Tier
from app.core.pricing import PriceList
from app.db import Database

ORGANIZER = 100


@pytest.fixture
async def prices(db: Database) -> PriceList:
    return PriceList.from_settings(await repo.get_settings(db))


@pytest.fixture
async def full_game(db: Database, clock: FakeClock, rng: random.Random, make_user):
    """A free game with 10 active people (organizer + 1..9) and 11, 12 waiting, in that order."""
    await make_user(ORGANIZER, "Ольга")
    settings = await repo.get_settings(db)
    draft = GameDraft("Офис", "до 1000 ₽", date(2026, 12, 25), True)
    game = await games.create_game(db, ORGANIZER, draft, settings, now=clock.now(), today=date(2026, 11, 20), rng=rng)
    for user_id in range(1, 13):
        await make_user(user_id, f"Коллега {user_id}")
        clock.advance(1)
        await games.join_game(db, game.id, user_id, JoinVia.LINK, clock.now())
    return game


async def test_request_upgrade_creates_and_reuses_payment(db, full_game, prices, clock) -> None:
    payment = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    assert not isinstance(payment, billing.PaymentOutcome)
    assert (payment.amount_rub, payment.status, payment.payer_id) == (490, PaymentStatus.CREATED, 11)
    clock.advance(3600)
    again = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    assert again == payment
    clock.advance(24 * 3600)
    fresh = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    assert fresh.inv_id != payment.inv_id


async def test_only_members_may_pay(db, full_game, prices, clock, make_user) -> None:
    await make_user(500, "Чужой")
    with pytest.raises(games.PermissionDenied):
        await billing.request_upgrade(db, full_game.id, 500, Tier.S, prices, clock.now())
    with pytest.raises(billing.NoUpgradeAvailable):
        await billing.request_upgrade(db, full_game.id, ORGANIZER, Tier.FREE, prices, clock.now())


async def test_confirm_payment_upgrades_and_activates_queue_in_join_order(db, full_game, prices, clock) -> None:
    payment = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    outcome = await billing.confirm_payment(db, payment.inv_id, prices, raw="OutSum=490", now=clock.now())
    assert outcome is not None and not outcome.already_processed
    assert outcome.game is not None and (outcome.game.tier, outcome.game.participant_limit) == (Tier.S, 30)
    assert [p.user_id for p in outcome.activated] == [10, 11, 12]
    replay = await billing.confirm_payment(db, payment.inv_id, prices, raw="again", now=clock.now())
    assert replay is not None and replay.already_processed and replay.activated == []
    stored = await repo.get_payment(db, payment.inv_id)
    assert stored is not None and stored.status == PaymentStatus.PAID and stored.raw == "OutSum=490"
    assert await billing.confirm_payment(db, 9999, prices, raw="", now=clock.now()) is None


async def test_upgrade_price_difference_and_double_payment(db, full_game, prices, clock) -> None:
    first = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    second = await billing.request_upgrade(db, full_game.id, 12, Tier.S, prices, clock.now())
    await billing.confirm_payment(db, first.inv_id, prices, raw="", now=clock.now())
    outcome = await billing.confirm_payment(db, second.inv_id, prices, raw="", now=clock.now())
    assert outcome is not None and outcome.duplicates == (first.inv_id, second.inv_id)
    assert outcome.fully_excess and outcome.excess_rub == 490
    with pytest.raises(billing.NoUpgradeAvailable):
        await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    to_m = await billing.request_upgrade(db, full_game.id, 11, Tier.M, prices, clock.now())
    assert to_m.amount_rub == 990 - 980 == 10


async def test_an_older_link_for_a_bigger_tier_is_charged_too_much(db, full_game, prices, clock) -> None:
    """§4: an upgrade costs the difference. A link priced before another payment arrived overcharges."""
    to_m = await billing.request_upgrade(db, full_game.id, ORGANIZER, Tier.M, prices, clock.now())
    to_s = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    assert (to_m.amount_rub, to_s.amount_rub) == (990, 490)
    s_paid = await billing.confirm_payment(db, to_s.inv_id, prices, raw="", now=clock.now())
    assert s_paid is not None and s_paid.excess_rub == 0
    m_paid = await billing.confirm_payment(db, to_m.inv_id, prices, raw="", now=clock.now())
    assert m_paid is not None and m_paid.game is not None and m_paid.game.tier == Tier.M
    assert (m_paid.excess_rub, m_paid.fully_excess, m_paid.duplicates) == (490, False, ())


async def test_an_older_link_for_a_smaller_tier_buys_nothing(db, full_game, prices, clock) -> None:
    to_m = await billing.request_upgrade(db, full_game.id, ORGANIZER, Tier.M, prices, clock.now())
    to_s = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    await billing.confirm_payment(db, to_m.inv_id, prices, raw="", now=clock.now())
    s_paid = await billing.confirm_payment(db, to_s.inv_id, prices, raw="", now=clock.now())
    assert s_paid is not None and s_paid.fully_excess and s_paid.excess_rub == 490 and s_paid.duplicates == ()
    assert s_paid.game is not None and (s_paid.game.tier, s_paid.game.participant_limit) == (Tier.M, 100)


async def test_a_payment_after_the_draw_is_not_applied(db, full_game, prices, clock, rng) -> None:
    payment = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    await games.run_draw(db, full_game.id, ORGANIZER, now=clock.now(), rng=rng)
    outcome = await billing.confirm_payment(db, payment.inv_id, prices, raw="", now=clock.now())
    assert outcome is not None and outcome.game_not_collecting and outcome.fully_excess
    assert outcome.activated == []
    assert outcome.game is not None and (outcome.game.tier, outcome.game.participant_limit) == (Tier.FREE, 10)


async def test_grant_and_refund(db, full_game, prices, clock) -> None:
    outcome = await billing.grant_tier(db, full_game.id, Tier.L, 5000, prices, clock.now())
    assert outcome.game is not None and outcome.game.participant_limit == 300
    assert outcome.payment.status == PaymentStatus.GRANTED and outcome.payment.provider == "manual"
    with pytest.raises(billing.NoUpgradeAvailable):
        await billing.request_upgrade(db, full_game.id, 1, Tier.L, prices, clock.now())
    refunded = await billing.refund_payment(db, outcome.payment.inv_id)
    assert refunded is not None and refunded.status == PaymentStatus.REFUNDED
    assert await repo.paid_sum(db, full_game.id) == 0


async def test_upgrade_with_nothing_left_to_pay_applies_at_once(db, full_game, prices, clock) -> None:
    await billing.grant_tier(db, full_game.id, Tier.S, 990, prices, clock.now())
    outcome = await billing.request_upgrade(db, full_game.id, 1, Tier.M, prices, clock.now())
    assert isinstance(outcome, billing.PaymentOutcome)
    assert outcome.game is not None and outcome.game.tier == Tier.M and outcome.payment.amount_rub == 0


async def test_upgrade_only_while_collecting(db, full_game, prices, clock, rng) -> None:
    await games.run_draw(db, full_game.id, ORGANIZER, now=clock.now(), rng=rng)
    with pytest.raises(games.WrongStatus):
        await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())


# --- relay ---------------------------------------------------------------------------------


@pytest.fixture
async def drawn_game(db, full_game, clock, rng):
    result = await games.run_draw(db, full_game.id, ORGANIZER, now=clock.now(), rng=rng)
    return result


async def test_relay_routes_both_ways_without_revealing_santa(db, drawn_game, clock) -> None:
    game_id = drawn_game.game.id
    santa = 1
    receiver = drawn_game.pairs[santa]
    route = await relay.route_new(db, game_id, santa, RelayDirection.TO_RECEIVER)
    assert route.recipient.user_id == receiver
    question = await relay.send_relay(db, route, "Какой размер?", clock.now())
    reply_route = await relay.route_reply(db, question.id, receiver)
    assert reply_route.recipient.user_id == santa and reply_route.direction == RelayDirection.TO_SANTA
    to_santa = await relay.route_new(db, game_id, receiver, RelayDirection.TO_SANTA)
    assert to_santa.recipient.user_id == santa
    with pytest.raises(games.PermissionDenied):
        await relay.route_reply(db, question.id, santa)


async def test_relay_daily_limit(db, drawn_game, clock) -> None:
    route = await relay.route_new(db, drawn_game.game.id, 1, RelayDirection.TO_RECEIVER)
    for _ in range(relay.RELAYS_PER_DAY):
        await relay.send_relay(db, route, "?", clock.now())
    with pytest.raises(relay.RelayLimitReached):
        await relay.send_relay(db, route, "?", clock.now())
    await relay.send_relay(db, route, "?", clock.now() + timedelta(days=1, seconds=1))


async def test_report_and_blocking(db, drawn_game, clock) -> None:
    santa, receiver = 2, drawn_game.pairs[2]
    route = await relay.route_new(db, drawn_game.game.id, santa, RelayDirection.TO_RECEIVER)
    message = await relay.send_relay(db, route, "грубость", clock.now())
    with pytest.raises(games.PermissionDenied):
        await relay.report_relay(db, message.id, santa, clock.now())
    report = await relay.report_relay(db, message.id, receiver, clock.now())
    assert (report.reporter_id, report.reported_id, report.text) == (receiver, santa, "грубость")
    await repo.set_blocked(db, santa, True)
    with pytest.raises(games.UserBlocked):
        await relay.send_relay(db, route, "ещё", clock.now())


async def test_relay_needs_anon_chat_and_draw(db, full_game, clock, rng) -> None:
    with pytest.raises(games.WrongStatus):
        await relay.route_new(db, full_game.id, 1, RelayDirection.TO_RECEIVER)
    await games.run_draw(db, full_game.id, ORGANIZER, now=clock.now(), rng=rng)
    await games.update_game_settings(db, full_game.id, ORGANIZER, anon_chat=False)
    with pytest.raises(relay.AnonChatOff):
        await relay.route_new(db, full_game.id, 1, RelayDirection.TO_RECEIVER)
    waiting = await repo.get_participant(db, full_game.id, 12)
    assert waiting is not None and waiting.status == ParticipantStatus.WAITING


async def test_invids_stay_unique_after_restoring_an_older_backup(db, full_game, prices, clock) -> None:
    """Robokassa needs a new InvId for every payment, also after the database was rolled back."""
    issued = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    await db.execute("DELETE FROM payments")  # the restored copy predates this payment
    await db.execute("DELETE FROM sqlite_sequence WHERE name = 'payments'")
    clock.advance(600)  # a restore takes minutes
    fresh = await billing.request_upgrade(db, full_game.id, 11, Tier.S, prices, clock.now())
    assert fresh.inv_id > issued.inv_id
    again = await billing.request_upgrade(db, full_game.id, 12, Tier.S, prices, clock.now())
    assert again.inv_id == fresh.inv_id + 1
