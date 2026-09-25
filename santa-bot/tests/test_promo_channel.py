"""PROMO_SPEC §8: posts in the owner's MAX channel — calendar, idempotency, overdue posts, label, on/off."""

from __future__ import annotations

import dataclasses
import re
from datetime import datetime, timezone

import pytest

from app.context import AppContext
from app.core import texts
from app.core.clock import FakeClock
from app.core.payloads import sanitize_source
from app.max_api import Forbidden
from app.promo import channel, content, store
from app.promo.store import PostStatus
from tests.bot import ADMIN_ID
from tests.promo_helpers import msk, travel
from tools.fake_max import FakeMaxApi

CHANNEL = -500
LABEL = "Реклама. Иванов И. И., ИНН 123456789012"
BOT_LINK = "https://max.ru/santa_test_bot?start=s_"


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(datetime(2026, 11, 1, 9, 0, tzinfo=timezone.utc))


@pytest.fixture
def env(env: dict[str, str]) -> dict[str, str]:
    return {**env, "PROMO_ENABLED": "1", "PROMO_MAX_CHANNEL_ID": str(CHANNEL)}


@pytest.fixture
async def channel_on(ctx: AppContext) -> None:
    await store.set_settings(ctx.db, channel_on=True)


async def posted(ctx: AppContext, api: FakeMaxApi) -> list[str]:
    await ctx.outbox.drain()
    return [message.text for message in api.messages_in_chat(CHANNEL)]


def test_the_calendar_fits_the_season_and_the_rules() -> None:
    posts = content.CALENDAR
    assert 12 <= len(posts) <= 16
    assert [post.id for post in posts] == [f"ch{n:02d}" for n in range(1, len(posts) + 1)]
    assert (posts[0].date, posts[-1].date) == ("11-05", "12-24")
    assert [post.date for post in posts] == sorted(post.date for post in posts)
    for post in posts:
        assert re.fullmatch(r"(1[12])-([0-2]\d|3[01])", post.date) and re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d",
                                                                                     post.time)
        assert sanitize_source(post.id) == post.id, "the source survives the landing's sanitizer"
        assert len(post.text) + len(LABEL) + 2 <= texts.MAX_MESSAGE
        assert not re.search(r"розыгрыш|конкурс|приз|вишлист|скидк|промокод", post.text, re.IGNORECASE)


async def test_due_posts_go_out_once(ctx: AppContext, api: FakeMaxApi, clock: FakeClock, channel_on: None) -> None:
    travel(clock, msk(5, 11, 59))
    await channel.post_due(ctx)
    assert await posted(ctx, api) == []

    travel(clock, msk(5, 12, 5))
    await channel.post_due(ctx)
    await channel.post_due(ctx)

    assert await posted(ctx, api) == [content.CALENDAR[0].text]
    (message,) = api.messages_in_chat(CHANNEL)
    assert [(button.text, button.url) for button in message.buttons] == [  # type: ignore[union-attr]
        (texts.BTN_PROMO_CHANNEL, BOT_LINK + "ch01")]
    assert await store.post_statuses(ctx.db) == {"ch01": PostStatus.SENT}


async def test_posts_more_than_a_day_overdue_are_skipped(ctx: AppContext, api: FakeMaxApi, clock: FakeClock,
                                                         channel_on: None) -> None:
    travel(clock, msk(10, 11))  # ch01 (5 Nov) is days late, ch02 (9 Nov 12:00) 23 hours late
    await channel.post_due(ctx)
    assert await posted(ctx, api) == [content.CALENDAR[1].text]
    assert await store.post_statuses(ctx.db) == {"ch01": PostStatus.SKIPPED, "ch02": PostStatus.SENT}
    upcoming = await channel.upcoming(ctx, 3)
    assert [post.id for post, _ in upcoming] == ["ch03", "ch04", "ch05"]
    assert await channel.publish(ctx, CHANNEL, content.CALENDAR[0]), "an admin may still send a skipped post"
    assert not await channel.publish(ctx, CHANNEL, content.CALENDAR[0])


async def test_the_ad_label_is_appended(ctx: AppContext, api: FakeMaxApi, clock: FakeClock,
                                        channel_on: None) -> None:
    ctx.config = dataclasses.replace(ctx.config, promo=dataclasses.replace(ctx.config.promo, channel_ad_label=LABEL))
    travel(clock, msk(5, 12, 1))
    await channel.post_due(ctx)
    assert await posted(ctx, api) == [f"{content.CALENDAR[0].text}\n\n{LABEL}"]


async def test_nothing_is_posted_while_off_or_without_a_channel(ctx: AppContext, api: FakeMaxApi,
                                                                clock: FakeClock) -> None:
    travel(clock, msk(5, 12, 1))
    await channel.post_due(ctx)
    assert await posted(ctx, api) == [] and await store.post_statuses(ctx.db) == {}
    await store.set_settings(ctx.db, channel_on=True)
    ctx.config = dataclasses.replace(ctx.config, promo=dataclasses.replace(ctx.config.promo, channel_id=None))
    await channel.post_due(ctx)
    assert await store.post_statuses(ctx.db) == {}


async def test_a_refused_post_alerts_the_admins(ctx: AppContext, api: FakeMaxApi, clock: FakeClock,
                                                channel_on: None) -> None:
    api.fail_next(Forbidden(403, "chat.denied"))
    travel(clock, msk(5, 12, 1))
    await channel.post_due(ctx)
    await ctx.outbox.drain()
    assert api.messages_in_chat(CHANNEL) == []
    assert api.texts_to(ADMIN_ID) == [texts.promo_post_failed("chat.denied")]
    assert await store.post_statuses(ctx.db) == {"ch01": PostStatus.SENT}, "it is not posted twice by the job"
