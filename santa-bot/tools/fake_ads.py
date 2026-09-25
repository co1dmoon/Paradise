"""FakeAdPlatform: an in-memory ad platform (``app.promo.platforms.AdPlatform``) for tests.

It keeps campaigns and daily spend, applies suspend/resume/budget changes to them,
records every call in order and can fail on demand:

    direct = FakeAdPlatform(Platform.DIRECT)
    direct.add("701", "Поиск", budget=Budget(300_000, BudgetKind.WEEK))
    direct.spend_on("701", date(2026, 11, 28), 12_000)
    direct.fail("suspend", Transient("down"))
    ctx.ad_platforms[Platform.DIRECT] = direct

The HTTP clients themselves are tested against aiohttp test servers (tests/test_promo_direct.py
and tests/test_promo_vkads.py).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from datetime import date

from app.promo.platforms import (
    AdApiError,
    Budget,
    BudgetKind,
    CampaignInfo,
    CampaignState,
    Platform,
    SpendByDay,
    SpendRow,
)

READS = frozenset({"list_campaigns", "daily_spend"})


class FakeAdPlatform:
    def __init__(self, platform: Platform) -> None:
        self.platform = platform
        self.campaigns: dict[str, CampaignInfo] = {}
        self.spend: SpendByDay = {}
        self.calls: list[tuple[object, ...]] = []
        self._failures: dict[str, list[AdApiError]] = {}

    # --- setup --------------------------------------------------------------------------------------

    def add(self, campaign_id: str, name: str, *, state: CampaignState = CampaignState.ACTIVE,
            budget: Budget | None = None) -> None:
        self.campaigns[campaign_id] = CampaignInfo(campaign_id, name, state, budget)

    def spend_on(self, campaign_id: str, day: date, cost_kop: int, clicks: int = 10) -> None:
        self.spend[(campaign_id, day)] = SpendRow(cost_kop, clicks)

    def fail(self, method: str, error: AdApiError, times: int = 1) -> None:
        """The next ``times`` calls of ``method`` raise ``error``."""
        self._failures.setdefault(method, []).extend([error] * times)

    def mutations(self) -> list[tuple[object, ...]]:
        """The calls that change something on the platform (suspend, resume, set_budget)."""
        return [call for call in self.calls if call[0] not in READS]

    # --- AdPlatform ---------------------------------------------------------------------------------------

    async def list_campaigns(self, ids: Sequence[str]) -> list[CampaignInfo]:
        self._call("list_campaigns", tuple(ids))
        return [self.campaigns[campaign_id] for campaign_id in ids if campaign_id in self.campaigns]

    async def daily_spend(self, ids: Sequence[str], date_from: date, date_to: date) -> SpendByDay:
        self._call("daily_spend", tuple(ids), date_from, date_to)
        return {key: row for key, row in self.spend.items() if key[0] in ids and date_from <= key[1] <= date_to}

    async def suspend(self, campaign_id: str) -> None:
        self._call("suspend", campaign_id)
        self._set(campaign_id, state=CampaignState.PAUSED)

    async def resume(self, campaign_id: str) -> None:
        self._call("resume", campaign_id)
        self._set(campaign_id, state=CampaignState.ACTIVE)

    async def set_budget(self, campaign_id: str, gross_kop: int, kind: BudgetKind) -> None:
        self._call("set_budget", campaign_id, gross_kop, kind)
        self._set(campaign_id, budget=Budget(gross_kop, kind))

    async def close(self) -> None:
        """Nothing to release."""

    # --- internals -----------------------------------------------------------------------------------------

    def _call(self, method: str, *args: object) -> None:
        self.calls.append((method, *args))
        queued = self._failures.get(method)
        if queued:
            raise queued.pop(0)

    def _set(self, campaign_id: str, **changes: object) -> None:
        if campaign_id in self.campaigns:
            self.campaigns[campaign_id] = replace(self.campaigns[campaign_id], **changes)  # type: ignore[arg-type]
