"""Robokassa ResultURL (§7) and end-to-end scenario 3 (§14): limit, waiting list, payment, replay."""

from __future__ import annotations

import asyncio
import hashlib
import json

import pytest

from app import repo
from app.config import Config, load_config
from app.context import AppContext
from app.core import billing, texts
from app.core.clock import FakeClock
from app.core.models import GameStatus, ParticipantStatus, PaymentStatus, Tier
from app.handlers.views import Action
from app.main import build_context
from app.payments import robokassa
from tests.bot import ADMIN_ID, Bot
from tests.site import (
    ORGANIZER,
    fill,
    full_game_with_waiting,
    pay_click,
    payment_of,
    result_form,
    result_signature,
    site_client,
    status_in,
)
from tools.fake_max import FakeMaxApi

RESULT = "/pay/robokassa/result"


async def events(ctx: AppContext, kind: str) -> int:
    return int(await ctx.db.fetchval("SELECT COUNT(*) FROM events WHERE type = ?", (kind,)))


async def outbox_rows(ctx: AppContext) -> int:
    return int(await ctx.db.fetchval("SELECT COUNT(*) FROM outbox"))


# --- signatures ------------------------------------------------------------------------------------


def test_result_signature_matches_an_independent_vector(config: Config) -> None:
    notice = robokassa.ResultNotice.parse({"OutSum": "490.000000", "InvId": "17", "SignatureValue": "x"})
    assert notice is not None and notice.inv_id == 17
    assert robokassa.result_signature(config, notice) == hashlib.md5(b"490.000000:17:test-pass-2").hexdigest()


def test_result_signature_with_shp_parameters_and_sha512(env: dict[str, str]) -> None:
    sha512 = load_config({**env, "ROBOKASSA_HASH": "sha512"}, announce=lambda _: None)
    params = {"OutSum": "490.00", "InvId": "3", "SignatureValue": "x", "Shp_b": "2", "Shp_a": "1"}
    notice = robokassa.ResultNotice.parse(params)
    assert notice is not None
    expected = hashlib.sha512(b"490.00:3:test-pass-2:Shp_a=1:Shp_b=2").hexdigest()
    assert robokassa.result_signature(sha512, notice) == expected


@pytest.mark.parametrize("params", [
    {},
    {"OutSum": "490.00", "InvId": "17"},
    {"OutSum": "490.00", "InvId": "abc", "SignatureValue": "x"},
    {"OutSum": "490.00", "InvId": "0", "SignatureValue": "x"},
    {"OutSum": "много", "InvId": "17", "SignatureValue": "x"},
    {"OutSum": "NaN", "InvId": "17", "SignatureValue": "x"},
])
def test_malformed_notices_are_rejected(params: dict[str, str]) -> None:
    assert robokassa.ResultNotice.parse(params) is None


def test_signature_comparison_ignores_case_and_surrounding_spaces() -> None:
    assert robokassa.signature_matches("abc123", " ABC123 ")
    assert not robokassa.signature_matches("abc123", "abc124")


# --- §14 scenario 3: limit and payment -------------------------------------------------------------------


