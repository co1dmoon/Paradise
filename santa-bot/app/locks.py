"""Per-user and per-game asyncio locks (§11).

A per-user lock serializes each user's updates; a per-game lock covers join,
limit, payment and draw. Locks disappear when nobody holds or waits for them.
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
