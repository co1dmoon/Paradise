"""Risky edges of the bot flows: double taps, stale buttons, limits, pagination, payment links."""

from __future__ import annotations

from collections.abc import Sequence
from urllib.parse import parse_qs, urlsplit

import pytest

from app import repo
from app.core import billing, games, texts
from app.core.clock import FakeClock
from app.core.dates import DateError
from app.core.models import Game, GameStatus, JoinVia, ParticipantStatus, PaymentStatus, StateKind, Tier
from app.handlers.views import Action
from app.payments import robokassa
from tests.bot import Bot
from tests.helpers import consented_user
from tools.fake_max import FakeMaxApi, FakeUser, message_created

ORGANIZER = 100


@pytest.fixture
async def olga(bot: Bot) -> FakeUser:
    organizer = bot.person(ORGANIZER, "Ольга")
    await bot.onboard(organizer)
    return organizer


async def fill(bot: Bot, clock: FakeClock, game: Game, ids: Sequence[int]) -> list[FakeUser]:
    """Consented users joined through the core (fast); they can still press forged buttons or type."""
    people = []
    for user_id in ids:
        await consented_user(bot.ctx.db, clock, user_id, f"Гость {user_id}")
        clock.advance(1)
        await games.join_game(bot.ctx.db, game.id, user_id, JoinVia.LINK, clock.now())
        people.append(bot.person(user_id, f"Гость {user_id}"))
    return people


async def open_panel(bot: Bot, organizer: FakeUser, game: Game) -> None:
    await bot.forge(organizer, f"{Action.PANEL}:{game.id}")


# --- double taps ----------------------------------------------------------------------------------------


async def test_double_tap_on_draw_sends_each_pair_once(bot: Bot, api: FakeMaxApi, clock, olga) -> None:
    game = await bot.create_game(olga)
    await fill(bot, clock, game, [201, 202, 203])
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_DRAW))
    first, second = olga.press(texts.BTN_CONFIRM_DRAW), olga.press(texts.BTN_CONFIRM_DRAW)
    await bot(first)
    await bot(second)
    assert olga.screen_text == texts.ALREADY_DRAWN_ACTION
    await bot.drain()
    for user_id in (ORGANIZER, 201, 202, 203):
        assert sum(t.startswith("Жеребьёвка в игре") for t in api.texts_to(user_id)) == 1
    assert sum(t.startswith("Готово! Пары отправлены") for t in olga.texts) == 1


async def test_double_tap_on_the_last_wizard_step_creates_one_game(bot: Bot, olga) -> None:
    await bot(olga.press(texts.BTN_CREATE_GAME))
    await bot(olga.press(texts.BTN_SKIP))
    await bot(olga.press(texts.BUDGET_PRESETS[0]))
    await bot(olga.press(texts.BTN_DATE_UNKNOWN))
    first, second = olga.press(texts.BTN_I_PARTICIPATE), olga.press(texts.BTN_I_PARTICIPATE)
    await bot(first)
    await bot(second)
    assert len(await repo.games_of_user(bot.ctx.db, ORGANIZER)) == 1
    assert olga.screen_text == texts.BUTTON_OUTDATED


