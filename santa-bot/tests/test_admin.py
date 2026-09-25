"""§9 admin commands and buttons, driven through ``process_update`` like the real webhook."""

from __future__ import annotations

import pytest

from app import repo
from app.core import texts
from app.core.clock import FakeClock
from app.core.models import Game, GameStatus, ParticipantStatus, PaymentProvider, PaymentStatus, Tier
from app.payments import robokassa
from tests.bot import ADMIN_ID, Bot
from tests.site import ORGANIZER, full_game_with_waiting, pay_click, result_form, status_in
from tools.fake_max import FakeMaxApi, FakeUser


@pytest.fixture
def admin(bot: Bot) -> FakeUser:
    return bot.person(ADMIN_ID, "Админ")


async def full_game(bot: Bot, clock: FakeClock) -> tuple[Game, FakeUser]:
    """'Отдел продаж': Ольга and 9 guests, Дмитрий in the queue."""
    dmitry = bot.person(301, "Дмитрий")
    return await full_game_with_waiting(bot, clock, dmitry), dmitry


async def test_stats_counts_the_viral_loop_end_to_end(bot: Bot, api: FakeMaxApi, clock: FakeClock,
                                                      admin: FakeUser) -> None:
    game, dmitry = await full_game(bot, clock)
    payment = await pay_click(bot, dmitry, game)
    assert (await robokassa.process_result(bot.ctx, result_form(payment))).status == 200
    olga = bot.person(ORGANIZER, "Ольга")
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    await bot.drain()
    await bot(dmitry.press(texts.BTN_NEW_GAME_ELSEWHERE))
    await bot(dmitry.press(texts.BTN_SKIP))
    await bot(dmitry.press(texts.BUDGET_PRESETS[0]))
    await bot(dmitry.press(texts.BTN_DATE_UNKNOWN))
    await bot(dmitry.press(texts.BTN_ONLY_ORGANIZE))

    await bot(admin.say("/stats"))  # the admin is the 12th new user; he has not consented

    today = admin.last_text.split("\n\n")[0]
    assert today == (
        "Сегодня:\n"
        "новые пользователи 12, согласий 11\n"
        "игр создано 2, из них с 3+ участниками 1\n"
        "жеребьёвок 1, участников в среднем 11,0, медиана 11,0\n"
        "упёрлись в бесплатный лимит 1\n"
        "оплачено игр 1, выручка 490 ₽, конверсия 100%\n"
        "участник → организатор 10%\n"
        "игр по кнопке «в другом чате» 1\n"
        "пары не дошли 0%"
    )
    assert admin.last_text.endswith("Открытых жалоб: 0\nОткуда организаторы (7 дней): direct 1, ref 1")


async def test_admin_commands_are_hidden_from_everyone_else(bot: Bot, clock: FakeClock) -> None:
    game, dmitry = await full_game(bot, clock)
    await bot(dmitry.say("/stats"))
    assert dmitry.last_text == texts.UNKNOWN_INPUT
    await bot.forge(dmitry, f"ag:{game.id}:S")
    assert dmitry.last_text == texts.NOT_ALLOWED
    assert (await bot.game(game.id)).tier == Tier.FREE


async def test_admin_commands_work_before_consent(bot: Bot, admin: FakeUser) -> None:
    await bot(admin.say("/admin"))
    assert admin.last_text == texts.ADMIN_HELP
    await bot(admin.say("/stats"))
    assert admin.last_text.startswith("Сегодня:")


