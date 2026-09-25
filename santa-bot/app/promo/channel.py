"""Posts in the owner's own public MAX channel (PROMO_SPEC §8).

The bot must be an admin of the channel («Администратором канала может быть назначен как
пользователь, так и бот», dev.max.ru). Posts go through the outbox — POST /messages with
``chat_id``, rate-limited and retried — and each calendar post is recorded in
``promo_posts_sent`` in the same transaction as its outbox row, so it goes out once. A post
more than 24 hours overdue is skipped and recorded as such: stale advice looks odd in a
channel. A post MAX refused is marked failed by the outbox hooks (``app.delivery``), which
tell the admins; /channel send tries a skipped or failed post again. The outbox does not
hand back message ids, so ``mid`` stays empty.

When the bot is added to a channel (bot_added with is_channel), the admins get its id for
PROMO_MAX_CHANNEL_ID, and /channel lists such channels (P1).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from app.config import Config
from app.context import AppContext
from app.core import kb, texts
from app.core.payloads import deep_link, source_payload
from app.max_api import BotAdded, OutMessage, Target
from app.outbox import PURPOSE_PROMO_POST
from app.promo import content, store
from app.promo.content import Post
from app.promo.store import PostStatus

log = logging.getLogger(__name__)

OVERDUE = timedelta(hours=24)


def post_message(config: Config, post: Post) -> OutMessage:
    """The post as it appears in the channel: the text, the ad label (if set) and the bot button."""
    label = config.promo.channel_ad_label
    text = f"{post.text}\n\n{label}" if label else post.text
    link = deep_link(config.max_bot_username, source_payload(post.id))
    return OutMessage(text, kb.keyboard(kb.link(texts.BTN_PROMO_CHANNEL, link)))


def due_at(post: Post, year: int, tz: ZoneInfo) -> datetime:
    month, day = (int(part) for part in post.date.split("-"))
    hour, minute = (int(part) for part in post.time.split(":"))
    return datetime(year, month, day, hour, minute, tzinfo=tz)


async def post_due(ctx: AppContext) -> None:
    """The promo_channel job (every 10 minutes): post what is due, skip what is a day overdue."""
    channel_id = ctx.config.promo.channel_id
    if channel_id is None or not (await store.get_settings(ctx.db)).channel_on:
        return
    now, tz = ctx.clock.now(), ctx.config.tz
    recorded = await store.post_statuses(ctx.db)
    for post in content.CALENDAR:
        at = due_at(post, now.astimezone(tz).year, tz)
        if post.id in recorded or at > now:
            continue
        if now - at > OVERDUE:
            await store.mark_post(ctx.db, post.id, PostStatus.SKIPPED, now)
            log.info("channel post skipped as overdue", extra={"post": post.id})
        elif await publish(ctx, channel_id, post):
            log.info("channel post queued", extra={"post": post.id})


async def publish(ctx: AppContext, channel_id: int, post: Post) -> bool:
    """Queue ``post`` for the channel unless it went out already (a skipped or failed post may go again)."""
    now = ctx.clock.now()
    async with ctx.db.transaction() as tx:
        fresh = await store.mark_post(tx, post.id, PostStatus.SENT, now)
        if not fresh and not await store.resend_post(tx, post.id, now):
            return False
        outbox_id = await ctx.outbox.enqueue(Target.chat(channel_id), post_message(ctx.config, post),
                                             purpose=PURPOSE_PROMO_POST, db=tx)
        assert outbox_id is not None  # no dedupe key: the row is always new
        await store.set_post_outbox(tx, post.id, outbox_id)
    return True


async def upcoming(ctx: AppContext, count: int) -> list[tuple[Post, datetime]]:
    """The next posts that will go out (not recorded yet, not a day overdue)."""
    now, tz = ctx.clock.now(), ctx.config.tz
    recorded = await store.post_statuses(ctx.db)
    pending = [(post, due_at(post, now.astimezone(tz).year, tz)) for post in content.CALENDAR
               if post.id not in recorded]
    return sorted(((post, at) for post, at in pending if now - at <= OVERDUE), key=lambda item: item[1])[:count]


async def channel_added(ctx: AppContext, update: BotAdded) -> None:
    """P1: the bot became a channel admin — tell the admins the id for PROMO_MAX_CHANNEL_ID."""
    await store.remember_channel(ctx.db, update.chat_id, ctx.clock.now())
    await ctx.alerts.notify_admins(texts.channel_added(update.chat_id))