async def test_double_tap_on_pay_reuses_the_payment(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    await fill(bot, clock, game, range(201, 210))
    late = bot.person(250, "Опоздавший")
    await bot.join(late, game, wishes=None)
    assert late.screen_text.startswith("Мест нет: в бесплатной игре до 10 участников.")
    first, second = late.press(texts.btn_pay(490)), late.press(texts.btn_pay(490))
    await bot(first)
    await bot(second)
    payments = await repo.payments_of_game(bot.ctx.db, game.id)
    assert len(payments) == 1 and payments[0].status == PaymentStatus.CREATED and payments[0].payer_id == 250


# --- the pay callback and the payment link -----------------------------------------------------------------


async def test_pay_callback_sends_a_signed_robokassa_link(bot: Bot, api: FakeMaxApi, clock, olga) -> None:
    game = await bot.create_game(olga, "Отдел продаж")
    await fill(bot, clock, game, range(201, 210))
    late = bot.person(250, "Опоздавший")
    await bot.join(late, game, wishes=None)
    participant = await repo.get_participant(bot.ctx.db, game.id, 250)
    assert participant is not None and participant.status == ParticipantStatus.WAITING
    await bot(late.press(texts.btn_pay(490)))

    payment = (await repo.payments_of_game(bot.ctx.db, game.id))[0]
    assert late.screen_text == texts.pay_offer(amount=490, title="Отдел продаж", limit=30)
    url = api.link_url(250, texts.btn_pay_link(490))
    parts = urlsplit(url)
    query = {key: values[0] for key, values in parse_qs(parts.query).items()}
    assert f"{parts.scheme}://{parts.netloc}{parts.path}" == robokassa.PAYMENT_URL
    assert query["OutSum"] == "490.00" and query["InvId"] == str(payment.inv_id) and query["IsTest"] == "1"
    assert query["Description"] == "Расширение игры Тайный Санта до 30 участников"
    signed = f"santa-shop:490.00:{payment.inv_id}:test-pass-1"
    assert query["SignatureValue"] == robokassa.sign([signed], "md5")


async def test_payment_return_payload_checks_and_shows_the_game(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    await fill(bot, clock, game, range(201, 210))
    late = bot.person(250, "Опоздавший")
    await bot.join(late, game, wishes=None)
    await bot(late.press(texts.btn_pay(490)))
    payment = (await repo.payments_of_game(bot.ctx.db, game.id))[0]
    await bot(late.start(f"p_{payment.inv_id}"))
    assert texts.PAY_CHECKING in late.texts and texts.PAYMENT_PENDING in late.texts
    assert late.screen_text.startswith(f"Игра «{game.title}» — идёт набор")

    stranger = bot.person(260, "Чужой")
    await bot.onboard(stranger, f"p_{payment.inv_id}")
    assert stranger.screen_text == texts.MENU


async def test_upgrade_with_nothing_to_pay_is_applied_at_once(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    await fill(bot, clock, game, range(201, 212))
    await billing.grant_tier(bot.ctx.db, game.id, Tier.S, 990, await bot.ctx.prices(), clock.now())
    await bot.forge(olga, f"{Action.PAY}:{game.code}:M")
    await bot.drain()
    assert olga.screen_text == texts.payment_received(title=game.title, limit=100)
    assert (await bot.game(game.id)).participant_limit == 100


# --- stale buttons ---------------------------------------------------------------------------------------------


async def test_cancelled_game_notifies_everyone_and_old_buttons_explain(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    ivan = bot.person(201, "Иван")
    await bot.join(ivan, game)
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_SETTINGS))
    await bot(olga.press(texts.BTN_CANCEL_GAME))
    await bot(olga.press(texts.BTN_CONFIRM_CANCEL_GAME))
    assert olga.screen_text == texts.game_cancelled_by_you(game.title)
    await bot.drain()
    assert ivan.screen_text == texts.game_cancelled(game.title)
    assert (await bot.game(game.id)).status == GameStatus.CANCELLED

    await bot(ivan.press(texts.BTN_EDIT_WISHES))
    assert ivan.screen_text == texts.GAME_CANCELLED
    await bot.forge(olga, f"{Action.CANCEL_GAME_CONFIRM}:{game.id}")
    assert olga.screen_text == texts.GAME_CANCELLED
    await bot.forge(olga, f"{Action.DRAW_CONFIRM}:{game.id}")
    assert olga.screen_text == texts.GAME_CANCELLED
    await bot(ivan.start(f"j_{game.code}"))
    assert ivan.screen_text.startswith(f"Игра «{game.title}» — отменена")


async def test_removed_participant_is_told_and_cannot_come_back(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    ivan = bot.person(201, "Иван")
    await bot.join(ivan, game)
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_PARTICIPANTS))
    assert "1. Ольга — пожелания нет" in olga.screen_text and "2. Иван — пожелания есть" in olga.screen_text
    await bot(olga.press(texts.BTN_REMOVE_PARTICIPANT))
    await bot(olga.press("Иван"))
    confirm = olga.press(texts.BTN_CONFIRM_REMOVE)
    await bot(confirm)
    assert olga.screen_text.startswith(texts.participant_removed("Иван"))
    await bot.forge(olga, f"{Action.REMOVE_CONFIRM}:{game.id}:201")
    assert olga.screen_text == texts.PARTICIPANT_GONE
    await bot.drain()
    assert ivan.screen_text == texts.removed_notice(game.title)

    await bot(ivan.press(texts.BTN_EDIT_WISHES))
    assert ivan.screen_text == texts.NOT_IN_GAME
    await bot(ivan.start(f"j_{game.code}"))
    assert ivan.screen_text == texts.REMOVED_CANNOT_JOIN


async def test_buttons_of_a_drawn_game(bot: Bot, api: FakeMaxApi, clock, olga) -> None:
    game = await bot.create_game(olga)
    ivan = bot.person(201, "Иван")
    await bot.join(ivan, game)
    await fill(bot, clock, game, [202])
    await games.run_draw(bot.ctx.db, game.id, ORGANIZER, now=clock.now(), rng=bot.ctx.rng)

    await bot(ivan.press(texts.BTN_LEAVE))
    assert ivan.screen_text == texts.confirm_leave_after_draw(game.title)
    await bot(ivan.press(texts.BTN_CANCEL))
    assert ivan.screen_text.startswith(f"Игра «{game.title}» — жеребьёвка проведена")
    await bot.forge(olga, f"{Action.EXCLUSION_SECOND}:{game.id}:201:202")
    assert olga.screen_text == texts.ALREADY_DRAWN_ACTION

    late = bot.person(300, "Поздний")
    await bot.onboard(late, f"j_{game.code}")
    assert late.screen_text == texts.ALREADY_DRAWN and late.button_texts == [texts.BTN_CREATE_GAME]

    await bot(ivan.press(texts.BTN_EDIT_WISHES))
    await bot(ivan.say("Теперь хочу шоколад"))
    assert ivan.screen_text == texts.WISHES_UPDATED
    await bot.forge(ivan, f"{Action.WHOM}:{game.id}")
    receiver_id = await repo.receiver_of(bot.ctx.db, game.id, 201)
    receiver = await repo.get_participant(bot.ctx.db, game.id, receiver_id)
    assert ivan.screen_text.startswith(f"Игра «{game.title}». Вы дарите: {receiver.display_name}.")


async def test_unknown_or_malformed_payloads_are_outdated(bot: Bot, olga) -> None:
    for payload in ("zzz", f"{Action.PANEL}", f"{Action.PANEL}:abc", f"{Action.WIZARD_DATE}:2026",
                    f"{Action.PAY}:NOPE22:S", f"{Action.OPEN_GAME}:99999"):
        await bot.forge(olga, payload)
        assert olga.screen_text == texts.BUTTON_OUTDATED, payload


# --- organizer, minimum, exclusions -------------------------------------------------------------------------------


async def test_organizer_who_does_not_participate(bot: Bot, api: FakeMaxApi, clock, olga) -> None:
    game = await bot.create_game(olga, participates=False)
    assert olga.screen_text == texts.GAME_CREATED_NOT_PARTICIPATING
    assert await repo.get_participant(bot.ctx.db, game.id, ORGANIZER) is None
    await fill(bot, clock, game, [201, 202, 203])
    await open_panel(bot, olga, game)
    assert "Участников: 3 из 10" in olga.screen_text and texts.BTN_MY_PARTICIPATION not in olga.button_texts
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    await bot.drain()
    assert not any(t.startswith("Жеребьёвка в игре") for t in olga.texts)
    assert olga.screen_text == "Готово! Пары отправлены 3 из 3."
    await bot(olga.start(f"j_{game.code}"))
    assert olga.screen_text.startswith(f"Игра «{game.title}» · код {game.code}")


async def test_draw_needs_three_participants(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    await fill(bot, clock, game, [201])
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_DRAW))
    assert olga.screen_text == texts.DRAW_NEEDS_THREE
    await bot.forge(olga, f"{Action.DRAW_CONFIRM}:{game.id}")
    assert olga.screen_text == texts.DRAW_NEEDS_THREE
    assert (await bot.game(game.id)).status == GameStatus.COLLECTING


