"""The ad platforms behind one small protocol (PROMO_SPEC §3), and gross/net money.

All money inside the app is gross (VAT included) integer kopecks. Both cabinets and APIs
show budgets and spend net of VAT (Yandex Direct: «Все денежные показатели в аккаунте
указаны без учета НДС»; VK Ads: «без учёта НДС»), so the clients convert at the edge with
PROMO_VAT_PCT. The Direct spend report is requested with VAT included and needs no
conversion.

Errors of both clients map to four classes: ``AuthError`` (the owner must fix a token or
a key; the message says how), ``RateLimited``, ``Transient`` and ``PlatformError``.
"""

from __future__ import annotations

import json
import logging
import ssl
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Protocol

import aiohttp

log = logging.getLogger(__name__)

PLATFORM_MINIMUM_NET_KOP = 300_00  # both platforms: a budget of at least 300 ₽ without VAT
DEFAULT_TIMEOUT = 30.0
# PlatformError codes of the clients themselves (the platforms' own codes are numbers or their strings)
UNSUPPORTED_BUDGET = "unsupported_budget"  # not a budget the client can change (Direct weekly, VK daily)
NOT_FOUND = "not_found"
BAD_RESPONSE = "bad_response"  # an answer that cannot be read


class Platform(StrEnum):
    DIRECT = "yd"
    VK = "vk"


class CampaignState(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"
    OTHER = "other"  # not showing for another reason: moderation, no money, ended, archived…


class BudgetKind(StrEnum):
    WEEK = "week"
    DAY = "day"


@dataclass(frozen=True, slots=True)
class Budget:
    kop: int  # gross
    kind: BudgetKind


@dataclass(frozen=True, slots=True)
class CampaignInfo:
    id: str
    name: str
    state: CampaignState
    budget: Budget | None  # None: the platform shows no weekly (Direct) or daily (VK) budget


@dataclass(frozen=True, slots=True)
class SpendRow:
    cost_kop: int  # gross
    clicks: int


SpendByDay = dict[tuple[str, date], SpendRow]


class AdApiError(Exception):
    """Any failure of an ad platform call."""


class AuthError(AdApiError):
    """The platform refused our credentials; ``code`` picks the fix the admins are told."""

    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail


class RateLimited(AdApiError):
    """Too many requests or no API points left: try again later."""


class Transient(AdApiError):
    """Network failures, timeouts and server errors: try again later."""


class PlatformError(AdApiError):
    """The platform rejected the request (``message`` is its own explanation), or the client could not
    do what was asked; then ``code`` is one of the codes below."""

    def __init__(self, code: str | int, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


class AdPlatform(Protocol):
    platform: Platform

    async def list_campaigns(self, ids: Sequence[str]) -> list[CampaignInfo]:
        """The campaigns with these ids; unknown ids are left out."""

    async def daily_spend(self, ids: Sequence[str], date_from: date, date_to: date) -> SpendByDay:
        """Gross spend and clicks per (campaign id, day); days without spend may be missing."""

    async def suspend(self, campaign_id: str) -> None:
        """Stop the campaign; stopping a stopped campaign succeeds."""

    async def resume(self, campaign_id: str) -> None:
        """Start the campaign again; resuming a running campaign succeeds."""

    async def set_budget(self, campaign_id: str, gross_kop: int, kind: BudgetKind) -> None: ...

    async def close(self) -> None: ...


# --- money ------------------------------------------------------------------------------------------


def kop_from_rub(value: str | int | float | Decimal) -> int:
    """'1496.50' (rubles, as the APIs print them) → 149650 kopecks. Raises ``ValueError``."""
    try:
        amount = Decimal(str(value).strip().replace(",", "."))
    except InvalidOperation as error:
        raise ValueError(f"not an amount: {value!r}") from error
    if not amount.is_finite():
        raise ValueError(f"not an amount: {value!r}")
    return int((amount * 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def gross_kop(net_kop: int, vat_pct: int) -> int:
    """Net → gross, rounded half up to the kopeck."""
    return int((Decimal(net_kop) * (100 + vat_pct) / 100).quantize(Decimal(1), rounding=ROUND_HALF_UP))


def net_kop(gross: int, vat_pct: int) -> int:
    """Gross → net, rounded down: a budget set on a platform never exceeds the approved gross sum."""
    return gross * 100 // (100 + vat_pct)


def minimum_gross_kop(vat_pct: int) -> int:
    """The platforms' 300 ₽ net minimum budget, gross (366 ₽ at 22%)."""
    return gross_kop(PLATFORM_MINIMUM_NET_KOP, vat_pct)


# --- HTTP ---------------------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class HttpReply:
    status: int
    headers: Mapping[str, str]
    text: str

    def json(self) -> dict[str, Any]:
        """The body as a JSON object, or {} for anything else (parsed leniently)."""
        try:
            data = json.loads(self.text) if self.text else {}
        except ValueError:
            return {}
        return data if isinstance(data, dict) else {}


class HttpClient:
    """One aiohttp session with the app's TLS trust; network failures become ``Transient``.

    Callers pass headers per request, so a token never sits in a shared default.
    """

    def __init__(self, ssl_context: ssl.SSLContext | bool = True, *, timeout: float = DEFAULT_TIMEOUT) -> None:
        self._ssl = ssl_context
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None

    async def request(
        self,
        method: str,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        form: Mapping[str, str] | None = None,
        timeout: float | None = None,
    ) -> HttpReply:
        try:
            async with self._get_session().request(
                method, url, headers=headers, params=params, json=json_body, data=form,
                timeout=aiohttp.ClientTimeout(total=timeout or self._timeout),
            ) as response:
                return HttpReply(response.status, dict(response.headers), await response.text())
        except TimeoutError as error:
            raise Transient(f"timeout on {method} {_path(url)}") from error
        except aiohttp.ClientError as error:
            raise Transient(f"{type(error).__name__} on {method} {_path(url)}") from error

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=self._ssl, limit=5))
        return self._session


def _path(url: str) -> str:
    """The URL without the query (queries never carry secrets here, but keep logs short)."""
    return url.split("?", 1)[0]


def response_int(value: Any) -> int | None:
    """An integer from a lenient JSON value or header (1, '1'); None otherwise."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        return int(value.strip())
    return None


def chunks(items: Sequence[str], size: int) -> list[Sequence[str]]:
    return [items[i : i + size] for i in range(0, len(items), size)]
