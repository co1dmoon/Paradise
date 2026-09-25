"""Operations: startup in both modes, admin alerts for errors and a rejected token, delivery failures (§10, §11, §14.8)."""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Awaitable, Callable

import pytest
from aiohttp.test_utils import TestServer

from app import repo
from app.config import Config, load_config
from app.context import CTX_KEY, AppContext
from app.core import texts
from app.core.clock import FakeClock
from app.log import JsonFormatter
from app.main import create_app
from app.max_api import BotInfo, OutMessage, Target, Unauthorized
from app.payments import robokassa
from tests.bot import ADMIN_ID, Bot
from tests.site import site_client
from tools import fake_max
from tools.fake_max import FakeMaxApi


async def eventually(check: Callable[[], Awaitable[bool]], timeout: float = 3.0) -> None:
    """Wait (in real time) for background work of a running app."""
    deadline = asyncio.get_running_loop().time() + timeout
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, "condition not reached in time"
        await asyncio.sleep(0.01)


# --- §14 scenario 8: delivery ----------------------------------------------------------------------------


async def test_blocked_participant_gets_dm_ok_0_and_result_dm_ok_0(bot: Bot, api: FakeMaxApi) -> None:
    olga = bot.person(100, "Ольга")
    await bot.onboard(olga)
    game = await bot.create_game(olga)
    for user_id, name in ((201, "Иван"), (202, "Мария"), (203, "Пётр")):
        await bot.join(bot.person(user_id, name), game)
    api.block_user(202)
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_DRAW))
    await bot(olga.press(texts.BTN_CONFIRM_DRAW))
    await bot.drain()

    user = await repo.get_user(bot.ctx.db, 202)
    maria = await repo.get_participant(bot.ctx.db, game.id, 202)
    assert user is not None and not user.dm_ok
    assert maria is not None and maria.result_dm_ok is False
    ivan = await repo.get_participant(bot.ctx.db, game.id, 201)
    assert ivan is not None and ivan.result_dm_ok is True
    assert api.last_text(100).startswith("Готово! Пары отправлены 3 из 4. Не дошло: Мария")
    await bot(olga.press(texts.BTN_PANEL))
    await bot(olga.press(texts.BTN_NOT_RECEIVED))
    assert "Мария" in olga.last_text


# --- alerts ------------------------------------------------------------------------------------------------


async def test_rejected_token_on_a_send_alerts_once_and_the_message_still_arrives(
    ctx: AppContext, api: FakeMaxApi
) -> None:
    api.fail_next(Unauthorized(401, "invalid token"), times=3)
    await ctx.outbox.enqueue(Target.user(5), OutMessage("дойдёт"))
    await ctx.outbox.drain()
    assert api.texts_to(5) == ["дойдёт"]
    assert api.texts_to(ADMIN_ID) == [texts.TOKEN_REJECTED]


async def test_web_errors_become_500_and_one_alert(
    ctx: AppContext, api: FakeMaxApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(*_: object) -> None:
        raise RuntimeError("secret detail")

    monkeypatch.setattr(robokassa, "process_result", broken)
    async with site_client(ctx) as client:
        for _ in range(3):
            response = await client.post("/pay/robokassa/result", data={"InvId": "1"})
            assert response.status == 500
            assert "secret" not in await response.text()
        assert (await client.get("/offer")).status == 200
    await ctx.outbox.drain()
    alerts = api.texts_to(ADMIN_ID)
    assert alerts == [texts.error_alert("RuntimeError на сайте (POST /pay/robokassa/result)")]


def test_json_log_lines_carry_extra_fields() -> None:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("test.json")
    logger.addHandler(handler)
    logger.propagate = False
    logger.warning("ответ", extra={"user_id": 5})
    entry = json.loads(stream.getvalue())
    assert (entry["level"], entry["msg"], entry["user_id"], entry["logger"]) == ("warning", "ответ", 5, "test.json")


# --- startup and shutdown (§11) ------------------------------------------------------------------------------


async def test_webhook_mode_starts_the_scheduler_and_stops_cleanly(config: Config, clock: FakeClock) -> None:
    api = FakeMaxApi(clock=clock)
    server = TestServer(create_app(config, api=api, clock=clock))
    await server.start_server()
    ctx = server.app[CTX_KEY]

    async def scheduled() -> bool:
        return "webhook_watchdog" in await repo.job_runs(ctx.db)

    await eventually(scheduled)
    assert [s.update_types for s in api.subscriptions] == [(
        "bot_started", "bot_added", "bot_removed", "bot_stopped", "message_created", "message_callback")]
    await server.close()


async def test_polling_mode_processes_updates_without_a_webhook(env: dict[str, str], clock: FakeClock) -> None:
    polling = load_config({**env, "MODE": "polling"}, announce=lambda _: None)
    api = FakeMaxApi(clock=clock)
    api.pending_updates.append(fake_max.bot_started(42, name="Анна"))
    server = TestServer(create_app(polling, api=api, clock=clock))
    await server.start_server()
    ctx = server.app[CTX_KEY]

    async def answered() -> bool:
        return bool(api.messages_to(42))

    await eventually(answered)
    assert api.last_text(42) == texts.consent(polling.public_base_url)
    assert api.subscriptions == [] and not ctx.runtime.webhook_registered
    names = set(await repo.job_runs(ctx.db))
    assert "webhook_watchdog" not in names
    await server.close()


async def test_rejected_token_at_startup_alerts_the_admins(
    config: Config, clock: FakeClock, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = FakeMaxApi(clock=clock)

    async def get_me() -> BotInfo:
        raise Unauthorized(401, "invalid token")

    monkeypatch.setattr(api, "get_me", get_me)
    server = TestServer(create_app(config, api=api, clock=clock))
    await server.start_server()
    ctx = server.app[CTX_KEY]
    await ctx.outbox.drain()
    assert texts.TOKEN_REJECTED in api.texts_to(ADMIN_ID)
    await server.close()
