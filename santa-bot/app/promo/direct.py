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
  10021 "not suspended": both count as success. Any other warning of a change (e.g. 10163
  «Настройка не будет изменена», 10165 «Параметр не будет применен») means it may not have
  happened, so the call fails.
- A unified campaign keeps its budget inside the strategy of one part (``Search`` or
  ``Network``; ``SERVING_OFF`` and ``NETWORK_DEFAULT`` carry none):
  ``UnifiedCampaign.BiddingStrategy.<part>.<Structure>.WeeklySpendLimit`` (micros, net of VAT),
  or a budget for a period, ``CustomPeriodBudget {SpendLimit, StartDate, EndDate}`` (a
  campaign is created with one or the other). The period budget is read as a limit on total
  spend; only the weekly one is changed. ``campaigns.update`` requires ``BiddingStrategyType``
  in the part it changes, and omitted parameters keep their values, so a change sends the
  current type with only the new ``WeeklySpendLimit`` and then reads the budget back. A
  strategy the client does not know, or budgets in both parts, leave the budget unknown.
- Pay-per-conversion strategies (PAY_FOR_CONVERSION, PAY_FOR_CONVERSION_CRR) are flagged:
  Yandex may charge a whole week's budget on the day all the conversions come
  (yandex.ru/support/direct/ru/strategies/week-budget).
