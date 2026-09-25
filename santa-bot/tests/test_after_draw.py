"""P1 after the draw (§5.5, §5.11): redraw, [Подарок готов], leaving or removal with a spliced cycle, reveal."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from app import repo
from app.core import texts
from app.core.analytics import build_stats_report
from app.core.clock import FakeClock
from app.core.models import Game, ParticipantStatus
from app.handlers.views import Action
from tests.bot import Bot
from tools.fake_max import FakeMaxApi, FakeUser

ORGANIZER = 100
NAMES = {ORGANIZER: "Ольга", 201: "Иван", 202: "Мария", 203: "Пётр", 204: "Анна"}


def results(api: FakeMaxApi, user_id: int) -> list[str]:
    return [t for t in api.texts_to(user_id) if "Вы — Тайный Санта для:" in t]


@pytest.fixture
async def party(bot: Bot) -> tuple[dict[int, FakeUser], Game]:
    """Ольга and four guests; the pairs are drawn but not delivered yet (call ``bot.drain``)."""
    people = {user_id: bot.person(user_id, name) for user_id, name in NAMES.items()}
    await bot.onboard(people[ORGANIZER])
    game = await bot.create_game(people[ORGANIZER], "Семья")
    await bot(people[ORGANIZER].say("Шарф"))
    for user_id in (201, 202, 203, 204):
        await bot.join(people[user_id], game, wishes=f"Подарок для {NAMES[user_id]}")
    await draw(bot, people[ORGANIZER])
    return people, await bot.game(game.id)


async def draw(bot: Bot, olga: FakeUser) -> None:
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))


async def redraw(bot: Bot, olga: FakeUser) -> None:
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_REDRAW))
    await bot(olga.press(texts.BTN_CONFIRM_REDRAW))


async def test_redraw_replaces_undelivered_pairs_twice_at_most(bot: Bot, api: FakeMaxApi, party) -> None:
    people, game = party
    olga = people[ORGANIZER]
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_REDRAW))
    assert olga.screen_text.endswith("Перезапустить можно ещё 2 раза.")
    await bot(olga.press(texts.BTN_CONFIRM_REDRAW))
    assert olga.last_text == texts.REDRAW_STARTED
    await bot.drain()
    for user_id in NAMES:
        (result,) = results(api, user_id)
        assert result.startswith(texts.REDRAW_PREFIX), "the old pair never arrives after the new one"
    assert api.last_text(ORGANIZER) == "Готово! Пары отправлены 5 из 5."

    await redraw(bot, olga)
    await bot.drain()
    assert all(len(results(api, user_id)) == 2 for user_id in NAMES)
    assert (await bot.game(game.id)).redraw_count == 2
    await bot(olga.press(texts.BTN_PANEL))
    assert texts.BTN_REDRAW not in olga.button_texts
    await bot.forge(olga, f"{Action.REDRAW}:{game.id}")
    assert olga.last_text == texts.REDRAW_LIMIT

    report = await build_stats_report(bot.ctx.db, bot.ctx.clock.now(), bot.ctx.config.tz)
    assert report.blocks[0][1].draws == 1, "redraws are not new draws"


async def test_replies_to_messages_from_before_a_redraw_are_refused(bot: Bot, api: FakeMaxApi, party) -> None:
    people, game = party
    await bot.drain()
    before = await repo.assignments(bot.ctx.db, game.id)
    for santa in NAMES:
        await bot(people[santa].press(texts.BTN_ASK_RECEIVER))
        await bot(people[santa].say("Какой цвет любишь?"))
    await redraw(bot, people[ORGANIZER])
    after = await repo.assignments(bot.ctx.db, game.id)
    santa = next(giver for giver in NAMES if before[giver] != after[giver])
    receiver = people[before[santa]]
    await bot.drain()

    message, _ = api.find_button(receiver.user_id, texts.BTN_REPLY_TO_SANTA)
    reply = next(b for b in message.buttons if b.text == texts.BTN_REPLY_TO_SANTA)
    await bot.forge(receiver, reply.payload)  # type: ignore[union-attr]
    assert receiver.last_text == texts.RELAY_PAIR_CHANGED


async def test_gift_ready_is_counted_on_the_panel(bot: Bot, api: FakeMaxApi, party) -> None:
    people, game = party
    await bot.drain()
    ivan = people[201]
    await bot(ivan.press(texts.BTN_GIFT_READY))
    assert ivan.last_text == texts.GIFT_READY_SAVED
    assert (await repo.get_participant(bot.ctx.db, game.id, 201)).gift_ready  # type: ignore[union-attr]
    await bot(people[ORGANIZER].press(texts.BTN_PANEL))
    assert "Подарки готовы: 1 из 5." in people[ORGANIZER].screen_text

    await bot.forge(ivan, f"{Action.MY_PARTICIPATION}:{game.id}")
    await bot.forge(ivan, f"{Action.OPEN_GAME}:{game.id}")
    assert texts.BTN_GIFT_READY not in [b.text for b in api.screen(201).buttons]


async def test_leaving_after_the_draw_hands_the_receiver_to_the_santa(bot: Bot, api: FakeMaxApi, party) -> None:
    people, game = party
    await bot.drain()
    pairs = await repo.assignments(bot.ctx.db, game.id)
    leaver = next(user_id for user_id in (201, 202, 203, 204) if pairs[user_id] != ORGANIZER)
    santa = next(giver for giver, receiver in pairs.items() if receiver == leaver)
    new_receiver = pairs[leaver]

    await bot.forge(people[leaver], f"{Action.OPEN_GAME}:{game.id}")
    await bot(people[leaver].press(texts.BTN_LEAVE))
    assert people[leaver].screen_text == texts.confirm_leave_after_draw("Семья")
    await bot(people[leaver].press(texts.BTN_CONFIRM_LEAVE))
    await bot.drain()

    assert api.last_text(santa) == texts.receiver_left(receiver=NAMES[new_receiver],
                                                       wishes=f"Подарок для {NAMES[new_receiver]}"
                                                       if new_receiver != ORGANIZER else "Шарф")
    after = await repo.assignments(bot.ctx.db, game.id)
    assert after[santa] == new_receiver and leaver not in after and leaver not in after.values()
    assert not any(t.startswith(texts.splice_needs_redraw("Семья")) for t in api.texts_to(ORGANIZER))


async def test_removal_down_to_two_asks_the_organizer_to_redraw(bot: Bot, api: FakeMaxApi, party) -> None:
    people, game = party
    await bot.drain()
    olga = people[ORGANIZER]
    for user_id in (201, 202, 203):
        await bot(olga.press(texts.BTN_PANEL))
        await bot(olga.press(texts.BTN_PARTICIPANTS))
        await bot(olga.press(texts.BTN_REMOVE_PARTICIPANT))
        await bot(olga.press(NAMES[user_id]))
        assert olga.screen_text == texts.confirm_remove_after_draw(NAMES[user_id])
        await bot(olga.press(texts.BTN_CONFIRM_REMOVE))
    await bot.drain()
    left = await repo.participants(bot.ctx.db, game.id, ParticipantStatus.ACTIVE)
    assert [p.user_id for p in left] == [ORGANIZER, 204]
    assert texts.splice_needs_redraw("Семья") in api.texts_to(ORGANIZER)
    assert api.last_text(201) == texts.removed_notice("Семья")


async def test_reveal_after_the_exchange_date(bot: Bot, api: FakeMaxApi, clock: FakeClock, party) -> None:
    people, game = party
    await bot.drain()
    olga = people[ORGANIZER]
    await repo.update_game(bot.ctx.db, game.id, exchange_date=date(2026, 11, 25))
    await bot(olga.press(texts.BTN_PANEL))
    assert texts.BTN_REVEAL not in olga.button_texts
    await bot.forge(olga, f"{Action.REVEAL}:{game.id}")
    assert olga.last_text == texts.REVEAL_TOO_EARLY

    clock.advance((datetime(2026, 11, 25, 18, tzinfo=timezone.utc) - clock.now()).total_seconds())
    await bot.forge(olga, f"{Action.PANEL}:{game.id}")
    await bot(olga.press(texts.BTN_REVEAL))
    assert olga.screen_text == texts.confirm_reveal("Семья")
    await bot(olga.press(texts.BTN_CONFIRM_REVEAL))
    await bot.drain()

    pairs = await repo.assignments(bot.ctx.db, game.id)
    giver, lines = ORGANIZER, []
    for _ in NAMES:
        lines.append(f"{NAMES[giver]} → {NAMES[pairs[giver]]}")
        giver = pairs[giver]
    expected = "Кто чей Санта в игре «Семья»:\n" + "\n".join(lines)
    for user_id in NAMES:
        chain = [t for t in api.texts_to(user_id) if t.startswith("Кто чей Санта")]
        assert chain == [expected]
    assert texts.BTN_NEW_GAME_ELSEWHERE in api.button_texts(201)
    await bot.forge(olga, f"{Action.REVEAL_CONFIRM}:{game.id}")
    assert olga.last_text == texts.REVEAL_ALREADY_DONE