async def test_game_summary_grant_button_and_queue(bot: Bot, api: FakeMaxApi, clock: FakeClock,
                                                    admin: FakeUser) -> None:
    game, dmitry = await full_game(bot, clock)
    await bot(admin.say(f"/game {game.code.lower()}"))
    summary = admin.last_text
    assert summary.startswith(f"Игра {game.code} · «Отдел продаж»\nСтатус: идёт набор · тариф free\n"
                              "Участников: 10 из 10, в очереди 1\nОрганизатор: 100")
    assert summary.endswith("Платежей нет.")
    assert admin.button_texts == ["Выдать S", "Выдать M", "Выдать L", texts.BTN_ADMIN_CANCEL_GAME]

    await bot(admin.press("Выдать S"))
    await bot.drain()
    assert admin.last_text == f"Игра {game.code}: выдан тариф S, лимит 30."
    assert await status_in(bot.ctx, game, dmitry.user_id) == ParticipantStatus.ACTIVE
    assert dmitry.last_text == texts.spot_opened("Отдел продаж")
    await bot(dmitry.say("Книгу про путешествия"))
    assert dmitry.last_text == texts.WISHES_SAVED
    assert api.last_text(ORGANIZER) == "Игра «Отдел продаж» расширена до 30 участников."
    (payment,) = await repo.payments_of_game(bot.ctx.db, game.id)
    assert (payment.status, payment.provider, payment.payer_id, payment.amount_rub) == (
        PaymentStatus.GRANTED, PaymentProvider.MANUAL, None, 0)

    await bot(admin.say(f"/game {game.code}"))
    assert admin.last_text.endswith(f"InvId {payment.inv_id} · S · 0 ₽ · granted")


async def test_grant_command_records_the_amount(bot: Bot, clock: FakeClock, admin: FakeUser) -> None:
    game, _ = await full_game(bot, clock)
    for bad in ("/grant", f"/grant {game.code}", f"/grant {game.code} XL", f"/grant {game.code} M много"):
        await bot(admin.say(bad))
        assert admin.last_text == texts.GRANT_USAGE, bad
    await bot(admin.say("/grant ZZZZZZ M"))
    assert admin.last_text == texts.ADMIN_GAME_NOT_FOUND

    await bot(admin.say(f"/grant {game.code} m 990"))
    upgraded = await bot.game(game.id)
    assert (upgraded.tier, upgraded.participant_limit) == (Tier.M, 100)
    assert await repo.paid_sum(bot.ctx.db, game.id) == 990
    await bot(admin.say("/stats"))
    assert "оплачено игр 1, выручка 990 ₽" in admin.last_text

    await repo.update_game(bot.ctx.db, game.id, status=GameStatus.DRAWN)
    await bot(admin.say(f"/grant {game.code} L"))
    assert admin.last_text == texts.UPGRADE_ONLY_BEFORE_DRAW
    assert (await bot.game(game.id)).tier == Tier.M


async def test_admin_cancels_a_game_after_confirmation(bot: Bot, api: FakeMaxApi, clock: FakeClock,
                                                       admin: FakeUser) -> None:
    game, dmitry = await full_game(bot, clock)
    await bot(admin.say(f"/game {game.code}"))
    await bot(admin.press(texts.BTN_ADMIN_CANCEL_GAME))
    assert admin.last_text.startswith(f"Отменить игру {game.code} «Отдел продаж»?")
    assert (await bot.game(game.id)).status == GameStatus.COLLECTING

    await bot(admin.press(texts.BTN_CONFIRM_CANCEL_GAME))
    await bot.drain()
    assert (await bot.game(game.id)).status == GameStatus.CANCELLED
    assert admin.last_text == f"Игра {game.code} отменена, участники получили уведомление."
    notice = texts.game_cancelled_by_service(title="Отдел продаж", support_email="help@santa.example.ru")
    for user_id in (ORGANIZER, 201, 209, dmitry.user_id):
        assert api.last_text(user_id) == notice

    await bot(admin.press(texts.BTN_CONFIRM_CANCEL_GAME))
    assert admin.last_text == texts.ADMIN_GAME_CLOSED
    await bot(admin.say(f"/game {game.code}"))
    assert api.last_to(ADMIN_ID).buttons == [], "a cancelled game has nothing to grant or cancel"


