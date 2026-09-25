"""Website pages (§8): the jinja2 environment, values shared by every page and the draw counter.

Pages are server-rendered Russian HTML with no external resources and no scripts,
except the optional Yandex Metrica loader (static/metrica.js) when METRICA_ID is set.
The legal pages are templates filled from .env; the owner must review them (README_RU §13).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import re

import jinja2
from aiohttp import web

from app.config import Config
from app.context import AppContext
from app.core import analytics, texts
from app.core.payloads import deep_link, payment_return_payload, source_payload
from app.core.pricing import PriceList

TEMPLATES_DIR = Path(__file__).parent / "templates"
STATIC_DIR = Path(__file__).parent / "static"
COUNTER_TTL = 10 * 60.0
COUNTER_MIN = 50
EXAMPLE_EXCHANGE = date(2026, 12, 26)
NBSP = "\u00a0"
MISSING = "(не указано)"
_INV_ID = re.compile(r"^[1-9][0-9]{0,17}$")


def environment() -> jinja2.Environment:
    env = jinja2.Environment(
        loader=jinja2.FileSystemLoader(TEMPLATES_DIR),
        autoescape=True,
        undefined=jinja2.StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
    env.filters["rub"] = rubles
    return env


def rubles(amount: int) -> str:
    """2490 → '2 490 ₽' with non-breaking spaces."""
    return f"{amount:,}".replace(",", NBSP) + f"{NBSP}₽"


def html(env: jinja2.Environment, template: str, values: dict[str, Any], *, status: int = 200) -> web.Response:
    body = env.get_template(template).render(values)
    response = web.Response(text=body, status=status, content_type="text/html", charset="utf-8")
    response.headers["Cache-Control"] = "no-cache"
    return response


@dataclass(frozen=True, slots=True)
class Seller:
    """The self-employed seller shown in the footer and the legal pages (OWNER_*, SUPPORT_EMAIL)."""

    full_name: str
    inn: str
    email: str
    has_email: bool

    @classmethod
    def from_config(cls, config: Config) -> Seller:
        """Missing values (a site-only deploy before .env is complete) show up as '(не указано)'."""
        return cls(
            full_name=config.owner_full_name or MISSING,
            inn=config.owner_inn or MISSING,
            email=config.support_email or MISSING,
            has_email=bool(config.support_email),
        )


def bot_link(config: Config, payload: str | None = None) -> str | None:
    """Deep link to the bot; None until MAX_BOT_USERNAME is known (site-only deploy)."""
    if not config.max_bot_username:
        return None
    return deep_link(config.max_bot_username, payload)


def landing_bot_link(config: Config, src: str | None) -> str | None:
    """'Открыть бота' carries s_{src}, src from utm_source or src, sanitized (§6.6)."""
    return bot_link(config, source_payload(src or ""))


def parse_inv_id(raw: str) -> int | None:
    """InvId from Robokassa's redirect, or None when it does not look like one of ours."""
    return int(raw) if _INV_ID.match(raw) else None


def payment_return_link(config: Config, inv_id: int | None) -> str | None:
    """Back to the bot after Robokassa: p_{InvId}, or just the bot when the InvId is unknown."""
    return bot_link(config, None if inv_id is None else payment_return_payload(inv_id))


async def common_values(ctx: AppContext) -> dict[str, Any]:
    """What base.html and the legal pages need: seller, prices, links, Metrica."""
    config = ctx.config
    prices = await ctx.prices()
    return {
        "seller": Seller.from_config(config),
        "free_limit": prices.free_limit,
        "price_rows": price_rows(prices),
        "max_limit": prices.max_limit,
        "base_url": config.public_base_url,
        "bot_url": bot_link(config),
        "metrica_id": config.metrica_id,
        "consent_version": config.consent_version,
        "anon_days": 30,
        "retention_days": 180,
    }


def example_result() -> str:
    """The landing's sample pair message, built by the bot's own text function."""
    return texts.draw_result(
        title="Отдел продаж", receiver="Мария К.", wishes="настольная игра, тёплые носки, хороший чай",
        budget="до 1000 ₽", exchange_date=EXAMPLE_EXCHANGE,
    )


def example_buttons() -> tuple[str, ...]:
    return texts.BTN_ASK_RECEIVER, texts.BTN_WRITE_SANTA


def price_rows(prices: PriceList) -> list[tuple[int, int]]:
    """(limit, price) for every paid tier, smallest first."""
    return [(offer.limit, offer.price) for offer in sorted(prices.offers, key=lambda o: o.limit)]


class DrawCounter:
    """'Уже проведено N жеребьёвок': counted at most once per 10 minutes, hidden below 50."""

    def __init__(self) -> None:
        self._value = 0
        self._expires = float("-inf")

    async def visible(self, ctx: AppContext) -> int | None:
        now = ctx.clock.monotonic()
        if now >= self._expires:
            self._value = await analytics.total_draws(ctx.db)
            self._expires = now + COUNTER_TTL
        return self._value if self._value >= COUNTER_MIN else None

