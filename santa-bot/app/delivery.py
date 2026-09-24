"""Outbox hooks: draw-result bookkeeping and admin alerts for rejected messages (§5.5, §10)."""

from __future__ import annotations

from app.alerts import Alerter
from app.core import games, texts
from app.core.clock import Clock
from app.db import Db
from app.max_api import BadRequest, MaxApiError, OutMessage, Target
from app.outbox import PURPOSE_ALERT, PURPOSE_DRAW_RESULT, Outbox, OutboxItem

BAD_REQUEST_ALERT_INTERVAL = 5 * 60.0


class DeliveryTracker:
    """Implements ``OutboxHooks``.

    - A draw result (purpose ``draw_result``) sets participants.result_dm_ok; when the
      last result of a draw resolves, the organizer gets the 'Пары отправлены' summary.
    - BadRequest on a non-alert message is reported to the admins (throttled).
    """

    def __init__(self, db: Db, outbox: Outbox, alerts: Alerter, clock: Clock) -> None:
        self._db = db
        self._outbox = outbox
        self._alerts = alerts
        self._clock = clock

    async def on_delivered(self, item: OutboxItem) -> None:
        if item.purpose == PURPOSE_DRAW_RESULT:
            await self._draw_result(item, delivered=True)

    async def on_failed(self, target: Target, error: MaxApiError, item: OutboxItem | None) -> None:
        if item is not None and item.purpose == PURPOSE_DRAW_RESULT:
            await self._draw_result(item, delivered=False)
        if isinstance(error, BadRequest) and (item is None or item.purpose != PURPOSE_ALERT):
            await self._alerts.alert(
                "bad_request",
                texts.message_rejected(target=target.key, error=error.detail),
                min_interval=BAD_REQUEST_ALERT_INTERVAL,
            )

    async def _draw_result(self, item: OutboxItem, *, delivered: bool) -> None:
        if item.game_id is None or item.target.kind != "user":
            return
        summary = await games.record_result_delivery(
            self._db, item.game_id, item.target.id, delivered, self._clock.now()
        )
        if summary is None:
            return
        game = summary.game
        await self._outbox.enqueue(
            Target.user(game.organizer_id),
            OutMessage(texts.draw_summary(sent=summary.sent, total=summary.total, failed_names=summary.failed_names)),
            dedupe_key=f"draw_summary:{game.id}:{game.redraw_count}",
            game_id=game.id,
        )
