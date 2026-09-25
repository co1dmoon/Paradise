"""Entry point for every incoming MAX update, shared by the webhook and polling.

``process_update`` parses leniently, drops unknown types, bot senders and
duplicates (webhook retries), keeps ``users.dm_ok`` current, serializes each
user's updates with a per-user lock and hands the typed update to
``app.handlers.router.dispatch``. Unhandled errors are logged and reported to
the admins at most once per 5 minutes; the user is told that something went wrong.
"""

from __future__ import annotations

import logging
from typing import Any

from app import repo
from app.context import AppContext
from app.core import texts
from app.handlers import router
from app.max_api import (
    BotStarted,
    BotStopped,
    CallbackQuery,
    MessageCreated,
    OutMessage,
    Target,
    Update,
    dedupe_key,
    parse_update,
    update_user_id,
)

log = logging.getLogger(__name__)


async def process_update(ctx: AppContext, raw: dict[str, Any]) -> None:
    update = parse_update(raw)
    if update is None:
        return
    if isinstance(update, MessageCreated) and update.sender.is_bot:
        return
    now = ctx.clock.now()
    ctx.runtime.last_update_at = now
    if not await repo.mark_update_processed(ctx.db, dedupe_key(update), now):
        return
    user_id = update_user_id(update)
    try:
        if user_id is None:
            await router.dispatch(ctx, update)
            return
        async with ctx.locks.user(user_id):
            await _track_reachability(ctx, update, user_id)
            await router.dispatch(ctx, update)
    except Exception as error:
        log.exception("update failed", extra={"update_type": raw.get("update_type"), "user_id": user_id})
        await ctx.alerts.error(f"{type(error).__name__} при обработке {raw.get('update_type')}")
        if user_id is not None and _is_private(update):
            await ctx.outbox.send_now(Target.user(user_id), OutMessage(texts.SOMETHING_WENT_WRONG))


def _is_private(update: Update) -> bool:
    """Whether the update came from the user's private chat with the bot (never apologize in a group)."""
    match update:
        case BotStarted():
            return True
        case MessageCreated():
            return update.is_private
        case CallbackQuery():
            return update.chat_type in (None, "dialog")
    return False


async def _track_reachability(ctx: AppContext, update: Update, user_id: int) -> None:
    """A user who writes to the bot can receive messages again; bot_stopped means they cannot."""
    if isinstance(update, BotStopped):
        await repo.set_dm_ok(ctx.db, user_id, False)
    elif isinstance(update, BotStarted | CallbackQuery) or (
        isinstance(update, MessageCreated) and update.is_private
    ):
        await repo.set_dm_ok(ctx.db, user_id, True)
