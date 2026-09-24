"""Routes typed updates to the private, callback, admin and group handlers.

Seam: ``app.updates.process_update`` calls ``dispatch`` for every new update,
already deduplicated and under the sender's per-user lock.
"""

from __future__ import annotations

import logging

from app.context import AppContext
from app.max_api import Update

log = logging.getLogger(__name__)


async def dispatch(ctx: AppContext, update: Update) -> None:
    log.info("update received; conversation handlers are not installed yet", extra={"kind": type(update).__name__})
