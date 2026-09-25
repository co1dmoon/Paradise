"""Yandex Direct API v501 client (PROMO_SPEC §3.1), checked against yandex.com/dev/direct on 2026-09-25.

- JSON endpoints ``https://api.direct.yandex.com/json/v501/{service}`` (unified performance
  campaigns need v501). The documented sandbox address is ``…/json/v5/``; whether it serves
  v501 is unverified, so the sandbox is called at v501 too and a failure says so instead of
  silently switching versions.
- Headers ``Authorization: Bearer <token>`` and ``Accept-Language: ru``; the ``Units``
  response header (points spent/left/limit) is logged at debug level.
- Request errors come back as ``{"error": {"error_code", "error_string", "error_detail"}}``:
  53 is an invalid token, 58 an unapproved API application, 54/513/3000 missing access;
  152 means no points and 506 too many connections; 52 and 1000–1002 are server errors.
  The token page of the same docs says an invalid token answers 1002 instead, so a 1002 that
  talks about the token counts as an authorization error too.
- ``campaigns.suspend``/``resume`` report per-object warnings 10020 "already suspended" and
  10021 "not suspended": both count as success.
- A unified campaign keeps its budget inside the strategy:
  ``UnifiedCampaign.BiddingStrategy.{Search|Network}.<Structure>.WeeklySpendLimit`` (micros,
  net of VAT). ``campaigns.update`` requires ``BiddingStrategyType`` in the strategy it
  changes, and omitted parameters keep their values, so a budget change sends the current
  strategy type with only the new ``WeeklySpendLimit``. A strategy with a budget for a period
  (``CustomPeriodBudget``) counts as having no weekly budget: it is neither read nor changed.
- Spend comes from the Reports service (CAMPAIGN_PERFORMANCE_REPORT, Date/CampaignId/Clicks/
  Cost, VAT included, TSV). HTTP 201/202 mean "queued": ask again after ``retryIn`` seconds
  with the same report name. Offline reports are kept for 5 hours, so every fetch gets a
  name of its own (dates, ids and the fetch time) and never reads a stale copy.

Mutating calls are never retried: a failure is reported and the next run decides again.
"""

from __future__ import annotations

import hashlib
import logging
import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.core.clock import Clock
from app.promo.platforms import (
    BAD_RESPONSE,
    NOT_FOUND,
    UNSUPPORTED_BUDGET,
    AdApiError,
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
    gross_kop,
    kop_from_rub,
    net_kop,
    response_int,
)

log = logging.getLogger(__name__)

LIVE_URL = "https://api.direct.yandex.com/json/v501/"
SANDBOX_URL = "https://api-sandbox.direct.yandex.com/json/v501/"
REPORT_WAIT_LIMIT = 300.0  # seconds spent waiting for an offline report at most
DEFAULT_RETRY_IN = 10
REPORT_TIMEOUT = 120.0
MICROS_PER_KOP = 10_000

_AUTH_CODES = frozenset({53, 54, 58, 513, 3000})
_RATE_CODES = frozenset({152, 506})
_SERVER_CODES = frozenset({52, 1000, 1001, 1002})
_ALREADY_IN_STATE = frozenset({10020, 10021})
_OPERATION_ERROR = 1002
_ABOUT_TOKEN = re.compile(r"токен|token", re.IGNORECASE)
_REPORT_FIELDS = ("Date", "CampaignId", "Clicks", "Cost")
_STATES = {"ON": CampaignState.ACTIVE, "SUSPENDED": CampaignState.PAUSED}
_UNIFIED = "UNIFIED_CAMPAIGN"
# Strategies whose weekly budget can be changed by sending WeeklySpendLimit alone.
_STRATEGY_STRUCTURES = {
    "WB_MAXIMUM_CLICKS": "WbMaximumClicks",
    "WB_MAXIMUM_CONVERSION_RATE": "WbMaximumConversionRate",
    "AVERAGE_CPC": "AverageCpc",
    "AVERAGE_CPA": "AverageCpa",
    "PAY_FOR_CONVERSION": "PayForConversion",
}


