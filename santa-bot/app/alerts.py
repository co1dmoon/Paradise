"""Messages to the admins (ADMIN_USER_IDS) with per-key throttling (§7, §9).

Alerts go through the outbox (purpose ``alert``), so they survive restarts and
respect the rate limits. A throttled alert is counted and the count is appended
to the next alert with the same key.
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from app.core import texts
from app.core.clock import Clock
from app.max_api import OutMessage, Target
from app.outbox import PURPOSE_ALERT, Outbox

log = logging.getLogger(__name__)

ERROR_ALERT_INTERVAL = 5 * 60.0
TOKEN_ALERT_INTERVAL = 60 * 60.0


@dataclass(slots=True)
class _Throttle:
    last_sent: float
    suppressed: int = 0


class Alerter:
    def __init__(self, outbox: Outbox, admin_ids: Sequence[int], clock: Clock) -> None:
        self._outbox = outbox
        self._admin_ids = tuple(admin_ids)
        self._clock = clock
        self._throttles: dict[str, _Throttle] = {}

    @property
    def admin_ids(self) -> tuple[int, ...]:
        return self._admin_ids

    async def notify_admins(self, message: OutMessage | str) -> None:
        """Send to every admin now-ish (queued). Logs a warning when no admin is configured."""
        if isinstance(message, str):
            message = OutMessage(message)
        if not self._admin_ids:
            log.warning("admin notification dropped: ADMIN_USER_IDS is empty")
            return
        for admin_id in self._admin_ids:
            await self._outbox.enqueue(Target.user(admin_id), message, disable_preview=True, purpose=PURPOSE_ALERT)

    async def alert(self, key: str, text: str, *, min_interval: float = ERROR_ALERT_INTERVAL) -> bool:
        """Notify the admins at most once per ``min_interval`` seconds for ``key``.

        Returns False when the alert was held back (and counted).
        """
        now = self._clock.monotonic()
        throttle = self._throttles.get(key)
        if throttle is not None and now - throttle.last_sent < min_interval:
            throttle.suppressed += 1
            return False
        suppressed = throttle.suppressed if throttle else 0
        self._throttles[key] = _Throttle(last_sent=now)
        await self.notify_admins(texts.with_suppressed(text, suppressed))
        return True

    async def error(self, summary: str) -> bool:
        """An unhandled exception (handlers, jobs, web): one alert per 5 minutes for all errors."""
        return await self.alert("error", texts.error_alert(summary))

    async def token_rejected(self) -> bool:
        """MAX answered 401 (§9). The alert is queued and arrives once the token works again."""
        log.error("MAX rejected the bot token (401): check MAX_BOT_TOKEN")
        return await self.alert("unauthorized", texts.TOKEN_REJECTED, min_interval=TOKEN_ALERT_INTERVAL)