async def test_waiting_user_pays_and_the_game_is_upgraded_once(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    assert (await status_in(bot.ctx, game, 250)) == ParticipantStatus.WAITING
    payment = await pay_click(bot, late, game)
    assert (payment.status, payment.amount_rub, payment.tier) == (PaymentStatus.CREATED, 490, Tier.S)

    async with site_client(bot.ctx) as client:
        form = result_form(payment, "490.000000")
        response = await client.post(RESULT, data=form)
        assert (response.status, await response.text()) == (200, f"OK{payment.inv_id}")
        assert response.content_type == "text/plain"

        upgraded = await bot.game(game.id)
        assert (upgraded.tier, upgraded.participant_limit) == (Tier.S, 30)
        assert (await status_in(bot.ctx, game, 250)) == ParticipantStatus.ACTIVE
        paid = await payment_of(bot.ctx, payment.inv_id)
        assert paid.status == PaymentStatus.PAID and paid.paid_at is not None and paid.raw is not None
        assert json.loads(paid.raw) == {"OutSum": "490.000000", "InvId": str(payment.inv_id), "Fee": "17.15",
                                        "PaymentMethod": "SBP", "IncCurrLabel": "SBPR"}
        await bot.drain()
        assert texts.spot_opened(game.title) in late.texts
        assert texts.payment_received(title=game.title, limit=30) in late.texts
        assert texts.payer_paid(payer="Опоздавший", title=game.title, limit=30) in api.texts_to(100)

        sent, rows, successes = len(api.sent), await outbox_rows(bot.ctx), await events(bot.ctx, "pay_success")
        replay = await client.post(RESULT, data=form)
        assert (replay.status, await replay.text()) == (200, f"OK{payment.inv_id}")
        await bot.drain()
        assert (len(api.sent), await outbox_rows(bot.ctx), await events(bot.ctx, "pay_success")) == (
            sent, rows, successes)
        assert await bot.game(game.id) == upgraded


async def test_concurrent_result_calls_apply_the_payment_once(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    payment = await pay_click(bot, late, game)
    async with site_client(bot.ctx) as client:
        form = result_form(payment)
        first, second = await asyncio.gather(client.post(RESULT, data=form), client.post(RESULT, data=form))
        assert [first.status, second.status] == [200, 200]
        assert {await first.text(), await second.text()} == {f"OK{payment.inv_id}"}
    await bot.drain()
    assert await events(bot.ctx, "pay_success") == 1
    assert late.texts.count(texts.payment_received(title=game.title, limit=30)) == 1
    assert late.texts.count(texts.spot_opened(game.title)) == 1


async def test_get_notice_and_test_mode_two_decimals_upper_case_signature(bot: Bot, clock: FakeClock) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    payment = await pay_click(bot, late, game)
    params = {"OutSum": "490.00", "InvId": str(payment.inv_id),
              "SignatureValue": result_signature("490.00", payment.inv_id).upper(), "IsTest": "1"}
    async with site_client(bot.ctx) as client:
        response = await client.get(RESULT, params=params)
        assert (response.status, await response.text()) == (200, f"OK{payment.inv_id}")
    assert (await payment_of(bot.ctx, payment.inv_id)).status == PaymentStatus.PAID


# --- refusals ---------------------------------------------------------------------------------------------


async def test_bad_signature_is_refused_and_alerted_once_per_10_minutes(
    bot: Bot, api: FakeMaxApi, clock: FakeClock
) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    payment = await pay_click(bot, late, game)
    forged = {**result_form(payment), "SignatureValue": result_signature("490.000000", payment.inv_id, "guess")}
    async with site_client(bot.ctx) as client:
        for _ in range(3):
            response = await client.post(RESULT, data=forged)
            assert response.status == 400 and not (await response.text()).startswith("OK")
        clock.advance(robokassa.BAD_SIGNATURE_ALERT_INTERVAL)
        assert (await client.post(RESULT, data=forged)).status == 400
    await bot.drain()
    alerts = [t for t in api.texts_to(ADMIN_ID) if "неверной подписью" in t]
    assert len(alerts) == 2 and "пропущено: 2" in alerts[1]
    assert (await payment_of(bot.ctx, payment.inv_id)).status == PaymentStatus.CREATED
    assert (await bot.game(game.id)).tier == Tier.FREE


async def test_amount_mismatch_is_refused_and_alerted(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    payment = await pay_click(bot, late, game)
    async with site_client(bot.ctx) as client:
        response = await client.post(RESULT, data=result_form(payment, "1.000000"))
        assert (response.status, await response.text()) == (400, "amount mismatch")
    await bot.drain()
    assert (await payment_of(bot.ctx, payment.inv_id)).status == PaymentStatus.CREATED
    assert (await bot.game(game.id)).participant_limit == 10
    assert texts.amount_mismatch_alert(inv_id=payment.inv_id, received="1.000000", expected=490) in api.texts_to(
        ADMIN_ID)


async def test_unknown_invoice_with_a_valid_signature_alerts_the_admin(ctx: AppContext, api: FakeMaxApi) -> None:
    params = {"OutSum": "490.000000", "InvId": "777", "SignatureValue": result_signature("490.000000", 777)}
    async with site_client(ctx) as client:
        response = await client.post(RESULT, data=params)
        assert response.status == 400
    await ctx.outbox.drain()
    assert texts.unknown_invoice_alert(777) in api.texts_to(ADMIN_ID)


async def test_malformed_notice_is_refused_without_an_alert(ctx: AppContext, api: FakeMaxApi) -> None:
    async with site_client(ctx) as client:
        assert (await client.post(RESULT, data={"InvId": "5"})).status == 400
        assert (await client.get(RESULT)).status == 400
    await ctx.outbox.drain()
    assert api.texts_to(ADMIN_ID) == []


async def test_live_mode_verifies_with_the_live_password(
    env: dict[str, str], api: FakeMaxApi, clock: FakeClock
) -> None:
    live = load_config({**env, "ROBOKASSA_TEST": "0", "ROBOKASSA_PASSWORD1": "live-1",
                        "ROBOKASSA_PASSWORD2": "live-2"}, announce=lambda _: None)
    ctx = await build_context(live, api=api, clock=clock)
    try:
        bot = Bot(ctx, api)
        late = bot.person(250, "Опоздавший")
        game = await full_game_with_waiting(bot, clock, late)
        payment = await pay_click(bot, late, game)
        async with site_client(ctx) as client:
            with_test_password = await client.post(RESULT, data=result_form(payment))
            assert with_test_password.status == 400
            live_form = {**result_form(payment), "SignatureValue": result_signature("490.000000", payment.inv_id,
                                                                                    "live-2")}
            assert (await client.post(RESULT, data=live_form)).status == 200
        assert (await bot.game(game.id)).tier == Tier.S
    finally:
        await ctx.wait_background()
        await ctx.db.close()


async def test_payments_disabled_answers_503(env: dict[str, str], api: FakeMaxApi, clock: FakeClock) -> None:
    no_shop = load_config({**env, "ROBOKASSA_MERCHANT_LOGIN": ""}, announce=lambda _: None)
    ctx = await build_context(no_shop, api=api, clock=clock)
    try:
        async with site_client(ctx) as client:
            params = {"OutSum": "490.00", "InvId": "1", "SignatureValue": result_signature("490.00", 1)}
            assert (await client.post(RESULT, data=params)).status == 503
    finally:
        await ctx.db.close()


# --- after-effects ------------------------------------------------------------------------------------------


async def test_two_payments_for_the_same_tier_alert_the_admin(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    first_payer, second_payer = bot.person(250, "Первый"), bot.person(251, "Второй")
    game = await full_game_with_waiting(bot, clock, first_payer, second_payer)
    first = await pay_click(bot, first_payer, game)
    second = await pay_click(bot, second_payer, game)
    async with site_client(bot.ctx) as client:
        for payment in (first, second):
            assert (await client.post(RESULT, data=result_form(payment))).status == 200
    await bot.drain()
    expected = texts.double_payment(code=game.code, first_inv=first.inv_id, second_inv=second.inv_id)
    assert api.texts_to(ADMIN_ID).count(expected) == 1


async def test_payment_after_the_draw_is_recorded_and_alerted(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    payment = await pay_click(bot, late, game)
    await repo.update_game(bot.ctx.db, game.id, status=GameStatus.DRAWN)
    async with site_client(bot.ctx) as client:
        assert (await client.post(RESULT, data=result_form(payment))).status == 200
    await bot.drain()
    assert texts.payment_after_draw(code=game.code, inv_id=payment.inv_id) in api.texts_to(ADMIN_ID)
    assert (await payment_of(bot.ctx, payment.inv_id)).status == PaymentStatus.PAID
    email = bot.ctx.config.support_email
    assert api.last_text(late.user_id) == texts.payment_too_late(title=game.title, amount=490, support_email=email)
    assert not any("оплатил(а)" in text for text in api.texts_to(ORGANIZER))
    assert (await bot.game(game.id)).participant_limit == 10, "a drawn game is not enlarged"

    await bot.forge(late, f"{Action.OPEN_GAME}:{game.id}")
    assert "я напишу" not in late.screen_text
    await bot(late.press(texts.BTN_LEAVE))
    assert late.screen_text == texts.confirm_leave(game.title)
    await bot(late.press(texts.BTN_CONFIRM_LEAVE))
    assert await status_in(bot.ctx, game, late.user_id) == ParticipantStatus.LEFT


async def test_an_older_link_paid_after_another_upgrade_is_flagged_for_a_refund(
    bot: Bot, api: FakeMaxApi, clock: FakeClock
) -> None:
    """The organizer opens «до 100 — 990 ₽», a waiting person pays 490 ₽ for S, then the organizer pays."""
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    olga = bot.person(ORGANIZER, "Ольга")
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_UPGRADE))
    await bot(olga.press(texts.btn_tier(100, 990)))
    to_m = next(p for p in await repo.payments_of_game(bot.ctx.db, game.id) if p.payer_id == ORGANIZER)
    to_s = await pay_click(bot, late, game)
    async with site_client(bot.ctx) as client:
        for payment in (to_s, to_m):
            assert (await client.post(RESULT, data=result_form(payment))).status == 200
    await bot.drain()
    alert = texts.overpayment(code=game.code, inv_id=to_m.inv_id, amount=990, excess=490)
    assert alert in api.texts_to(ADMIN_ID)
    assert (await bot.game(game.id)).participant_limit == 100
    assert "490 ₽ мы вернём" in api.last_text(ORGANIZER)


async def test_notice_after_a_refund_changes_nothing(bot: Bot, clock: FakeClock) -> None:
    late = bot.person(250, "Опоздавший")
    game = await full_game_with_waiting(bot, clock, late)
    payment = await pay_click(bot, late, game)
    await repo.mark_payment(bot.ctx.db, payment.inv_id, PaymentStatus.REFUNDED)
    async with site_client(bot.ctx) as client:
        response = await client.post(RESULT, data=result_form(payment))
        assert (response.status, await response.text()) == (200, f"OK{payment.inv_id}")
    assert (await payment_of(bot.ctx, payment.inv_id)).status == PaymentStatus.REFUNDED
    assert (await bot.game(game.id)).tier == Tier.FREE


async def test_a_full_paid_game_does_not_call_itself_free(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    game = await full_game_with_waiting(bot, clock)
    await billing.grant_tier(bot.ctx.db, game.id, Tier.S, 490, await bot.ctx.prices(), clock.now())
    await fill(bot, clock, game, range(210, 230))
    newcomer = bot.person(260, "Новенький")
    await bot.join(newcomer, game, wishes=None)
    assert await status_in(bot.ctx, game, newcomer.user_id) == ParticipantStatus.WAITING
    assert api.last_text(newcomer.user_id) == texts.waiting_list(free=False, current_limit=30, limit=100, price=500)