class DirectClient:
    platform = Platform.DIRECT

    def __init__(
        self,
        token: str,
        *,
        vat_pct: int,
        clock: Clock,
        base_url: str = LIVE_URL,
        http: HttpClient | None = None,
        report_wait_limit: float = REPORT_WAIT_LIMIT,
    ) -> None:
        self._token = token
        self._vat = vat_pct
        self._clock = clock
        self._base = base_url.rstrip("/") + "/"
        self._http = http or HttpClient()
        self._report_wait_limit = report_wait_limit

    async def close(self) -> None:
        await self._http.close()

    # --- campaigns ---------------------------------------------------------------------------------

    async def list_campaigns(self, ids: Sequence[str]) -> list[CampaignInfo]:
        if not ids:
            return []
        items = await self._get_campaigns(ids)
        return [self._campaign(item) for item in items]

    async def suspend(self, campaign_id: str) -> None:
        await self._change_state("suspend", "SuspendResults", campaign_id)

    async def resume(self, campaign_id: str) -> None:
        await self._change_state("resume", "ResumeResults", campaign_id)

    async def set_budget(self, campaign_id: str, gross_kop: int, kind: BudgetKind) -> None:
        """Set the weekly budget of a unified campaign (the only budget its strategies have)."""
        if kind != BudgetKind.WEEK:
            raise PlatformError(UNSUPPORTED_BUDGET, "a unified campaign has a weekly budget only")
        items = await self._get_campaigns([campaign_id])
        if not items:
            raise PlatformError(NOT_FOUND, f"campaign {campaign_id}")
        place = _weekly_budget(items[0])
        if place is None:
            raise PlatformError(UNSUPPORTED_BUDGET, "the campaign's strategy has no weekly budget to change")
        micros = net_kop(gross_kop, self._vat) * MICROS_PER_KOP
        strategy = {place.part: {"BiddingStrategyType": place.strategy_type,
                                 place.structure: {"WeeklySpendLimit": micros}}}
        result = await self._call("campaigns", "update", {
            "Campaigns": [{"Id": int(campaign_id), "UnifiedCampaign": {"BiddingStrategy": strategy}}],
        })
        _check_action_results(result, "UpdateResults")

    async def _get_campaigns(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        result = await self._call("campaigns", "get", {
            "SelectionCriteria": {"Ids": [int(campaign_id) for campaign_id in ids]},
            "FieldNames": ["Id", "Name", "Type", "State", "Status"],
            "UnifiedCampaignFieldNames": ["BiddingStrategy"],
        })
        items = result.get("Campaigns")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _campaign(self, item: Mapping[str, Any]) -> CampaignInfo:
        place = _weekly_budget(item)
        budget = None
        if place is not None:
            budget = Budget(gross_kop(_kop_from_micros(place.micros), self._vat), BudgetKind.WEEK)
        return CampaignInfo(
            id=str(item.get("Id", "")),
            name=str(item.get("Name") or ""),
            state=_STATES.get(str(item.get("State")), CampaignState.OTHER),
            budget=budget,
        )

    async def _change_state(self, method: str, results_key: str, campaign_id: str) -> None:
        result = await self._call("campaigns", method, {"SelectionCriteria": {"Ids": [int(campaign_id)]}})
        _check_action_results(result, results_key)

    # --- spend ------------------------------------------------------------------------------------------

    async def daily_spend(self, ids: Sequence[str], date_from: date, date_to: date) -> SpendByDay:
        if not ids:
            return {}
        body = {"params": {
            "SelectionCriteria": {
                "DateFrom": date_from.isoformat(),
                "DateTo": date_to.isoformat(),
                "Filter": [{"Field": "CampaignId", "Operator": "IN", "Values": [str(i) for i in ids]}],
            },
            "FieldNames": list(_REPORT_FIELDS),
            "ReportName": self._report_name(ids, date_from, date_to),
            "ReportType": "CAMPAIGN_PERFORMANCE_REPORT",
            "DateRangeType": "CUSTOM_DATE",
            "Format": "TSV",
            "IncludeVAT": "YES",
        }}
        headers = {
            **self._headers(),
            "processingMode": "auto",
            "returnMoneyInMicros": "false",
            "skipReportHeader": "true",
            "skipReportSummary": "true",
        }
        waited = 0.0
        while True:
            reply = await self._http.request("POST", self._base + "reports", headers=headers, json_body=body,
                                             timeout=REPORT_TIMEOUT)
            _log_units(reply)
            if reply.status == 200:
                return _parse_report(reply.text)
            if reply.status not in (201, 202):
                raise _reply_error(reply)
            delay = max(1, response_int(reply.headers.get("retryIn")) or DEFAULT_RETRY_IN)
            if waited + delay > self._report_wait_limit:
                raise Transient(f"the spend report was not ready after {int(waited)} s")
            await self._clock.sleep(delay)
            waited += delay

    def _report_name(self, ids: Sequence[str], date_from: date, date_to: date) -> str:
        """Unique per request parameters and per fetch, stable while one fetch waits for it."""
        digest = hashlib.sha1(",".join(sorted(ids)).encode()).hexdigest()[:10]
        stamp = self._clock.now().strftime("%Y%m%dT%H%M%S")
        return f"santa-spend {date_from.isoformat()}..{date_to.isoformat()} {digest} {stamp}"

    # --- transport ----------------------------------------------------------------------------------------

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._token}", "Accept-Language": "ru"}

    async def _call(self, service: str, method: str, params: Mapping[str, Any]) -> dict[str, Any]:
        reply = await self._http.request(
            "POST", self._base + service, headers=self._headers(), json_body={"method": method, "params": params}
        )
        _log_units(reply)
        data = reply.json()
        if "error" in data or reply.status != 200:
            raise _reply_error(reply)
        result = data.get("result")
        if not isinstance(result, dict):
            raise PlatformError(BAD_RESPONSE, f"unexpected answer to {service}.{method}")
        return result


