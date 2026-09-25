"""Group mode (§5.10, P1): the bot in a group chat keeps a live game card there.

- bot_added (not a channel): a greeting with a LINK to gc_{chat} (gcm{abs} for a
  negative chat id); the gc payload runs the creation wizard for that chat
  (``flows.start_group_game``).
- The card lists who takes part and carries one LINK button to j_CODE, so strangers
  in the chat never trigger bot actions. It is edited when the game changes, at
  most once per 10 s per game; if the edit fails, a new card is posted.
- bot_removed: the chat's games continue in link mode.

The owner must first allow group chats in the bot settings (this re-triggers moderation).
"""

from __future__ import annotations

from app import repo
from app.context import AppContext
from app.core.analytics import Event, record
from app.core.games import ACTIVE, Reveal
from app.handlers import views
from app.max_api import BotAdded, BotRemoved, Target

CARD_INTERVAL = 10.0


async def on_bot_added(ctx: AppContext, update: BotAdded) -> None:
    if update.is_channel:
        return
    await ctx.outbox.send_now(Target.chat(update.chat_id), views.group_hello(ctx.config, update.chat_id))
    await record(ctx.db, Event.GROUP_ADDED, ctx.clock.now(), user_id=update.user.user_id if update.user else None)


async def on_bot_removed(ctx: AppContext, update: BotRemoved) -> None:
    await repo.detach_group_chat(ctx.db, update.chat_id)


async def card_changed(ctx: AppContext, game_id: int) -> None:
    """Something the card shows changed (people, limit, status, title…): post or edit it."""
    await ctx.debouncer.run(("group_card", game_id), CARD_INTERVAL, lambda: _refresh_card(ctx, game_id))


async def post_reveal(ctx: AppContext, revealed: Reveal) -> None:
    """§5.11 in group mode: the chain goes to the chat, with a link to start another game."""
    game = revealed.game
    assert game.group_chat_id is not None
    await ctx.outbox.enqueue(Target.chat(game.group_chat_id), views.reveal_in_group(ctx.config, game, revealed.chain),
                             disable_preview=True, dedupe_key=f"reveal:{game.id}:chat", game_id=game.id)


async def _refresh_card(ctx: AppContext, game_id: int) -> None:
    game = await repo.get_game(ctx.db, game_id)
    if game is None or game.group_chat_id is None:
        return
    names = [person.display_name for person in await repo.participants(ctx.db, game_id, ACTIVE)]
    card = views.group_card(ctx.config, game, names)
    chat = Target.chat(game.group_chat_id)
    if game.group_card_mid is not None and await ctx.outbox.edit_now(chat, game.group_card_mid, card):
        return
    mid = await ctx.outbox.send_now(chat, card, disable_preview=True)
    if mid is not None:
        await repo.update_game(ctx.db, game_id, group_card_mid=mid)
