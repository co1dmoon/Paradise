"""Routes typed updates to the private, callback, admin and group handlers.

Seam: ``app.updates.process_update`` calls ``dispatch`` for every new update,
already deduplicated, with bot senders dropped, under the sender's per-user lock.

- Private messages and bot_started go to ``private``; buttons to ``callbacks``.
- In group chats everything except bot_added and bot_removed is ignored (§5, §5.10);
  a button pressed in a group is only answered.
- ``admin`` is imported so that its commands and buttons
  register themselves with ``private.command`` and ``callbacks.on``.
"""

from __future__ import annotations

import logging

from app.context import AppContext
from app.handlers import admin, callbacks, group, private  # noqa: F401  (admin registers its commands and buttons)
from app.max_api import BotAdded, BotRemoved, BotStarted, BotStopped, CallbackQuery, MessageCreated, Target, Update

log = logging.getLogger(__name__)

PRIVATE_CHAT = "dialog"


async def dispatch(ctx: AppContext, update: Update) -> None:
    match update:
        case BotStarted():
            await private.on_start(ctx, update)
        case MessageCreated() if update.is_private:
            await private.on_message(ctx, update)
        case CallbackQuery() if update.chat_type in (None, PRIVATE_CHAT):
            await callbacks.on_callback(ctx, update)
        case CallbackQuery():
            await ctx.outbox.answer(Target.user(update.user.user_id), update.callback_id)
        case BotAdded():
            await group.on_bot_added(ctx, update)
        case BotRemoved():
            await group.on_bot_removed(ctx, update)
        case MessageCreated() | BotStopped():
            log.info("update ignored", extra={"kind": type(update).__name__})