@dataclass(frozen=True, slots=True)
class _WeeklyBudget:
    """Where a unified campaign keeps its weekly budget."""

    part: str  # Search or Network
    strategy_type: str  # e.g. WB_MAXIMUM_CLICKS
    structure: str  # e.g. WbMaximumClicks
    micros: int  # WeeklySpendLimit, net of VAT


def _weekly_budget(item: Mapping[str, Any]) -> _WeeklyBudget | None:
    """The weekly budget of a unified campaign's strategy; None without one (e.g. a period budget)."""
    if item.get("Type", _UNIFIED) != _UNIFIED:
        return None
    unified = item.get("UnifiedCampaign")
    strategy = unified.get("BiddingStrategy") if isinstance(unified, dict) else None
    if not isinstance(strategy, dict):
        return None
    for part in ("Search", "Network"):
        entry = strategy.get(part)
        if not isinstance(entry, dict):
            continue
        strategy_type = str(entry.get("BiddingStrategyType"))
        structure = _STRATEGY_STRUCTURES.get(strategy_type)
        settings = entry.get(structure) if structure else None
        if structure is None or not isinstance(settings, dict) or _has_period_budget(settings):
            continue
        limit = response_int(settings.get("WeeklySpendLimit"))
        if limit:
            return _WeeklyBudget(part, strategy_type, structure, limit)
    return None


def _has_period_budget(settings: Mapping[str, Any]) -> bool:
    """A budget for a period (CustomPeriodBudget) limits the strategy; its WeeklySpendLimit may not."""
    period = settings.get("CustomPeriodBudget")
    return settings.get("BudgetType") == "CUSTOM_PERIOD_BUDGET" or (
        isinstance(period, dict) and bool(response_int(period.get("SpendLimit")))
    )


