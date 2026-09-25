"""Tracking links (PROMO_SPEC §2, §7): manual links made with /link and the ad campaigns' URLs.

A link leads to the landing page with ``?src=<source>`` (the landing puts it into the bot
link ``s_<source>``, sanitized to [a-z0-9]{1,16}) or straight into the bot. Sources are
``p<slug>`` for manual links, ``yd<campaignId>`` and ``vk<adPlanId>`` for campaigns.

Platform macros, checked 2026-09-25: Yandex Direct replaces ``{campaign_id}`` in ad links
(yandex.ru/support/direct/statistics/url-tags), so one URL serves every Direct campaign.
VK Ads replaces ``{{ad_plan_id}}`` with the campaign id — ``{{campaign_id}}`` is the ad
group there — in the group's «Параметры URL», which take priority over the ad's own link
and default to VK's automatic UTM tags (ads.vk.ru/help/features/utm).
"""

from __future__ import annotations

import re
from datetime import datetime

from app.config import Config
from app.core.inputs import MAX_TITLE, clean_text, shorten
from app.core.payloads import deep_link, source_payload
from app.db import Db
from app.promo import store
from app.promo.attribution import SourceMetrics, source_metrics
from app.promo.platforms import Platform
from app.promo.store import PromoLink

_SLUG = re.compile(r"^[a-z0-9]{1,15}$")
DIRECT_SOURCE_TEMPLATE = "yd{campaign_id}"
VK_URL_PARAMS_TEMPLATE = "src=vk{{ad_plan_id}}"


def landing_url(config: Config, src: str) -> str:
    return f"{config.public_base_url}/?src={src}"


def bot_url(config: Config, src: str) -> str:
    return deep_link(config.max_bot_username, source_payload(src))


def campaign_template(config: Config, platform: Platform) -> str:
    """One URL (Direct) or URL parameters (VK) that work for every ad without re-moderation."""
    if platform == Platform.DIRECT:
        return landing_url(config, DIRECT_SOURCE_TEMPLATE)
    return VK_URL_PARAMS_TEMPLATE


def link_src(slug: str) -> str | None:
    """p<slug> for a valid slug ([a-z0-9]{1,15}, any case), else None."""
    normalized = slug.lower()
    return f"p{normalized}" if _SLUG.match(normalized) else None


async def create_link(db: Db, src: str, title: str, admin_id: int, now: datetime) -> tuple[PromoLink, bool]:
    """The link for ``src`` and whether it is new; an existing link keeps its title."""
    created = await store.insert_link(db, src, shorten(clean_text(title), MAX_TITLE) or src[1:], admin_id, now)
    link = await store.get_link(db, src)
    assert link is not None
    return link, created


async def links_with_metrics(db: Db, cutoff: datetime) -> list[tuple[PromoLink, SourceMetrics]]:
    links = await store.all_links(db)
    metrics = await source_metrics(db, [link.src for link in links], cutoff)
    return [(link, metrics[link.src]) for link in links]
