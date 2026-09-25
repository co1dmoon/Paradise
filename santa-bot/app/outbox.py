"""Durable, rate-limited delivery (§10).

Bulk and scheduled messages are rows in the ``outbox`` table, delivered by one
worker. Interactive replies are sent directly (``send_now``, ``edit_now``,
``answer``) but pass through the same ``RateLimiter``:

- globally 25 requests/s, evenly spaced (a token bucket with a burst of 1, so
  any one-second window holds at most 25 calls, well under MAX's 30/s);
- per target (dialog or chat) one send or edit per second, plus a separate
  one-per-second lane for callback answers, so a dialog never exceeds MAX's 2/s.

Failures: 429/transient errors (and 401, which is usually a config mistake being
fixed, and alerts the admins) retry after 2, 5, 15, 60, 180 and 600 s, then the
row is dead. Forbidden
to a user sets ``users.dm_ok = 0``. Every final outcome reaches ``OutboxHooks``
(see ``app.delivery``), which tracks draw results and alerts the admins.

The clock is injected; ``drain`` runs the worker to completion on a fake clock.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Protocol

from app import repo
from app.core.clock import Clock, from_iso, to_iso
from app.db import Db
from app.max_api import (
    Forbidden,
    MaxApi,
    MaxApiError,
    OutMessage,
    RateLimited,
    Target,
    Transient,
    Unauthorized,
)

log = logging.getLogger(__name__)

GLOBAL_PER_SECOND = 25.0
PER_TARGET_INTERVAL = 1.0
RETRY_DELAYS: tuple[int, ...] = (2, 5, 15, 60, 180, 600)
IDLE_POLL_SECONDS = 2.0
BATCH_SIZE = 100
PURPOSE_DRAW_RESULT = "draw_result"
PURPOSE_ALERT = "alert"
_RETRYABLE = (RateLimited, Transient, Unauthorized)


class RateLimiter:
    """Reservation-based limiter: ``acquire`` books the next free slot, then sleeps until it."""

    def __init__(
        self,
        clock: Clock,
        *,
        per_second: float = GLOBAL_PER_SECOND,
        per_key_interval: float = PER_TARGET_INTERVAL,
    ) -> None:
        self._clock = clock
        self._spacing = 1.0 / per_second
        self._key_interval = per_key_interval
        self._next_global = float("-inf")
        self._next_by_key: dict[str, float] = {}

    def key_wait(self, key: str) -> float:
        """Seconds until ``key`` may be used again (0 when free now)."""
        return max(0.0, self._next_by_key.get(key, float("-inf")) - self._clock.monotonic())

    async def acquire(self, key: str) -> None:
        now = self._clock.monotonic()
        slot = max(now, self._next_global, self._next_by_key.get(key, float("-inf")))
        self._next_global = slot + self._spacing
        self._next_by_key[key] = slot + self._key_interval
        if len(self._next_by_key) > 10_000:
            self._forget_idle_keys(now)
        if slot > now:
            await self._clock.sleep(slot - now)

    def _forget_idle_keys(self, now: float) -> None:
        self._next_by_key = {key: t for key, t in self._next_by_key.items() if t > now}


@dataclass(frozen=True, slots=True)
class OutboxItem:
    id: int
    kind: str
    target: Target
    message_id: str | None
    message: OutMessage
    disable_preview: bool
    attempts: int
    purpose: str | None
    game_id: int | None


class OutboxHooks(Protocol):
    async def on_delivered(self, item: OutboxItem) -> None: ...

    async def on_failed(self, target: Target, error: MaxApiError, item: OutboxItem | None) -> None:
        """A final failure: a dead outbox row, or a failed direct send (``item`` is None)."""

    async def on_unauthorized(self) -> None:
        """MAX rejected the bot token (401); the message will be retried."""


class Outbox:
    def __init__(
        self,
        db: Db,
        api: MaxApi,
        clock: Clock,
        *,
        limiter: RateLimiter | None = None,
        hooks: OutboxHooks | None = None,
    ) -> None:
        self._db = db
        self._api = api
        self._clock = clock
        self._limiter = limiter or RateLimiter(clock)
        self.hooks = hooks
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    # --- queueing ---------------------------------------------------------------------

    async def enqueue(
        self,
        target: Target,
        message: OutMessage,
        *,
        disable_preview: bool = False,
        dedupe_key: str | None = None,
        purpose: str | None = None,
        game_id: int | None = None,
        delay: float = 0.0,
        db: Db | None = None,
    ) -> int | None:
        """Queue a send. Pass ``db`` to enqueue inside an open transaction.

        Returns the row id, or None when ``dedupe_key`` was already queued.
        """
        return await self._insert(
            db or self._db, "send", target, None, message, disable_preview, dedupe_key, purpose, game_id, delay
        )

    async def enqueue_edit(
        self,
        target: Target,
        message_id: str,
        message: OutMessage,
        *,
        dedupe_key: str | None = None,
        purpose: str | None = None,
        game_id: int | None = None,
        db: Db | None = None,
    ) -> int | None:
        return await self._insert(
            db or self._db, "edit", target, message_id, message, False, dedupe_key, purpose, game_id, 0.0
        )

    async def _insert(
        self,
        db: Db,
        kind: str,
        target: Target,
        message_id: str | None,
        message: OutMessage,
        disable_preview: bool,
        dedupe_key: str | None,
        purpose: str | None,
        game_id: int | None,
        delay: float,
    ) -> int | None:
        now = self._clock.now()
        result = await db.execute(
            "INSERT OR IGNORE INTO outbox (kind, target_type, target_id, message_id, body, disable_preview,"
            " not_before, created_at, dedupe_key, purpose, game_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                kind, target.kind, target.id, message_id, json.dumps(message.to_max(), ensure_ascii=False),
                disable_preview, to_iso(now + timedelta(seconds=delay)), to_iso(now), dedupe_key, purpose, game_id,
            ),
        )
        self.wake()
        return result.lastrowid if result.rowcount == 1 else None

    def wake(self) -> None:
        self._wake.set()

    async def pending_count(self) -> int:
        return int(await self._db.fetchval("SELECT COUNT(*) FROM outbox WHERE status = 'pending'"))

    async def cancel_pending(self, purpose: str, game_id: int, *, db: Db | None = None) -> int:
        """Drop queued messages that no longer make sense (the old pairs after a redraw)."""
        result = await (db or self._db).execute(
            "UPDATE outbox SET status = 'dead', last_error = 'superseded'"
            " WHERE status = 'pending' AND purpose = ? AND game_id = ?",
            (purpose, game_id),
        )
        return result.rowcount

    async def prune(self, before: datetime) -> int:
        """Delete delivered and dead rows created before ``before``."""
        result = await self._db.execute(
            "DELETE FROM outbox WHERE status IN ('done', 'dead') AND created_at < ?", (to_iso(before),)
        )
        return result.rowcount

    # --- direct sends (interactive replies) -------------------------------------------------

    async def send_now(self, target: Target, message: OutMessage, *, disable_preview: bool = False) -> str | None:
        """Send immediately through the limiter; returns the mid, or None on failure.

        A retryable failure is handed over to the outbox so the reply still arrives.
        """
        await self._limiter.acquire(target.key)
        try:
            return await self._api.send(target, message, disable_preview)
        except _RETRYABLE as error:
            log.warning("direct send failed, queued for retry", extra={"target": target.key, "error": str(error)})
            await self.enqueue(target, message, disable_preview=disable_preview, delay=RETRY_DELAYS[0])
            if isinstance(error, Unauthorized) and self.hooks is not None:
                await self.hooks.on_unauthorized()
        except MaxApiError as error:
            await self._final_failure(target, error, None)
        return None

    async def edit_now(self, target: Target, message_id: str, message: OutMessage) -> bool:
        await self._limiter.acquire(target.key)
        try:
            await self._api.edit(message_id, message)
            return True
        except MaxApiError as error:
            log.warning("direct edit failed", extra={"target": target.key, "error": str(error)})
            return False

    async def answer(
        self,
        target: Target,
        callback_id: str,
        *,
        notification: str | None = None,
        message: OutMessage | None = None,
    ) -> None:
        """Answer a callback (always do this, within ~1 s).

        ``message`` replaces the message the button was on. If a ``notification``
        cannot be shown, it is sent as a normal message instead.
        """
        await self._limiter.acquire(f"answer:{target.key}")
        try:
            shown = await self._api.answer(callback_id, notification, message)
        except MaxApiError as error:
            log.warning("callback answer failed", extra={"target": target.key, "error": str(error)})
            shown = False
        if notification and not shown:
            await self.send_now(target, OutMessage(notification))

    # --- worker -----------------------------------------------------------------------------

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.create_task(self._run(), name="outbox")

    async def stop(self) -> None:
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _run(self) -> None:
        while True:
            self._wake.clear()
            try:
                wait = await self.run_once()
            except Exception:
                log.exception("outbox pass failed")
                wait = IDLE_POLL_SECONDS
            if wait > 0:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)

    async def run_once(self) -> float:
        """Deliver what is due and allowed by the limiter; return seconds until the next attempt."""
        now = self._clock.now()
        rows = await self._db.fetchall(
            "SELECT * FROM outbox WHERE status = 'pending' AND not_before <= ? ORDER BY id LIMIT ?",
            (to_iso(now), BATCH_SIZE),
        )
        busy: set[str] = set()
        next_wait = IDLE_POLL_SECONDS
        delivered = 0
        for row in rows:
            item = _item(row)
            key = item.target.key
            if key in busy:
                continue  # keep per-target order: nothing overtakes a waiting message
            wait = self._limiter.key_wait(key)
            if wait > 0:
                busy.add(key)
                next_wait = min(next_wait, wait)
                continue
            await self._limiter.acquire(key)
            await self._deliver(item)
            delivered += 1
        if delivered:
            return 0.0
        return min(next_wait, await self._seconds_until_next_due(now))

    async def drain(self) -> None:
        """Run the worker until the queue is empty, sleeping on the injected clock (tests, simulator)."""
        while await self.pending_count():
            wait = await self.run_once()
            if wait > 0:
                await self._clock.sleep(wait)

    async def _seconds_until_next_due(self, now: datetime) -> float:
        earliest = await self._db.fetchval(
            "SELECT MIN(not_before) FROM outbox WHERE status = 'pending' AND not_before > ?", (to_iso(now),)
        )
        if earliest is None:
            return IDLE_POLL_SECONDS
        return max(0.0, (from_iso(earliest) - now).total_seconds())

    async def _deliver(self, item: OutboxItem) -> None:
        try:
            if item.kind == "edit" and item.message_id is not None:
                await self._api.edit(item.message_id, item.message)
            else:
                await self._api.send(item.target, item.message, item.disable_preview)
        except _RETRYABLE as error:
            await self._retry(item, error)
        except MaxApiError as error:
            await self._set_status(item.id, "dead", str(error))
            await self._final_failure(item.target, error, item)
        else:
            await self._set_status(item.id, "done", None)
            if self.hooks is not None:
                await self.hooks.on_delivered(item)

    async def _retry(self, item: OutboxItem, error: MaxApiError) -> None:
        attempts = item.attempts + 1
        if isinstance(error, Unauthorized) and self.hooks is not None:
            await self.hooks.on_unauthorized()
        if attempts > len(RETRY_DELAYS):
            await self._db.execute(
                "UPDATE outbox SET status = 'dead', attempts = ?, last_error = ? WHERE id = ?",
                (attempts, str(error), item.id),
            )
            await self._final_failure(item.target, error, item)
            return
        not_before = self._clock.now() + timedelta(seconds=RETRY_DELAYS[attempts - 1])
        await self._db.execute(
            "UPDATE outbox SET attempts = ?, not_before = ?, last_error = ? WHERE id = ?",
            (attempts, to_iso(not_before), str(error), item.id),
        )

    async def _set_status(self, item_id: int, status: str, error: str | None) -> None:
        await self._db.execute(
            "UPDATE outbox SET status = ?, attempts = attempts + 1, last_error = ? WHERE id = ?",
            (status, error, item_id),
        )

    async def _final_failure(self, target: Target, error: MaxApiError, item: OutboxItem | None) -> None:
        log.warning("message not delivered", extra={"target": target.key, "error": str(error)})
        if isinstance(error, Forbidden) and target.kind == "user":
            await repo.set_dm_ok(self._db, target.id, False)
        if self.hooks is not None:
            await self.hooks.on_failed(target, error, item)


def _item(row: Any) -> OutboxItem:
    return OutboxItem(
        id=row["id"],
        kind=row["kind"],
        target=Target(row["target_type"], row["target_id"]),
        message_id=row["message_id"],
        message=OutMessage.from_max(json.loads(row["body"])),
        disable_preview=bool(row["disable_preview"]),
        attempts=row["attempts"],
        purpose=row["purpose"],
        game_id=row["game_id"],
    )
