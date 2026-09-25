"""Shared fixtures: a fake clock, a migrated database, FakeMaxApi and a full AppContext."""

from __future__ import annotations

import random
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import pytest

from app import repo
from app.config import Config, load_config
from app.context import AppContext
from app.core.clock import FakeClock
from app.core.models import User
from app.db import Database
from app.main import build_context
from tests.bot import Bot
from tests.helpers import consented_user
from tools.fake_max import FakeMaxApi

BASE_ENV = {
    "DOMAIN": "santa.example.ru",
    "MAX_BOT_TOKEN": "test-token",
    "MAX_BOT_USERNAME": "santa_test_bot",
    "MAX_WEBHOOK_SECRET": "webhook-secret-0123456789abcdefgh",
    "WEBHOOK_PATH_SECRET": "pathsecret0123456789abcd",
    "ADMIN_EXPORT_TOKEN": "export-token-0123456789",
    "ADMIN_USER_IDS": "9000",
    "OWNER_FULL_NAME": "Иванов Иван Иванович",
    "OWNER_INN": "123456789012",
    "SUPPORT_EMAIL": "help@santa.example.ru",
    "ROBOKASSA_MERCHANT_LOGIN": "santa-shop",
    "ROBOKASSA_TEST_PASSWORD1": "test-pass-1",
    "ROBOKASSA_TEST_PASSWORD2": "test-pass-2",
}


@pytest.fixture
def env(tmp_path: Path) -> dict[str, str]:
    return {**BASE_ENV, "DATA_DIR": str(tmp_path / "data")}


@pytest.fixture
def config(env: dict[str, str]) -> Config:
    return load_config(env, announce=lambda _: None)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def rng() -> random.Random:
    return random.Random(20261101)


@pytest.fixture
async def db(tmp_path: Path, config: Config) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "test.db")
    await database.migrate()
    await repo.seed_settings(database, config.default_settings)
    yield database
    await database.close()


@pytest.fixture
def api(clock: FakeClock) -> FakeMaxApi:
    return FakeMaxApi(clock=clock)


@pytest.fixture
async def ctx(config: Config, api: FakeMaxApi, clock: FakeClock, rng: random.Random) -> AsyncIterator[AppContext]:
    context = await build_context(config, api=api, clock=clock, rng=rng)
    yield context
    await context.wait_background()
    await context.db.close()


@pytest.fixture
def bot(ctx: AppContext, api: FakeMaxApi) -> Bot:
    """Drives updates through ``process_update``; handler errors fail the test (see tests/bot.py)."""
    return Bot(ctx, api)


@pytest.fixture
def make_user(db: Database, clock: FakeClock) -> Callable[..., Awaitable[User]]:
    """Create a consented user in ``db``: ``await make_user(101, 'Ольга')``."""

    async def create(user_id: int, name: str = "Участник", source: str = "direct") -> User:
        return await consented_user(db, clock, user_id, name, source)

    return create