def _kop_from_micros(micros: int) -> int:
    return (micros + MICROS_PER_KOP // 2) // MICROS_PER_KOP


def _check_action_results(result: Mapping[str, Any], key: str) -> None:
    """Per-object errors of suspend/resume/update; "already in that state" warnings are success."""
    items = result.get(key)
    if not isinstance(items, list) or not items:
        raise PlatformError(BAD_RESPONSE, f"no {key} in the answer")
    for item in items:
        errors = item.get("Errors") if isinstance(item, dict) else None
        if errors:
            first = errors[0] if isinstance(errors[0], dict) else {}
            raise PlatformError(response_int(first.get("Code")) or "error", _notification_text(first))
        for warning in (item.get("Warnings") or []) if isinstance(item, dict) else []:
            if isinstance(warning, dict) and response_int(warning.get("Code")) not in _ALREADY_IN_STATE:
                log.warning("direct warning",
                            extra={"code": warning.get("Code"), "detail": _notification_text(warning)})


def _notification_text(item: Mapping[str, Any]) -> str:
    return " ".join(str(item[key]) for key in ("Message", "Details") if item.get(key))


def _reply_error(reply: HttpReply) -> AdApiError:
    """Map an error answer (JSON ``error`` object or a bare HTTP status) to the promo error classes."""
    error = reply.json().get("error")
    if isinstance(error, dict):
        code = response_int(error.get("error_code"))
        detail = " ".join(str(error[key]) for key in ("error_string", "error_detail") if error.get(key))
        if code in _AUTH_CODES or (code == _OPERATION_ERROR and _ABOUT_TOKEN.search(detail)):
            return AuthError(str(code), detail)
        if code in _RATE_CODES:
            return RateLimited(f"{code}: {detail}")
        if code in _SERVER_CODES or reply.status >= 500:
            return Transient(f"{code}: {detail}")
        return PlatformError(code if code is not None else reply.status, detail)
    if reply.status == 429:
        return RateLimited(f"HTTP {reply.status}")
    if reply.status >= 500:
        return Transient(f"HTTP {reply.status}")
    if reply.status in (401, 403):
        return AuthError(str(reply.status), "HTTP")
    return PlatformError(reply.status, reply.text[:200] or f"HTTP {reply.status}")


def _parse_report(text: str) -> SpendByDay:
    """TSV rows under a header line with the field names; '--' means no value."""
    lines = [line for line in text.splitlines() if line.strip()]
    header_at = next((i for i, line in enumerate(lines) if set(_REPORT_FIELDS) <= set(line.split("\t"))), None)
    if header_at is None:
        if not lines:
            return {}
        raise PlatformError(BAD_RESPONSE, "the report has no header with Date, CampaignId, Clicks and Cost")
    columns = {name: index for index, name in enumerate(lines[header_at].split("\t"))}
    spend: SpendByDay = {}
    for line in lines[header_at + 1:]:
        cells = line.split("\t")
        if len(cells) < len(columns):
            continue  # the summary line, when the server keeps it
        try:
            key = (cells[columns["CampaignId"]], date.fromisoformat(cells[columns["Date"]]))
            clicks = _number(cells[columns["Clicks"]], int)
            cost = _number(cells[columns["Cost"]], kop_from_rub)
        except ValueError as error:
            raise PlatformError(BAD_RESPONSE, f"unreadable row: {line[:80]!r}") from error
        previous = spend.get(key, SpendRow(0, 0))
        spend[key] = SpendRow(previous.cost_kop + cost, previous.clicks + clicks)
    return spend


def _number(cell: str, parse: Callable[[str], int]) -> int:
    cell = cell.strip()
    return 0 if cell in ("", "--") else int(parse(cell))


def _log_units(reply: HttpReply) -> None:
    units = reply.headers.get("Units")
    if units:
        log.debug("direct units", extra={"units": units})
