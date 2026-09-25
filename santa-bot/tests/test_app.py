"""The app factory starts and stops; process_update deduplicates and guards dispatch."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from aiohttp.test_utils import TestServer

from app import repo
from app.config import Config, load_config
from app.context import CTX_KEY, AppContext
from app.core import texts
from app.core.clock import FakeClock
from app.handlers import router
from app.main import create_app
from app.updates import process_update
from tools import fake_max
from tools.fake_max import FakeMaxApi


async def test_app_starts_registers_webhook_and_stops(config: Config, clock: FakeClock) -> None:
    api = FakeMaxApi(clock=clock)
    server = TestServer(create_app(config, api=api, clock=clock))
    await server.start_server()
    ctx = server.app[CTX_KEY]
    assert ctx.runtime.webhook_registered and ctx.runtime.bot_username == fake_max.BOT_USERNAME
    assert [s.url for s in api.subscriptions] == [config.webhook_url]
    assert [name for name, _ in api.commands] == ["start", "help", "cancel", "whoami"]
    assert (await ctx.settings()).free_limit == 10
    await server.close()


async def test_app_starts_without_a_bot_token(env: dict[str, str], clock: FakeClock) -> None:
    site_only = load_config({**env, "MAX_BOT_TOKEN": "", "ROBOKASSA_MERCHANT_LOGIN": ""}, announce=lambda _: None)
    api = FakeMaxApi(clock=clock)
    server = TestServer(create_app(site_only, api=api, clock=clock))
    await server.start_server()
    assert not server.app[CTX_KEY].runtime.webhook_registered and api.subscriptions == []
    await server.close()


async def test_username_mismatch_warns_admins(config: Config, clock: FakeClock) -> None:
    api = FakeMaxApi(clock=clock, bot_username="se999_bot")
    server = TestServer(create_app(config, api=api, clock=clock))
    await server.start_server()
    await server.app[CTX_KEY].outbox.drain()
    assert "se999_bot" in api.last_text(9000)
    await server.close()


@pytest.fixture
def dispatched(monkeypatch: pytest.MonkeyPatch) -> AsyncMock:
    mock = AsyncMock()
    monkeypatch.setattr(router, "dispatch", mock)
    return mock


async def test_duplicates_and_bots_are_dropped(ctx: AppContext, dispatched: AsyncMock) -> None:
    update = fake_max.message_created(5, "привет")
    await process_update(ctx, update)
    await process_update(ctx, update)
    await process_update(ctx, fake_max.message_created(6, "я бот", is_bot=True))
    await process_update(ctx, {"update_type": "message_edited"})
    assert dispatched.await_count == 1
    assert ctx.runtime.last_update_at is not None


async def test_reachability_is_tracked(ctx: AppContext, clock: FakeClock, dispatched: AsyncMock) -> None:
    await repo.insert_user(ctx.db, 5, "direct", clock.now())
    await process_update(ctx, fake_max.bot_stopped(5))
    assert not (await repo.get_user(ctx.db, 5)).dm_ok
    await process_update(ctx, fake_max.bot_started(5))
    assert (await repo.get_user(ctx.db, 5)).dm_ok


async def test_handler_errors_alert_admins_throttled(
    ctx: AppContext, api: FakeMaxApi, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(router, "dispatch", AsyncMock(side_effect=ValueError("boom")))
    for i in range(3):
        await process_update(ctx, fake_max.message_created(5, f"текст {i}"))
    await ctx.outbox.drain()
    alerts = api.texts_to(9000)
    assert len(alerts) == 1 and "ValueError" in alerts[0] and "boom" not in alerts[0]
    assert api.texts_to(5) == [texts.SOMETHING_WENT_WRONG] * 3
