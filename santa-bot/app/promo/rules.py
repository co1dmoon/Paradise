"""The autopilot's decisions (PROMO_SPEC §5): pure functions of the facts, no I/O.

Order of evaluation:
1. Season window: outside PROMO_START..PROMO_END every active campaign is paused; nothing
   else is evaluated.
2. Hard cap: when the gross spend so far plus one more day of the active budgets could
   cross the cap, every active campaign is paused; nothing else is evaluated.
3. Expensive: a campaign whose matured spend reached the minimum and brought no game with
   3+ participants, or brought them dearer than the pause threshold, is paused.
4. Cheap: a campaign whose matured games cost less than the scale threshold gets a budget
   raise (at most +max_raise_pct, never past the cap), which an admin must approve.

Pauses (1–3) need no approval. A campaign an admin paused is never touched by rules 3–4.
"Matured" numbers count only people first seen, and money spent, before the cohort
cutoff (today minus PROMO_LAG_DAYS), so a game has time to gather its participants.
Money is gross integer kopecks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from app.core import texts
from app.core.dates import in_season_window
from app.promo.platforms import Budget, BudgetKind, CampaignState, Platform

DIRECT_DAY_SHARE_PCT = 35  # Direct may spend up to 35% of a weekly budget in one day
RAISE_STEP_KOP = 10_00  # raised budgets are rounded down to 10 ₽
MIN_RAISE_KOP = 50_00  # a smaller raise is not worth an admin's tap
MIN_GAMES_TO_RAISE = 2


class PausedBy(StrEnum):
    AUTOPILOT = "autopilot"
    ADMIN = "admin"
    CAP = "cap"
    SEASON = "season"


class ActionKind(StrEnum):
    PAUSE = "pause"
    RESUME = "resume"
    SET_BUDGET = "set_budget"
    SUSPEND_ALL = "suspend_all"


@dataclass(frozen=True, slots=True)
class CampaignFacts:
    src: str
    platform: Platform
    name: str
    state: CampaignState
    budget: Budget | None  # gross; None when the platform shows none
    plan_budget: Budget | None  # the budget when the campaign was registered
    spend_total_kop: int
    spend_matured_kop: int
    spend_yesterday_kop: int
    games3: int
    games3_matured: int
    paid_rub: int
    last_budget_change_day: date | None
    paused_by: PausedBy | None

    @property
    def active(self) -> bool:
        return self.state == CampaignState.ACTIVE


@dataclass(frozen=True, slots=True)
class Thresholds:
    pause_cpa_kop: int
    scale_cpa_kop: int
    min_spend_kop: int
    max_raise_pct: int


@dataclass(frozen=True, slots=True)
class Situation:
    today: date  # Moscow
    season_start: str  # 'MM-DD'
    season_end: str
    cap_kop: int
    spent_total_kop: int  # all campaigns, gross
    cutoff: date  # matured numbers count days before this date
    thresholds: Thresholds
    min_budget_kop: int  # the platforms' minimum budget, gross


@dataclass(frozen=True, slots=True)
class Decision:
    src: str
    action: ActionKind
    reason: str
    params: Mapping[str, Any] = field(default_factory=dict)
    needs_approval: bool = False


def decide(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    """Rules 1–4 in order."""
    stops = protect(campaigns, situation)
    if stops:
        return stops
    pauses = _expensive(campaigns, situation)
    paused = {decision.src for decision in pauses}
    return [*pauses, *_cheap([c for c in campaigns if c.src not in paused], situation)]


def protect(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    """Rules 1–2 only: the season window and the hard cap (the guard between daily runs)."""
    return _season(campaigns, situation) or _cap(campaigns, situation)


def in_season(situation: Situation) -> bool:
    return in_season_window(situation.today, situation.season_start, situation.season_end)


def season_ended(situation: Situation) -> bool:
    """Outside the window: whether its end (rather than its start) is the boundary passed last."""
    mark = (situation.today.month, situation.today.day)
    start, end = _month_day(situation.season_start), _month_day(situation.season_end)
    return mark > end or start > end


def day_equivalent(budget: Budget) -> int:
    """One day of a budget: a daily budget itself, a weekly one divided by 7 (rounded up)."""
    return budget.kop if budget.kind == BudgetKind.DAY else -(-budget.kop // 7)


def day_spend_kop(campaign: CampaignFacts, remaining_kop: int) -> int:
    """What an active campaign may spend in one more day.

    With an unknown budget: 35% of the weekly budget it had when registered (Direct), its
    daily budget then (VK), and when nothing is known, all that is left under the cap.
    """
    if campaign.budget is not None:
        return day_equivalent(campaign.budget)
    plan = campaign.plan_budget
    if plan is None:
        return max(remaining_kop, 1)  # an active campaign can always spend something
    if campaign.platform == Platform.DIRECT and plan.kind == BudgetKind.WEEK:
        return -(-plan.kop * DIRECT_DAY_SHARE_PCT // 100)
    return day_equivalent(plan)


def projected_kop(campaigns: Sequence[CampaignFacts], situation: Situation) -> int:
    """Spend so far plus one more day of every active campaign."""
    remaining = situation.cap_kop - situation.spent_total_kop
    return situation.spent_total_kop + sum(day_spend_kop(c, remaining) for c in campaigns if c.active)


def cap_holds(campaigns: Sequence[CampaignFacts], situation: Situation) -> bool:
    return projected_kop(campaigns, situation) <= situation.cap_kop


def with_change(
    campaigns: Sequence[CampaignFacts], src: str, *, state: CampaignState | None = None, budget: Budget | None = None
) -> list[CampaignFacts]:
    """The facts as they would be after resuming a campaign or changing its budget (for cap checks)."""
    changes: dict[str, Any] = {}
    if state is not None:
        changes["state"] = state
    if budget is not None:
        changes["budget"] = budget
    return [replace(c, **changes) if c.src == src else c for c in campaigns]


# --- the rules -----------------------------------------------------------------------------------------


def _season(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    if in_season(situation):
        return []
    reason = texts.promo_reason_season(ended=season_ended(situation))
    return [_pause(c, PausedBy.SEASON, reason) for c in campaigns if c.active]


def _cap(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    projected = projected_kop(campaigns, situation)
    if projected <= situation.cap_kop:
        return []
    reason = texts.promo_reason_cap(cap_kop=situation.cap_kop, spent_kop=situation.spent_total_kop,
                                    projected_kop=projected)
    return [_pause(c, PausedBy.CAP, reason) for c in campaigns if c.active]


def _expensive(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    rules = situation.thresholds
    decisions = []
    for c in _managed(campaigns):
        if c.spend_matured_kop < rules.min_spend_kop:
            continue
        if c.games3_matured and c.spend_matured_kop <= rules.pause_cpa_kop * c.games3_matured:
            continue
        reason = texts.promo_reason_expensive(until=_last_matured_day(situation), spend_kop=c.spend_matured_kop,
                                              games=c.games3_matured, pause_cpa_kop=rules.pause_cpa_kop)
        decisions.append(_pause(c, PausedBy.AUTOPILOT, reason))
    return decisions


def _cheap(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    """Raises for cheap campaigns; each one takes its share of the room under the cap in turn."""
    rules = situation.thresholds
    room = situation.cap_kop - projected_kop(campaigns, situation)
    decisions = []
    for c in _managed(campaigns):
        budget = c.budget
        if budget is None or c.last_budget_change_day == situation.today:
            continue
        if c.games3_matured < MIN_GAMES_TO_RAISE or c.spend_matured_kop >= rules.scale_cpa_kop * c.games3_matured:
            continue
        target = _raise_target(budget, room + day_equivalent(budget), rules.max_raise_pct, situation.min_budget_kop)
        if target is None:
            continue
        reason = texts.promo_reason_cheap(until=_last_matured_day(situation), spend_kop=c.spend_matured_kop,
                                          games=c.games3_matured, scale_cpa_kop=rules.scale_cpa_kop)
        params = {"from_kop": budget.kop, "to_kop": target, "kind": budget.kind}
        decisions.append(Decision(c.src, ActionKind.SET_BUDGET, reason, params, needs_approval=True))
        room -= day_equivalent(Budget(target, budget.kind)) - day_equivalent(budget)
    return decisions


def _raise_target(budget: Budget, room_kop: int, max_raise_pct: int, min_budget_kop: int) -> int | None:
    """The raised budget: +max_raise_pct at most, within the cap (``room_kop`` is one day of room for
    this campaign), rounded down to 10 ₽, not below the platform minimum; None below a 50 ₽ raise."""
    keeps_cap = room_kop if budget.kind == BudgetKind.DAY else room_kop * 7
    target = min(budget.kop * (100 + max_raise_pct) // 100, keeps_cap)
    target -= target % RAISE_STEP_KOP
    if target < min_budget_kop <= keeps_cap:
        target = min_budget_kop
    return target if target - budget.kop >= MIN_RAISE_KOP else None


def _managed(campaigns: Sequence[CampaignFacts]) -> list[CampaignFacts]:
    """Active campaigns the rules may act on: not the ones an admin paused."""
    return [c for c in campaigns if c.active and c.paused_by != PausedBy.ADMIN]


def _pause(campaign: CampaignFacts, paused_by: PausedBy, reason: str) -> Decision:
    return Decision(campaign.src, ActionKind.PAUSE, reason, {"paused_by": paused_by})


def _last_matured_day(situation: Situation) -> date:
    return situation.cutoff - timedelta(days=1)


def _month_day(text: str) -> tuple[int, int]:
    month, day = text.split("-")
    return int(month), int(day)