async def test_exclusions_that_make_the_draw_impossible(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    await fill(bot, clock, game, [201, 202])
    await bot.forge(olga, f"{Action.EXCLUSION_SECOND}:{game.id}:201:202")
    await bot.forge(olga, f"{Action.EXCLUSION_SECOND}:{game.id}:202:201")
    assert texts.EXCLUSION_EXISTS in olga.screen_text
    await bot.forge(olga, f"{Action.EXCLUSION_SECOND}:{game.id}:201:201")
    assert olga.screen_text == texts.EXCLUSION_SAME_PERSON
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    assert olga.screen_text == texts.DRAW_IMPOSSIBLE and texts.BTN_EXCLUSIONS in olga.button_texts
    assert (await bot.game(game.id)).status == GameStatus.COLLECTING

    await bot(olga.press(texts.BTN_EXCLUSIONS))
    await bot(olga.press(texts.btn_remove_pair(1)))
    assert olga.screen_text.startswith(texts.EXCLUSION_REMOVED)
    assert await repo.exclusions(bot.ctx.db, game.id) == []


async def test_pickers_are_paginated_by_eight(bot: Bot, clock, olga) -> None:
    await repo.set_setting(bot.ctx.db, "free_limit", 20)
    game = await bot.create_game(olga)
    await fill(bot, clock, game, range(201, 212))
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_PARTICIPANTS))
    await bot(olga.press(texts.BTN_REMOVE_PARTICIPANT))
    first_page = olga.button_texts
    assert first_page[:8] == [f"Гость {i}" for i in range(201, 209)]
    assert texts.BTN_NEXT_PAGE in first_page and texts.BTN_PREVIOUS_PAGE in first_page  # 'Назад' is also 'back'
    await bot(olga.press(texts.BTN_NEXT_PAGE))
    second_page = olga.button_texts
    assert second_page[:3] == ["Гость 209", "Гость 210", "Гость 211"] and texts.BTN_NEXT_PAGE not in second_page

    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_EXCLUSIONS))
    await bot(olga.press(texts.BTN_ADD_PAIR))
    assert olga.button_texts[:8] == ["Ольга", *[f"Гость {i}" for i in range(201, 208)]]


