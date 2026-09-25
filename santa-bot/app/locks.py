"""Per-user and per-game asyncio locks (§11), and one lock for the ad autopilot.

A per-user lock serializes each user's updates; a per-game lock covers join,
limit, payment and draw. Keyed locks disappear when nobody holds or waits for them.
The promo lock serializes the autopilot's decisions with the admins' ad commands
(PROMO_SPEC §6), so a budget is never decided on stale numbers.
"""

from __future__ import annotations

import asyncio
import weakref


class KeyedLocks:
    def __init__(self) -> None:
        self._locks: weakref.WeakValueDictionary[int, asyncio.Lock] = weakref.WeakValueDictionary()

    def __call__(self, key: int) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock


class Locks:
    def __init__(self) -> None:
        self.user = KeyedLocks()
        self.game = KeyedLocks()
        self.promo = asyncio.Lock()
