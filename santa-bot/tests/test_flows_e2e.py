"""§14 end-to-end scenarios 1, 2, 4, 5, 6 and the ref part of 9: webhook update → handlers → FakeMaxApi."""

from __future__ import annotations

import pytest

from app import repo
from app.core import texts
from app.core.analytics import build_stats_report
from app.core.models import GameStatus, JoinVia, ParticipantStatus, StateKind
from app.handlers.views import Action
from tests.bot import ADMIN_ID, Bot
from tools.fake_max import FakeMaxApi


async def organizer_with_game(bot: Bot, *, participates: bool = True):
    olga = bot.person(100, "Ольга Иванова")
    await bot.onboard(olga)
    return olga, await bot.create_game(olga, participates=participates)


# --- 1. consent gate --------------------------------------------------------------------------------


async def test_consent_gate_stores_only_id_and_source_then_resumes_the_payload(bot: Bot, api: FakeMaxApi) -> None:
    _, game = await organizer_with_game(bot)
    petr = bot.person(201, "Пётр Сидоров")
    await bot(petr.start(f"j_{game.code}"))

    assert petr.screen_text == texts.consent(bot.ctx.config.public_base_url)
    user = await repo.get_user(bot.ctx.db, 201)
    assert user is not None and (user.first_source, user.max_name, user.username) == (f"j:{game.code}", None, None)
    assert user.consent_at is None and user.consent_version is None
    assert await repo.get_participant(bot.ctx.db, game.id, 201) is None

    await bot(petr.say("привет"))
    await bot.forge(petr, f"{Action.MY_GAMES}")
    assert petr.screen_text == texts.consent(bot.ctx.config.public_base_url)
    await bot(petr.say("/whoami"))
    assert petr.screen_text == texts.whoami(201)

    await bot(petr.press(texts.BTN_CONSENT))
    user = await repo.get_user(bot.ctx.db, 201)
    assert user is not None and user.consent_version == "2026-10" and user.max_name == "Пётр Сидоров"
    assert any(t.startswith(f"Вы в игре «{game.title}»! Организатор: Ольга Иванова") for t in petr.texts)
    assert petr.screen_text == texts.ASK_WISHES
    participant = await repo.get_participant(bot.ctx.db, game.id, 201)
    assert participant is not None and participant.via == JoinVia.LINK


async def test_buttons_before_consent_show_the_consent_screen(bot: Bot) -> None:
    anna = bot.person(202, "Анна")
    await bot(anna.start())
    await bot.forge(anna, f"{Action.CREATE}")
    assert anna.screen_text.startswith("Привет! Я помогу провести")
    assert await repo.games_of_user(bot.ctx.db, 202) == []


# --- 2. family game in link mode ----------------------------------------------------------------------


async def test_family_game_in_link_mode(bot: Bot, api: FakeMaxApi) -> None:
    olga, game = await organizer_with_game(bot)
    await bot(olga.say("Планшет для рисования"))
    assert olga.screen_text.startswith("Записал!")

    family = [bot.person(301 + i, name) for i, name in enumerate(
        ["Иван", "Мария", "Пётр", "Анна", "Сергей", "Елена", "Дмитрий", "Наталья"])]
    for number, person in enumerate(family):
        await bot.onboard(person, f"j_{game.code}")
        if number % 2 == 0:
            await bot(person.press(texts.BTN_CHANGE_NAME))
            await bot(person.say(f"{person.name}, 5Б"))
            assert f"Записал: «{person.name}, 5Б»." in person.texts
        await bot(person.say(f"Пожелания {person.name}: книга"))
        assert person.screen_text == texts.WISHES_SAVED

    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_EXCLUSIONS))
    await bot(olga.press(texts.BTN_ADD_PAIR))
    await bot(olga.press("Иван, 5Б"))
    await bot(olga.press("Мария"))
    assert "Записал: Иван, 5Б и Мария не будут дарить друг другу." in olga.screen_text
    assert await repo.exclusions(bot.ctx.db, game.id) == [(301, 302)]

    await bot(olga.press(texts.BTN_PANEL))
    panel = olga.screen_text
    assert "Участников: 9 из 10" in panel and "Без пожеланий: 0 · Исключений: 1" in panel
    assert "очеред" not in panel

    await bot(olga.press(texts.BTN_DRAW))
    assert olga.screen_text.startswith("Провести жеребьёвку для 9 участников?")
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    assert olga.screen_text == texts.DRAW_STARTED
    await bot.drain()

    pairs = await repo.assignments(bot.ctx.db, game.id)
    assert len(pairs) == 9 and pairs[301] != 302 and pairs[302] != 301
    people = {p.user_id: p for p in await repo.participants(bot.ctx.db, game.id)}
    for giver_id, receiver_id in pairs.items():
        results = [t for t in api.texts_to(giver_id) if t.startswith("Жеребьёвка в игре")]
        assert len(results) == 1
        receiver = people[receiver_id]
        assert f"Вы — Тайный Санта для: {receiver.display_name}." in results[0]
        assert f"Пожелания: {receiver.wishes}" in results[0]
    assert olga.screen_text == "Готово! Пары отправлены 9 из 9."
    assert (await bot.game(game.id)).status == GameStatus.DRAWN