# --- input limits ----------------------------------------------------------------------------------------------------


async def test_length_limits_are_enforced(bot: Bot, olga) -> None:
    await bot(olga.press(texts.BTN_CREATE_GAME))
    await bot(olga.say("Т" * 61))
    assert olga.screen_text == texts.title_too_long(60)
    await bot(olga.say("Т" * 60))
    await bot(olga.press(texts.BTN_CUSTOM_BUDGET))
    await bot(olga.say("б" * 31))
    assert olga.screen_text == texts.budget_too_long(30)
    await bot(olga.say("до 700 ₽"))
    await bot(olga.say("31.02"))
    assert olga.screen_text == texts.DATE_ERRORS[DateError.FORMAT]
    await bot(olga.say("25.12"))
    await bot(olga.say("что-то"))
    assert olga.screen_text == texts.ASK_PARTICIPATES
    await bot(olga.press(texts.BTN_I_PARTICIPATE))
    game = (await repo.games_of_user(bot.ctx.db, ORGANIZER))[0][0]
    assert (game.title, game.budget_text, str(game.exchange_date)) == ("Т" * 60, "до 700 ₽", "2026-12-25")

    await bot(olga.say("п" * 1001))
    assert olga.screen_text == texts.wishes_too_long(1000)
    await bot(olga.say("п" * 1000))
    assert olga.screen_text == texts.WISHES_SAVED

    await bot.forge(olga, f"{Action.CHANGE_NAME}:{game.id}")
    await bot(olga.say("И" * 41))
    assert olga.screen_text == texts.name_too_long(40)
    await bot(olga.say("  Ольга​  Иванова "))
    assert olga.screen_text == texts.name_saved("Ольга Иванова")


async def test_relay_length_limit_and_text_only(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    await fill(bot, clock, game, [201, 202])
    await games.run_draw(bot.ctx.db, game.id, ORGANIZER, now=clock.now(), rng=bot.ctx.rng)
    await bot.forge(olga, f"{Action.ASK_RECEIVER}:{game.id}")
    await bot(olga.say("x" * 501))
    assert olga.screen_text == texts.relay_too_long(500)
    await bot(message_created(ORGANIZER, "", name="Ольга", attachments=[{"type": "image"}]))
    assert olga.screen_text == texts.TEXT_ONLY
    await bot(olga.say("x" * 500))
    assert olga.screen_text == texts.RELAY_SENT_ANONYMOUSLY


# --- other rules ---------------------------------------------------------------------------------------------------------


async def test_maintenance_refuses_new_games_and_joins(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    await repo.set_setting(bot.ctx.db, "maintenance", True)
    await bot(olga.press(texts.BTN_CREATE_GAME))
    assert olga.screen_text == texts.MAINTENANCE
    newcomer = bot.person(201, "Новичок")
    await bot.onboard(newcomer, f"j_{game.code}")
    assert newcomer.screen_text == texts.MAINTENANCE
    assert await repo.get_participant(bot.ctx.db, game.id, 201) is None


async def test_blocked_user_cannot_create_a_game(bot: Bot, olga) -> None:
    await repo.set_blocked(bot.ctx.db, ORGANIZER, True)
    await bot(olga.press(texts.BTN_CREATE_GAME))
    assert olga.screen_text == texts.blocked(bot.ctx.config.support_email)


async def test_group_chat_messages_are_ignored(bot: Bot, api: FakeMaxApi) -> None:
    await bot(message_created(700, "abc234", chat_id=-5, chat_type="chat"))
    assert api.sent == [] and await repo.get_user(bot.ctx.db, 700) is None


async def test_wish_reminder_goes_to_people_without_wishes_once_a_day(bot: Bot, api: FakeMaxApi, clock, olga) -> None:
    game = await bot.create_game(olga)
    ivan = bot.person(201, "Иван")
    await bot.join(ivan, game)
    await fill(bot, clock, game, [202, 203])
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_REMIND_WISHES))
    assert olga.screen_text == texts.reminded(3)
    await bot.drain()
    assert api.last_text(202) == texts.wish_reminder(game.title)
    assert texts.wish_reminder(game.title) not in api.texts_to(201)
    await bot(olga.press(texts.BTN_REMIND_WISHES))
    assert olga.screen_text == texts.REMINDER_TOO_SOON

    await bot(bot.person(202, "Гость 202").press(texts.BTN_SURPRISE_ME))
    assert (await repo.get_participant(bot.ctx.db, game.id, 202)).wishes == texts.SURPRISE_WISHES


