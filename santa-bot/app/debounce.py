"""At most one run per interval per key; changes in between fold into one trailing run.

Used for the group card (§5.10: edited at most once per 10 s per game). The action
reads fresh state when it runs, so the last change is never lost. A pending
trailing run lives in memory only: after a restart the next change runs it again.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Coroutine, Hashable
from typing import Any

from app.core.clock import Clock

Spawn = Callable[[Coroutine[Any, Any, None]], object]


class Debouncer:
    def __init__(self, clock: Clock, spawn: Spawn) -> None:
        self._clock = clock
        self._spawn = spawn
        self._last_run: dict[Hashable, float] = {}
        self._pending: set[Hashable] = set()

    async def run(self, key: Hashable, interval: float, action: Callable[[], Awaitable[None]]) -> None:
        """Run ``action`` now if its last run for ``key`` was ``interval`` seconds ago or more;
        otherwise run it once when the interval is over (in the background)."""
        if key in self._pending:
            return
        wait = self._last_run.get(key, float("-inf")) + interval - self._clock.monotonic()
        if wait <= 0:
            await self._run(key, action)
            return
        self._pending.add(key)
        self._spawn(self._later(key, wait, action))

    async def _later(self, key: Hashable, wait: float, action: Callable[[], Awaitable[None]]) -> None:
        try:
            await self._clock.sleep(wait)
        finally:
            self._pending.discard(key)
        await self._run(key, action)

    async def _run(self, key: Hashable, action: Callable[[], Awaitable[None]]) -> None:
        """The interval counts from the end of a run (it may wait for the rate limiter)."""
        self._last_run[key] = self._clock.monotonic()
        try:
            await action()
        finally:
            self._last_run[key] = self._clock.monotonic()