# --- 4. code fallback ---------------------------------------------------------------------------------------


async def test_returning_user_joins_by_typed_code(bot: Bot) -> None:
    _, game = await organizer_with_game(bot)
    kate = bot.person(401, "Катя")
    await bot.onboard(kate)
    await bot(kate.say(f"код {game.code.lower()}"))
    participant = await repo.get_participant(bot.ctx.db, game.id, 401)
    assert participant is not None and participant.via == JoinVia.CODE
    assert kate.screen_text == texts.ASK_WISHES

    misha = bot.person(402, "Миша")
    await bot.onboard(misha)
    await bot(misha.say(f"  {game.code}  "))
    assert (await repo.get_participant(bot.ctx.db, game.id, 402)) is not None
    await bot(misha.say("код ZZZZZZ"))
    assert misha.screen_text == texts.GAME_NOT_FOUND

    lena = bot.person(403, "Лена")
    await bot.onboard(lena)
    await bot(lena.press(texts.BTN_JOIN_BY_CODE))
    await bot(lena.say("ZZZZZZ"))
    assert lena.screen_text == texts.GAME_NOT_FOUND
    await bot(lena.say(game.code))
    assert (await repo.get_participant(bot.ctx.db, game.id, 403)) is not None


# --- 5. anonymous messages ------------------------------------------------------------------------------------


@pytest.fixture
async def drawn(bot: Bot):
    olga, game = await organizer_with_game(bot)
    await bot(olga.say("Шарф"))
    people = [bot.person(501 + i, name) for i, name in enumerate(["Иван Петров", "Мария Кузнецова", "Пётр Орлов"])]
    for person in people:
        await bot.join(person, game)
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    await bot.drain()
    return olga, game, {p.user_id: p for p in [olga, *people]}


async def test_relay_is_anonymous_routes_replies_and_reports(bot: Bot, api: FakeMaxApi, drawn) -> None:
    _, game, people = drawn
    pairs = await repo.assignments(bot.ctx.db, game.id)
    santa = people[501]
    receiver = people[pairs[501]]

    await bot(santa.press(texts.BTN_ASK_RECEIVER))
    receiver_row = await repo.get_participant(bot.ctx.db, game.id, receiver.user_id)
    assert santa.screen_text == texts.relay_prompt_to_receiver(receiver_row.display_name)
    await bot(santa.say("Какой у тебя размер?"))
    assert santa.screen_text == texts.RELAY_SENT_ANONYMOUSLY
    await bot.drain()
    assert receiver.screen_text == f"Сообщение от вашего Тайного Санты (игра «{game.title}»): «Какой у тебя размер?»"
    delivered = api.last_to(receiver.user_id)
    assert delivered.disable_link_preview

    await bot(receiver.press(texts.BTN_REPLY_TO_SANTA))
    await bot(receiver.say("M, спасибо!"))
    assert receiver.screen_text == texts.RELAY_SENT_TO_SANTA
    await bot.drain()
    assert santa.screen_text == f"Сообщение от {receiver_row.display_name} — человека, которому вы дарите: «M, спасибо!»"

    await bot(santa.press(texts.BTN_REPLY_ANONYMOUSLY))
    await bot(santa.say("Понял"))
    await bot.drain()
    assert receiver.screen_text.endswith("«Понял»")

    for message in api.messages_to(receiver.user_id):
        assert "Иван Петров" not in message.text and "501" not in message.text
        assert all("501" not in getattr(b, "payload", "") for b in message.buttons)

    await bot(receiver.press(texts.BTN_REPORT))
    assert receiver.screen_text == texts.REPORT_SENT
    await bot(receiver.press(texts.BTN_REPORT))
    assert receiver.screen_text == texts.REPORT_SENT
    await bot.drain()
    reports = [t for t in api.texts_to(ADMIN_ID) if t.startswith("Жалоба №")]
    assert len(reports) == 1 and f"игра {game.code}" in reports[0] and "отправитель: 501" in reports[0]

    admin = bot.person(ADMIN_ID, "Админ")
    await bot(admin.press(texts.BTN_BLOCK_SENDER))
    assert admin.screen_text == texts.user_blocked(501)
    assert (await repo.get_user(bot.ctx.db, 501)).blocked

    await bot(santa.press(texts.BTN_ASK_RECEIVER))
    assert santa.screen_text == texts.blocked(bot.ctx.config.support_email)
    assert (await repo.get_state(bot.ctx.db, 501, bot.ctx.clock.now())) is None


