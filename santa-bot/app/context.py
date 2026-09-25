"""``AppContext``: everything a handler, web route or job needs, built once in ``app.main``."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

from aiohttp import web

from app import repo
from app.alerts import Alerter
from app.config import Config
from app.core.clock import Clock
from app.core.dates import local_today
from app.core.models import Settings
from app.core.pricing import PriceList
from app.db import Database
from app.debounce import Debouncer
from app.locks import Locks
from app.max_api import MaxApi
from app.outbox import Outbox
from app.promo.platforms import AdPlatform, Platform

log = logging.getLogger(__name__)


@dataclass(slots=True)
class RuntimeState:
    """Facts learned while running, reported by /healthz."""

    bot_username: str | None = None
    last_update_at: datetime | None = None
    webhook_registered: bool = False


@dataclass(slots=True)
class AppContext:
    """Stored on the aiohttp application under ``CTX_KEY``: ``request.app[CTX_KEY]``."""

    config: Config
    db: Database
    api: MaxApi
    outbox: Outbox
    alerts: Alerter
    clock: Clock
    rng: random.Random
    locks: Locks = field(default_factory=Locks)
    runtime: RuntimeState = field(default_factory=RuntimeState)
    ad_platforms: dict[Platform, AdPlatform] = field(default_factory=dict)  # PROMO_SPEC §3: configured ones only
    debouncer: Debouncer = field(init=False)
    _tasks: set[asyncio.Task[Any]] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.debouncer = Debouncer(self.clock, self.spawn)

    async def settings(self) -> Settings:
        return await repo.get_settings(self.db)

    async def prices(self) -> PriceList:
        return PriceList.from_settings(await self.settings())

    def today(self) -> date:
        """Today in Moscow time (config TZ)."""
        return local_today(self.clock.now(), self.config.tz)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
        """Run in the background, keeping a reference until done; exceptions are logged."""
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._task_done)
        return task

    def _task_done(self, task: asyncio.Task[Any]) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            log.error("background task failed", exc_info=task.exception(), extra={"task": task.get_name()})

    async def wait_background(self) -> None:
        """Wait for spawned tasks (tests, simulator, graceful shutdown)."""
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)


CTX_KEY = web.AppKey("ctx", AppContext)
