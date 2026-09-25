"""VK Ads API client (PROMO_SPEC §3.2), checked against ads.vk.ru/doc/api on 2026-09-25.

- Base ``https://ads.vk.ru/api/``; requests carry ``Authorization: Bearer <access_token>``.
- Tokens: ``POST v2/oauth2/token.json`` (form) with ``grant_type=client_credentials`` issues a
  pair that lives 86400 s (``expires_in`` may be a string); ``grant_type=refresh_token``
  renews it. At most 5 tokens may exist per client and user, and the 6th request fails
  with HTTP 403; ``POST v2/oauth2/token/delete.json`` with the client id and secret deletes
  the account's tokens. So the pair is kept in the database and reused, refreshed ahead of
  expiry, and a new one is requested only without a usable pair; on the limit the tokens
  are deleted once and the request is repeated once.
- A request with a bad token gets 401 ``{"code", "message"}``: ``expired_token`` (refresh
  and repeat), ``invalid_token`` (issue a new one and repeat), ``invalid_client`` (wrong id
  or secret), ``invalid_user`` / ``revoked_token`` (the account itself).
- ``GET v2/ad_plans.json?_id__in=…&fields=…`` lists campaigns ({count, offset, items}; at
  most 50 per page); status is active, blocked or deleted; ``budget_limit_day`` and
  ``budget_limit`` are decimals net of VAT.
- ``POST v2/ad_plans/mass_action.json`` takes a JSON array of changes (up to 200) and
  answers 204: ``{"id", "status": "blocked"|"active"}`` or ``{"id", "budget_limit_day"}``.
- ``GET v2/statistics/ad_plans/day.json?id=…&date_from=…&date_to=…&metrics=base`` answers
  ``items[{id, rows[{date, base{clicks, spent}}]}]``; ``spent`` is net of VAT.
- Limits come in ``X-RateLimit-{RPS,Hourly,Daily}-Remaining`` headers; 429 means "too many".
  An exhausted hour or day stops calls until it is over; an exhausted second waits a second.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal
from typing import Any, Protocol

from app.core.clock import Clock
from app.promo.platforms import (
    BAD_RESPONSE,
    UNSUPPORTED_BUDGET,
    AuthError,
    Budget,
    BudgetKind,
    CampaignInfo,
    CampaignState,
    HttpClient,
    HttpReply,
    Platform,
    PlatformError,
    RateLimited,
    SpendByDay,
    SpendRow,
    Transient,
    chunks,
    gross_kop,
    kop_from_rub,
    net_kop,
    response_int,
)

log = logging.getLogger(__name__)

LIVE_URL = "https://ads.vk.ru/api/"
REFRESH_MARGIN = timedelta(minutes=10)
DEFAULT_TOKEN_LIFETIME = 86400
PAGE_LIMIT = 50  # ad_plans.json returns at most 50 items per page
BATCH_LIMIT = 200  # statistics and mass_action take at most 200 objects
MOSCOW = timezone(timedelta(hours=3))  # VK counts daily limits by Moscow days; Moscow has no DST
_STATES = {"active": CampaignState.ACTIVE, "blocked": CampaignState.PAUSED}
_RETRY_WITH_NEW_TOKEN = frozenset({"expired_token", "invalid_token"})


@dataclass(frozen=True, slots=True)
class StoredToken:
    access_token: str
    refresh_token: str | None
    expires_at: datetime


class TokenStore(Protocol):
    async def load(self) -> StoredToken | None: ...

    async def save(self, token: StoredToken) -> None: ...

    async def clear(self) -> None: ...


class _TokenLimit(Exception):
    """VK refused a new token: 5 already exist for this client and user."""


class VkAdsClient:
    platform = Platform.VK

    def __init__(
        self,
        client_id: str,
        client_secret: str,
        tokens: TokenStore,
        *,
        vat_pct: int,
        clock: Clock,
        base_url: str = LIVE_URL,
        http: HttpClient | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._tokens = tokens
        self._vat = vat_pct
        self._clock = clock
        self._base = base_url.rstrip("/") + "/"
        self._http = http or HttpClient()
        self._token_lock = asyncio.Lock()
        self._blocked_until: datetime | None = None
        self._second_used_up = False

    async def close(self) -> None:
        await self._http.close()

    # --- campaigns ---------------------------------------------------------------------------------------

    async def list_campaigns(self, ids: Sequence[str]) -> list[CampaignInfo]:
        campaigns = []
        for batch in chunks(ids, PAGE_LIMIT):
            data = await self._json("GET", "v2/ad_plans.json", params={
                "_id__in": ",".join(batch),
                "fields": "id,name,status,budget_limit_day,budget_limit",
                "limit": str(PAGE_LIMIT),
            })
            campaigns += [self._campaign(item) for item in _items(data)]
        return campaigns

    async def suspend(self, campaign_id: str) -> None:
        await self._mass_action({"id": int(campaign_id), "status": "blocked"})

    async def resume(self, campaign_id: str) -> None:
        await self._mass_action({"id": int(campaign_id), "status": "active"})

    async def set_budget(self, campaign_id: str, gross_kop: int, kind: BudgetKind) -> None:
        if kind != BudgetKind.DAY:
            raise PlatformError(UNSUPPORTED_BUDGET, "only the campaign's daily budget can be changed")
        net = net_kop(gross_kop, self._vat)
        await self._mass_action({"id": int(campaign_id), "budget_limit_day": f"{net // 100}.{net % 100:02d}"})

    async def daily_spend(self, ids: Sequence[str], date_from: date, date_to: date) -> SpendByDay:
        spend: SpendByDay = {}
        for batch in chunks(ids, BATCH_LIMIT):
            data = await self._json("GET", "v2/statistics/ad_plans/day.json", params={
                "id": ",".join(batch),
                "date_from": date_from.isoformat(),
                "date_to": date_to.isoformat(),
                "metrics": "base",
            })
            spend.update(self._spend(data))
        return spend

    def _campaign(self, item: Mapping[str, Any]) -> CampaignInfo:
        day_limit = _decimal(item.get("budget_limit_day"))
        budget = None
        if day_limit is not None and day_limit > 0:
            budget = Budget(gross_kop(kop_from_rub(day_limit), self._vat), BudgetKind.DAY)
        return CampaignInfo(
            id=str(item.get("id", "")),
            name=str(item.get("name") or ""),
            state=_STATES.get(str(item.get("status")), CampaignState.OTHER),
            budget=budget,
        )

    def _spend(self, data: Mapping[str, Any]) -> SpendByDay:
        spend: SpendByDay = {}
        for item in _items(data):
            rows = item.get("rows")
            for row in rows if isinstance(rows, list) else []:
                base = row.get("base") if isinstance(row, dict) else None
                if not isinstance(base, dict):
                    continue
                try:
                    day = date.fromisoformat(str(row.get("date")))
                    spent = kop_from_rub(base.get("spent") or 0)
                except ValueError as error:
                    raise PlatformError(BAD_RESPONSE, f"unreadable statistics row {row!r}"[:120]) from error
                clicks = response_int(base.get("clicks")) or 0
                spend[(str(item.get("id")), day)] = SpendRow(gross_kop(spent, self._vat), clicks)
        return spend

    async def _mass_action(self, change: Mapping[str, Any]) -> None:
        await self._request("POST", "v2/ad_plans/mass_action.json", json_body=[dict(change)])

    # --- requests -------------------------------------------------------------------------------------------

    async def _json(self, method: str, path: str, *, params: Mapping[str, str]) -> dict[str, Any]:
        return (await self._request(method, path, params=params)).json()

    async def _request(
        self, method: str, path: str, *, params: Mapping[str, str] | None = None, json_body: Any = None
    ) -> HttpReply:
        """One API call with a valid token; an expired or unknown token is renewed once."""
        reply = await self._authorized(method, path, params, json_body)
        if reply.status == 401:
            code = _token_error(reply)
            if code not in _RETRY_WITH_NEW_TOKEN:
                raise AuthError(code, str(reply.json().get("message") or ""))
            await self._drop_token(expired_only=code == "expired_token")
            reply = await self._authorized(method, path, params, json_body)
            if reply.status == 401:
                raise AuthError(_token_error(reply), str(reply.json().get("message") or ""))
        _raise_for_status(reply)
        return reply

    async def _authorized(
        self, method: str, path: str, params: Mapping[str, str] | None, json_body: Any
    ) -> HttpReply:
        token = await self._access_token()
        return await self._send(method, path, headers={"Authorization": f"Bearer {token}"}, params=params,
                                json_body=json_body)

    async def _send(self, method: str, path: str, **kwargs: Any) -> HttpReply:
        await self._respect_limits()
        reply = await self._http.request(method, self._base + path, **kwargs)
        self._remember_limits(reply.headers)
        return reply

    async def _respect_limits(self) -> None:
        if self._blocked_until is not None and self._clock.now() < self._blocked_until:
            raise RateLimited(f"VK Ads request limit is used up until {self._blocked_until.isoformat()}")
        if self._second_used_up:
            self._second_used_up = False
            await self._clock.sleep(1.0)

    def _remember_limits(self, headers: Mapping[str, str]) -> None:
        now = self._clock.now()
        if response_int(headers.get("X-RateLimit-Daily-Remaining")) == 0:
            local = now.astimezone(MOSCOW)
            self._blocked_until = datetime.combine(local.date() + timedelta(days=1), datetime.min.time(),
                                                   tzinfo=MOSCOW)
        elif response_int(headers.get("X-RateLimit-Hourly-Remaining")) == 0:
            self._blocked_until = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        self._second_used_up = response_int(headers.get("X-RateLimit-RPS-Remaining")) == 0

    # --- tokens (never logged) -----------------------------------------------------------------------------------

    async def _access_token(self) -> str:
        async with self._token_lock:
            stored = await self._tokens.load()
            if stored is not None and stored.expires_at - self._clock.now() > REFRESH_MARGIN:
                return stored.access_token
            token = await self._renewed(stored) if stored is not None and stored.refresh_token else None
            if token is None:
                token = await self._issue()
            await self._tokens.save(token)
            return token.access_token

    async def _renewed(self, stored: StoredToken) -> StoredToken | None:
        """Refresh the pair; None when VK refuses the refresh token (then a new pair is issued)."""
        try:
            return await self._grant({"grant_type": "refresh_token", "refresh_token": stored.refresh_token or ""})
        except AuthError as error:
            if error.code == "invalid_client":
                raise
            log.warning("vk ads refresh token refused; requesting a new token", extra={"code": error.code})
            return None

    async def _issue(self) -> StoredToken:
        try:
            return await self._grant({"grant_type": "client_credentials"})
        except _TokenLimit:
            log.warning("vk ads token limit reached; deleting the account's tokens once")
        await self._tokens.clear()
        await self._delete_tokens()
        try:
            return await self._grant({"grant_type": "client_credentials"})
        except _TokenLimit as error:
            raise AuthError("token_limit", "") from error

    async def _grant(self, form: Mapping[str, str]) -> StoredToken:
        reply = await self._send("POST", "v2/oauth2/token.json", form={
            **form, "client_id": self._client_id, "client_secret": self._client_secret})
        data = reply.json()
        if reply.status == 403:
            raise _TokenLimit()
        if reply.status in (400, 401):
            raise AuthError(_token_error(reply), str(data.get("error_description") or data.get("message") or ""))
        _raise_for_status(reply)
        access = data.get("access_token")
        if not isinstance(access, str) or not access:
            raise PlatformError(BAD_RESPONSE, "no access_token in the token answer")
        refresh = data.get("refresh_token")
        lifetime = response_int(data.get("expires_in")) or DEFAULT_TOKEN_LIFETIME
        log.info("vk ads token received", extra={"grant": form["grant_type"]})
        return StoredToken(access, refresh if isinstance(refresh, str) and refresh else None,
                           self._clock.now() + timedelta(seconds=lifetime))

    async def _delete_tokens(self) -> None:
        reply = await self._send("POST", "v2/oauth2/token/delete.json", form={
            "client_id": self._client_id, "client_secret": self._client_secret})
        if reply.status >= 400:
            raise AuthError("token_limit", f"token/delete.json answered HTTP {reply.status}")

    async def _drop_token(self, *, expired_only: bool) -> None:
        """401 on a request: an expired token is refreshed, an unknown one replaced by a new pair."""
        async with self._token_lock:
            stored = await self._tokens.load()
            if stored is None:
                return
            if expired_only and stored.refresh_token:
                await self._tokens.save(StoredToken(stored.access_token, stored.refresh_token, self._clock.now()))
            else:
                await self._tokens.clear()


def _raise_for_status(reply: HttpReply) -> None:
    if reply.status < 400:
        return
    if reply.status == 429:
        raise RateLimited("VK Ads answered HTTP 429")
    if reply.status >= 500:
        raise Transient(f"VK Ads answered HTTP {reply.status}")
    error = reply.json().get("error")
    details = error if isinstance(error, dict) else {}
    code = str(details.get("code") or reply.status)
    message = str(details.get("message") or f"HTTP {reply.status}")
    if reply.status == 403:
        raise AuthError("forbidden", message)
    raise PlatformError(code, message)


def _token_error(reply: HttpReply) -> str:
    """The error code of a refused token: API calls answer {"code"}, the token endpoint may
    answer OAuth-style {"error": "invalid_client"} or VK-style {"error": {"code"}}."""
    data = reply.json()
    error = data.get("error")
    if isinstance(error, dict):
        error = error.get("code")
    return str(error or data.get("code") or "unauthorized")


def _items(data: Mapping[str, Any]) -> list[dict[str, Any]]:
    items = data.get("items")
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _decimal(value: Any) -> Decimal | None:
    """A finite amount from a lenient JSON value ("385.24", 385.24); None otherwise."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = Decimal(str(value))
    except ArithmeticError:
        return None
    return number if number.is_finite() else None