- Spend comes from the Reports service (CAMPAIGN_PERFORMANCE_REPORT, Date/CampaignId/Clicks/
  Cost, VAT included, TSV). HTTP 201/202 mean "queued": ask again after ``retryIn`` seconds
  with the same report name, within 5 minutes in all, request time included. Offline
  reports are kept for 5 hours, so every fetch gets a name of its own (dates, ids and the
  fetch time) and never reads a stale copy.

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
    NOT_APPLIED,
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
    SpendLimit,
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
REPORT_WAIT_LIMIT = 300.0  # seconds a spend report may take at most, waiting and asking included
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
_NO_BUDGET_PARTS = frozenset({"SERVING_OFF", "NETWORK_DEFAULT"})
# Strategies with a weekly budget or a budget for a period: their structure and whether they charge per conversion.
_STRATEGIES = {
    "WB_MAXIMUM_CLICKS": ("WbMaximumClicks", False),
    "WB_MAXIMUM_CONVERSION_RATE": ("WbMaximumConversionRate", False),
    "AVERAGE_CPC": ("AverageCpc", False),
    "AVERAGE_CPA": ("AverageCpa", False),
    "AVERAGE_CRR": ("AverageCrr", False),
    "PAY_FOR_CONVERSION": ("PayForConversion", True),
    "PAY_FOR_CONVERSION_CRR": ("PayForConversionCrr", True),
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
        """Set the weekly budget of a unified campaign, then read it back."""
        if kind != BudgetKind.WEEK:
            raise PlatformError(UNSUPPORTED_BUDGET, "a unified campaign has a weekly budget only")
        strategy = await self._strategy_of(campaign_id)
        if strategy is None or strategy.weekly_micros is None:
            raise PlatformError(UNSUPPORTED_BUDGET, "the campaign's strategy has no weekly budget to change")
        micros = net_kop(gross_kop, self._vat) * MICROS_PER_KOP
        part = {strategy.part: {"BiddingStrategyType": strategy.strategy_type,
                                strategy.structure: {"WeeklySpendLimit": micros}}}
        result = await self._call("campaigns", "update", {
            "Campaigns": [{"Id": int(campaign_id), "UnifiedCampaign": {"BiddingStrategy": part}}],
        })
        _check_action_results(result, "UpdateResults", tolerated=frozenset())
        after = await self._strategy_of(campaign_id)
        if after is None or after.weekly_micros != micros:
            raise PlatformError(NOT_APPLIED, "the weekly budget read back is not the one sent")

    async def _strategy_of(self, campaign_id: str) -> _Strategy | None:
        items = await self._get_campaigns([campaign_id])
        if not items:
            raise PlatformError(NOT_FOUND, f"campaign {campaign_id}")
        return _strategy(items[0])

    async def _get_campaigns(self, ids: Sequence[str]) -> list[dict[str, Any]]:
        result = await self._call("campaigns", "get", {
            "SelectionCriteria": {"Ids": [int(campaign_id) for campaign_id in ids]},
            "FieldNames": ["Id", "Name", "Type", "State", "Status"],
            "UnifiedCampaignFieldNames": ["BiddingStrategy"],
        })
        items = result.get("Campaigns")
        return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []

    def _campaign(self, item: Mapping[str, Any]) -> CampaignInfo:
        strategy = _strategy(item)
        budget = limit = None
        if strategy is not None and strategy.weekly_micros is not None:
            budget = Budget(self._gross(strategy.weekly_micros), BudgetKind.WEEK)
        if strategy is not None and strategy.period is not None:
            micros, start, end = strategy.period
            limit = SpendLimit(self._gross(micros), start, end)
        return CampaignInfo(
            id=str(item.get("Id", "")),
            name=str(item.get("Name") or ""),
            state=_STATES.get(str(item.get("State")), CampaignState.OTHER),
            budget=budget,
            limit=limit,
            pays_per_conversion=strategy is not None and strategy.pays_per_conversion,
        )

    def _gross(self, micros: int) -> int:
        return gross_kop((micros + MICROS_PER_KOP // 2) // MICROS_PER_KOP, self._vat)

    async def _change_state(self, method: str, results_key: str, campaign_id: str) -> None:
        result = await self._call("campaigns", method, {"SelectionCriteria": {"Ids": [int(campaign_id)]}})
        _check_action_results(result, results_key, tolerated=_ALREADY_IN_STATE)

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
        deadline = self._clock.monotonic() + self._report_wait_limit
        while True:
            left = deadline - self._clock.monotonic()
            if left <= 0:
                raise Transient(f"the spend report was not ready within {int(self._report_wait_limit)} s")
            reply = await self._http.request("POST", self._base + "reports", headers=headers, json_body=body,
                                             timeout=min(REPORT_TIMEOUT, left))
            _log_units(reply)
            if reply.status == 200:
                return _parse_report(reply.text)
            if reply.status not in (201, 202):
                raise _reply_error(reply)
            delay = max(1, response_int(reply.headers.get("retryIn")) or DEFAULT_RETRY_IN)
            if self._clock.monotonic() + delay >= deadline:
                raise Transient(f"the spend report was not ready within {int(self._report_wait_limit)} s")
            await self._clock.sleep(delay)

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
class _Strategy:
    """The one part of a unified campaign's strategy that carries its budget."""

    part: str  # Search or Network
    strategy_type: str  # e.g. WB_MAXIMUM_CLICKS
    structure: str  # e.g. WbMaximumClicks
    pays_per_conversion: bool
    weekly_micros: int | None  # WeeklySpendLimit, net of VAT
    period: tuple[int, date, date] | None  # CustomPeriodBudget: SpendLimit (net micros), StartDate, EndDate


def _strategy(item: Mapping[str, Any]) -> _Strategy | None:
    """The budgeted part of a unified campaign's strategy; None when there is none, one the client
    cannot read, or budgets in both parts."""
    if item.get("Type", _UNIFIED) != _UNIFIED:
        return None
    unified = item.get("UnifiedCampaign")
    strategy = unified.get("BiddingStrategy") if isinstance(unified, dict) else None
    if not isinstance(strategy, dict):
        return None
    found = []
    for part in ("Search", "Network"):
        entry = strategy.get(part)
        if not isinstance(entry, dict) or entry.get("BiddingStrategyType") in _NO_BUDGET_PARTS:
            continue
        budgeted = _budgeted_part(part, entry)
        if budgeted is None:
            return None
        found.append(budgeted)
    return found[0] if len(found) == 1 else None


def _budgeted_part(part: str, entry: Mapping[str, Any]) -> _Strategy | None:
    strategy_type = str(entry.get("BiddingStrategyType"))
    known = _STRATEGIES.get(strategy_type)
    settings = entry.get(known[0]) if known else None
    if known is None or not isinstance(settings, dict):
        return None
    structure, per_conversion = known
    period = settings.get("CustomPeriodBudget")
    period_micros = response_int(period.get("SpendLimit")) if isinstance(period, dict) else None
    if period_micros or settings.get("BudgetType") == "CUSTOM_PERIOD_BUDGET":
        dates = _period_dates(period)
        if not period_micros or dates is None:
            return None  # a budget for a period that cannot be read
        return _Strategy(part, strategy_type, structure, per_conversion, None, (period_micros, *dates))
    weekly = response_int(settings.get("WeeklySpendLimit"))
    return _Strategy(part, strategy_type, structure, per_conversion, weekly, None) if weekly else None


def _period_dates(period: Any) -> tuple[date, date] | None:
    try:
        return date.fromisoformat(str(period.get("StartDate"))), date.fromisoformat(str(period.get("EndDate")))
    except (AttributeError, ValueError):
        return None


def _check_action_results(result: Mapping[str, Any], key: str, *, tolerated: frozenset[int]) -> None:
    """Per-object errors and warnings of suspend/resume/update; only the ``tolerated`` warnings pass."""
    items = result.get(key)
    if not isinstance(items, list) or not items:
        raise PlatformError(BAD_RESPONSE, f"no {key} in the answer")
    for item in items:
        if not isinstance(item, dict):
            raise PlatformError(BAD_RESPONSE, f"unreadable {key}")
        for note in [*(item.get("Errors") or []), *(item.get("Warnings") or [])]:
            details = note if isinstance(note, dict) else {}
            code = response_int(details.get("Code"))
            if code not in tolerated:
                raise PlatformError(code or "error", _notification_text(details))


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