async def test_blocked_user_cannot_finish_a_pending_relay(bot: Bot, drawn) -> None:
    _, game, people = drawn
    santa = people[502]
    await bot(santa.press(texts.BTN_WRITE_SANTA))
    await repo.set_blocked(bot.ctx.db, 502, True)
    await bot(santa.say("Привет"))
    assert santa.screen_text == texts.blocked(bot.ctx.config.support_email)


async def test_only_the_recipient_may_reply_or_report(bot: Bot, drawn) -> None:
    _, game, people = drawn
    pairs = await repo.assignments(bot.ctx.db, game.id)
    santa, receiver = people[503], people[pairs[503]]
    await bot(santa.press(texts.BTN_ASK_RECEIVER))
    await bot(santa.say("Вопрос"))
    relay_id = int(await bot.ctx.db.fetchval("SELECT MAX(id) FROM relay_messages"))
    await bot.forge(santa, f"{Action.REPLY}:{relay_id}")
    assert santa.screen_text == texts.NOT_ALLOWED
    await bot.forge(santa, f"{Action.REPORT}:{relay_id}")
    assert santa.screen_text == texts.NOT_ALLOWED
    assert receiver.user_id != santa.user_id


# --- 6. organizer-only actions ------------------------------------------------------------------------------------


async def test_non_organizer_cannot_draw_or_remove(bot: Bot) -> None:
    _, game = await organizer_with_game(bot)
    ivan, maria = bot.person(601, "Иван"), bot.person(602, "Мария")
    await bot.join(ivan, game)
    await bot.join(maria, game)

    for payload in (f"{Action.DRAW}:{game.id}", f"{Action.DRAW_CONFIRM}:{game.id}",
                    f"{Action.REMOVE_CONFIRM}:{game.id}:602", f"{Action.PANEL}:{game.id}",
                    f"{Action.CANCEL_GAME_CONFIRM}:{game.id}"):
        await bot.forge(ivan, payload)
        assert ivan.screen_text == texts.NOT_ALLOWED, payload

    assert (await bot.game(game.id)).status == GameStatus.COLLECTING
    maria_row = await repo.get_participant(bot.ctx.db, game.id, 602)
    assert maria_row is not None and maria_row.status == ParticipantStatus.ACTIVE
    assert await repo.assignments(bot.ctx.db, game.id) == {}


# --- 9. the ref button ------------------------------------------------------------------------------------------------


async def test_ref_button_creates_a_referred_game_counted_in_stats(bot: Bot) -> None:
    _, game = await organizer_with_game(bot)
    ivan = bot.person(901, "Иван")
    await bot.join(ivan, game)
    assert texts.BTN_NEW_GAME_ELSEWHERE in ivan.button_texts

    await bot(ivan.press(texts.BTN_NEW_GAME_ELSEWHERE))
    assert ivan.screen_text == texts.ASK_TITLE
    await bot(ivan.press(texts.BTN_SKIP))
    await bot(ivan.press(texts.BUDGET_PRESETS[1]))
    await bot(ivan.press(texts.BTN_DATE_UNKNOWN))
    await bot(ivan.press(texts.BTN_ONLY_ORGANIZE))
    ref_game = (await repo.games_of_user(bot.ctx.db, 901))[0][0]
    assert ref_game.id != game.id and ref_game.source_game_id == game.id and ref_game.source == "ref"
    assert ref_game.title == texts.DEFAULT_TITLE and ref_game.budget_text == texts.BUDGET_PRESETS[1]

    report = await build_stats_report(bot.ctx.db, bot.ctx.clock.now(), bot.ctx.config.tz)
    today = report.blocks[0][1]
    assert today.ref_games == 1 and today.games_created == 2
    ref_clicks = await bot.ctx.db.fetchval("SELECT COUNT(*) FROM events WHERE type = 'ref_click'")
    assert ref_clicks == 1


async def test_n_payload_starts_a_referred_game_after_consent(bot: Bot) -> None:
    _, game = await organizer_with_game(bot)
    newcomer = bot.person(902, "Новичок")
    await bot.onboard(newcomer, f"n_{game.code}")
    user = await repo.get_user(bot.ctx.db, 902)
    assert user is not None and user.first_source == f"n:{game.code}"
    assert newcomer.screen_text == texts.ASK_TITLE
    state = await repo.get_state(bot.ctx.db, 902, bot.ctx.clock.now())
    assert state is not None and state.kind == StateKind.TITLE and state.data["source_game_id"] == game.id