async def test_refund_marks_only_paid_payments(bot: Bot, clock: FakeClock, admin: FakeUser) -> None:
    game, dmitry = await full_game(bot, clock)
    payment = await pay_click(bot, dmitry, game)
    await bot(admin.say(f"/refund {payment.inv_id}"))
    assert admin.last_text == texts.payment_not_refundable(inv_id=payment.inv_id, status="created")
    await robokassa.process_result(bot.ctx, result_form(payment))

    await bot(admin.say(f"/refund {payment.inv_id}"))
    assert admin.last_text == texts.refunded(payment.inv_id)
    refunded = await repo.get_payment(bot.ctx.db, payment.inv_id)
    assert refunded is not None and refunded.status == PaymentStatus.REFUNDED
    await bot(admin.say("/stats"))
    assert "выручка 0 ₽" in admin.last_text
    await bot(admin.say("/refund 999"))
    assert admin.last_text == texts.PAYMENT_NOT_FOUND
    await bot(admin.say("/refund первый"))
    assert admin.last_text == texts.REFUND_USAGE


async def test_price_changes_settings_and_validates(bot: Bot, admin: FakeUser) -> None:
    await bot(admin.say("/price"))
    assert admin.last_text == ("Цены сейчас:\nбесплатно — до 10\nS — до 30 за 490 ₽\n"
                               "M — до 100 за 990 ₽\nL — до 300 за 2490 ₽")
    await bot(admin.say("/price S 590"))
    await bot(admin.say("/price free 12"))
    await bot(admin.say("/price limit_S 40"))
    assert admin.last_text == ("Цены сейчас:\nбесплатно — до 12\nS — до 40 за 590 ₽\n"
                               "M — до 100 за 990 ₽\nL — до 300 за 2490 ₽")
    settings = await bot.ctx.settings()
    assert (settings.price_S, settings.free_limit, settings.limit_S) == (590, 12, 40)

    await bot(admin.say("/price M 500"))
    assert admin.last_text == "Цена тарифа M должна быть больше 590 ₽."
    await bot(admin.say("/price free 2"))
    assert admin.last_text == "Бесплатный лимит должен быть не меньше 3."
    for bad in ("/price XL 10", "/price S", "/price S дорого"):
        await bot(admin.say(bad))
        assert admin.last_text == texts.PRICE_USAGE, bad
    assert (await bot.ctx.settings()).price_M == 990


async def test_block_and_unblock(bot: Bot, admin: FakeUser) -> None:
    ivan = bot.person(501, "Иван")
    await bot.onboard(ivan)
    await bot(admin.say("/block 501"))
    assert admin.last_text == texts.user_blocked(501)
    await bot(ivan.press(texts.BTN_CREATE_GAME))
    assert ivan.last_text == texts.blocked("help@santa.example.ru")

    await bot(admin.press(texts.BTN_UNBLOCK))
    assert admin.last_text == texts.user_unblocked(501)
    user = await repo.get_user(bot.ctx.db, 501)
    assert user is not None and not user.blocked
    await bot(admin.say("/unblock 777"))
    assert admin.last_text == texts.USER_NOT_FOUND
    await bot(admin.say("/block Иван"))
    assert admin.last_text == texts.BLOCK_USAGE


async def test_maintenance_refuses_new_games_and_joins(bot: Bot, admin: FakeUser) -> None:
    olga = bot.person(ORGANIZER, "Ольга")
    await bot.onboard(olga)
    game = await bot.create_game(olga)
    await bot(admin.say("/maintenance on"))
    assert admin.last_text == texts.MAINTENANCE_ON
    assert (await bot.ctx.settings()).maintenance

    newcomer = bot.person(601, "Новичок")
    await bot.onboard(newcomer, f"j_{game.code}")
    assert newcomer.last_text == texts.MAINTENANCE
    assert await repo.get_participant(bot.ctx.db, game.id, 601) is None
    await bot(olga.press(texts.BTN_CREATE_GAME))
    assert olga.last_text == texts.MAINTENANCE

    await bot(admin.say("/maintenance off"))
    assert admin.last_text == texts.MAINTENANCE_OFF
    await bot(newcomer.say(game.code))
    assert await repo.get_participant(bot.ctx.db, game.id, 601) is not None
    await bot(admin.say("/maintenance"))
    assert admin.last_text == texts.MAINTENANCE_USAGE