async def test_invite_is_resent_with_live_progress(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga, "Отдел продаж")
    invite = olga.texts[-2]
    assert invite.splitlines()[0] == "Тайный Санта «Отдел продаж»" and f"start=j_{game.code}" in invite
    await fill(bot, clock, game, [201])
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_INVITE))
    assert "Уже участвуют: 2 — Ольга, Гость 201" in olga.screen_text


async def test_leaving_frees_a_place_for_the_queue(bot: Bot, api: FakeMaxApi, clock, olga) -> None:
    game = await bot.create_game(olga)
    ivan = bot.person(201, "Иван")
    await bot.join(ivan, game)
    await fill(bot, clock, game, range(202, 211))
    waiting = await repo.get_participant(bot.ctx.db, game.id, 210)
    assert waiting is not None and waiting.status == ParticipantStatus.WAITING
    await bot(ivan.press(texts.BTN_LEAVE))
    await bot(ivan.press(texts.BTN_CONFIRM_LEAVE))
    assert ivan.screen_text == texts.left_game(game.title)
    await bot.drain()
    assert api.last_text(210) == texts.spot_opened(game.title)
    assert (await repo.get_participant(bot.ctx.db, game.id, 210)).status == ParticipantStatus.ACTIVE


async def test_settings_toggles_and_text_edits(bot: Bot, clock, olga) -> None:
    game = await bot.create_game(olga)
    await open_panel(bot, olga, game)
    await bot(olga.press(texts.BTN_SETTINGS))
    await bot(olga.press(texts.btn_anon_chat(True)))
    assert texts.btn_anon_chat(False) in olga.button_texts
    await bot(olga.press(texts.BTN_SET_TITLE))
    await bot(olga.say("Новая семья"))
    assert olga.screen_text.startswith(texts.SETTINGS_SAVED)
    await bot(olga.press(texts.BTN_SET_DATE))
    await bot(olga.press("20.12"))
    await bot(olga.press(texts.btn_participation(True)))
    updated = await bot.game(game.id)
    assert (updated.title, updated.anon_chat, str(updated.exchange_date)) == ("Новая семья", False, "2026-12-20")
    assert not updated.organizer_participates
    assert (await repo.get_participant(bot.ctx.db, game.id, ORGANIZER)).status == ParticipantStatus.LEFT


async def test_my_games_and_cancel_command(bot: Bot, clock, olga) -> None:
    await bot.create_game(olga, "Первая")
    await bot(olga.say("/cancel"))
    assert olga.screen_text == texts.INPUT_CANCELLED
    await bot(olga.say("/cancel"))
    assert olga.screen_text == texts.NOTHING_TO_CANCEL
    await bot(olga.press(texts.BTN_MY_GAMES))
    assert olga.screen_text == f"{texts.MY_GAMES_HEADER}\nПервая — идёт набор, организатор"
    await bot(olga.say("просто текст"))
    assert olga.screen_text == texts.UNKNOWN_INPUT
    await bot(olga.say("/help"))
    assert olga.screen_text.startswith("Как это работает:")
    state = await repo.get_state(bot.ctx.db, ORGANIZER, clock.now())
    assert state is None or state.kind != StateKind.WISHES
