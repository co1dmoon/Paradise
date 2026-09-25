"""§14 scenario 10 (P1): bot_added → gc_ flow → the group card is posted, then edited on join and after the draw."""

from __future__ import annotations

from app import repo
from app.core import texts
from app.core.clock import FakeClock
from app.core.kb import LinkButton
from app.core.models import Game
from app.max_api import ChatInfo
from tests.bot import Bot
from tools import fake_max
from tools.fake_max import FakeMaxApi, FakeUser, SentMessage

CHAT = -700123
ORGANIZER = 100


def card(api: FakeMaxApi) -> SentMessage:
    return api.messages_in_chat(CHAT)[-1]


async def group_game(bot: Bot, api: FakeMaxApi) -> tuple[FakeUser, Game]:
    api.chats[CHAT] = ChatInfo(CHAT, "chat", "Отдел продаж", "active")
    olga = bot.person(ORGANIZER, "Ольга")
    await bot(fake_max.bot_added(CHAT, ORGANIZER, name="Ольга"))
    hello = card(api)
    assert hello.text == texts.GROUP_HELLO
    (button,) = hello.buttons
    assert isinstance(button, LinkButton) and button.url == "https://max.ru/santa_test_bot?start=gcm700123"

    await bot.onboard(olga, "gcm700123")
    assert olga.screen_text == texts.ASK_TITLE
    await bot(olga.say("Отдел продаж"))
    await bot(olga.press(texts.BUDGET_PRESETS[1]))
    await bot(olga.press(texts.BTN_DATE_UNKNOWN))
    await bot(olga.press(texts.BTN_I_PARTICIPATE))
    game = (await repo.games_of_user(bot.ctx.db, ORGANIZER))[0][0]
    return olga, game


async def test_group_card_follows_the_game(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    olga, game = await group_game(bot, api)
    assert game.group_chat_id == CHAT and game.group_card_mid == card(api).mid
    assert olga.last_text == texts.game_created_in_group(participates=True)
    assert not any(text.startswith("Тайный Санта «") for text in olga.texts), "no invite to forward"
    assert card(api).text == texts.group_card(title="Отдел продаж", budget="до 1000 ₽", exchange_date=None,
                                              names=["Ольга"], limit=10)
    (join,) = card(api).buttons
    assert isinstance(join, LinkButton) and join.url.endswith(f"?start=j_{game.code}")
    await bot(olga.say("Чай"))

    clock.advance(10)
    await bot.join(bot.person(201, "Иван"), game)
    assert "Участвуют: 2 из 10: Ольга, Иван" in card(api).text and len(card(api).edits) == 1

    await bot.join(bot.person(202, "Мария"), game)
    await bot.ctx.wait_background()
    assert "Участвуют: 3 из 10: Ольга, Иван, Мария" in card(api).text and len(card(api).edits) == 2
    edits = [call.at for call in api.calls if call.kind == "edit" and call.target_key == f"chat:{CHAT}"]
    assert len(edits) == 2 and edits[1] - edits[0] >= 10, "at most one edit per 10 s, the last change kept"

    clock.advance(10)
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    await bot.drain()
    assert card(api).text == texts.GROUP_CARD_DRAWN and card(api).buttons == []
    assert len(api.messages_in_chat(CHAT)) == 2, "the hello and one card, edited in place"


async def test_a_deleted_card_is_posted_again(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    _, game = await group_game(bot, api)
    await repo.update_game(bot.ctx.db, game.id, group_card_mid="mid.deleted")
    clock.advance(10)
    await bot.join(bot.person(201, "Иван"), game)
    assert len(api.messages_in_chat(CHAT)) == 3
    assert "Ольга, Иван" in card(api).text
    assert (await bot.game(game.id)).group_card_mid == card(api).mid


async def test_reveal_goes_to_the_group(bot: Bot, api: FakeMaxApi, clock: FakeClock) -> None:
    olga, game = await group_game(bot, api)
    for user_id, name in ((201, "Иван"), (202, "Мария")):
        await bot.join(bot.person(user_id, name), game)
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    await bot.drain()
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_REVEAL))
    await bot(olga.press(texts.BTN_CONFIRM_REVEAL))
    await bot.drain()
    assert olga.last_text == texts.REVEAL_SENT_TO_GROUP
    chain = card(api).text
    assert chain.startswith("Кто чей Санта в игре «Отдел продаж»:\nОльга → ")
    assert chain.endswith(f"Провести такую же игру в другом чате: https://max.ru/santa_test_bot?start=n_{game.code}")
    assert not any(text.startswith("Кто чей Санта") for text in api.texts_to(201)), "only the chat gets it"


async def test_group_of_a_missing_chat_and_removal(bot: Bot, api: FakeMaxApi) -> None:
    anna = bot.person(300, "Анна")
    await bot.onboard(anna, "gcm999")
    assert anna.last_text == texts.GROUP_NOT_FOUND

    _, game = await group_game(bot, api)
    await bot(fake_max.bot_removed(CHAT, ORGANIZER))
    detached = await bot.game(game.id)
    assert (detached.group_chat_id, detached.group_card_mid) == (None, None)
    await bot(fake_max.bot_added(-5, 300, is_channel=True))
    assert api.messages_in_chat(-5) == [], "channels are ignored"
    events = await bot.ctx.db.fetchval("SELECT COUNT(*) FROM events WHERE type = 'group_added'")
    assert events == 1
