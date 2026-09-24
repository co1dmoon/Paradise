"""Time sources and timestamp helpers.

Production code receives a ``Clock`` so tests and the simulator can control
time. All stored timestamps are UTC ISO strings with millisecond precision,
which sort correctly as text.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Protocol


class Clock(Protocol):
    def now(self) -> datetime:
        """Current time as a timezone-aware UTC datetime."""

    def monotonic(self) -> float:
        """Seconds from an arbitrary origin; only differences are meaningful."""

    async def sleep(self, seconds: float) -> None: ...


class SystemClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def monotonic(self) -> float:
        return time.monotonic()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(0.0, seconds))


class FakeClock:
    """Deterministic clock for tests and the simulator: ``sleep`` advances time instantly."""

    def __init__(self, start: datetime | None = None) -> None:
        self._start = start or datetime(2026, 11, 20, 9, 0, tzinfo=timezone.utc)
        self._elapsed = 0.0

    def now(self) -> datetime:
        return self._start + timedelta(seconds=self._elapsed)

    def monotonic(self) -> float:
        return self._elapsed

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds)
        await asyncio.sleep(0)

    def advance(self, seconds: float) -> None:
        if seconds > 0:
            self._elapsed += seconds


def to_iso(moment: datetime) -> str:
    """Serialize an aware datetime as UTC ISO text, e.g. '2026-11-20T09:00:00.000+00:00'."""
    return moment.astimezone(timezone.utc).isoformat(timespec="milliseconds")


def from_iso(text: str) -> datetime:
    return datetime.fromisoformat(text).astimezone(timezone.utc)
