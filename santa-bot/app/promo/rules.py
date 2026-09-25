"""The autopilot's decisions (PROMO_SPEC §5 and its Implementation notes): pure functions of the facts, no I/O.

Order of evaluation:
1. Season window: outside PROMO_START..PROMO_END every active campaign is paused (as waiting
   for the season before it, as over after it); nothing else is evaluated.
2. Blind budgets: an active campaign whose spend nothing visible limits is paused on its own;
   the others are judged without it.
3. Hard cap: when the gross spend so far plus one more day of every active campaign could
   cross the cap, every active campaign is paused, and those waiting for the season stay
   paused for the cap; nothing else is evaluated.
4. Season start: campaigns paused before the season start again, each only if the cap holds
   with it and its numbers are fresh; otherwise they stay paused for the cap (or a blind budget).
5. Expensive: a campaign whose matured spend reached the minimum and brought no game with
   3+ participants, or brought them dearer than the pause threshold, is paused.
6. Cheap: a campaign whose matured games cost less than the scale threshold gets a budget
   raise (at most +max_raise_pct, never past the cap), which an admin must approve.

Rules 1–3 are also the guard's (``protect``). Only raises need approval. A campaign an admin
paused is never touched by rules 5–6; raises and season starts need fresh numbers.
"Matured" numbers count only people first seen, and money spent, before the cohort cutoff
(today minus PROMO_LAG_DAYS), so a game has time to gather its participants.

One more day of spend (``day_exposure``) is the smallest of what is visible:
- VK's daily budget;
- Direct's weekly budget: a day may take 35% of the week plus the rest carried over from last
  week (at most 30% of it), so 45.5% when paying per click, and all of it when paying per
  conversion (all the conversions may come on one day). A change restarts the week, and on
  its day the old budget still counts: «35% от старого бюджета + 35% от нового», «100% старого
  + 100% нового» (yandex.ru/support/direct/ru/strategies/week-budget);
- a limit on total spend (VK's budget for the whole campaign, Direct's budget for a period):
  what is left of it.
With none of these the budget is blind. Money is gross integer kopecks.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import date, timedelta
from enum import StrEnum
from typing import Any

from app.core import texts
from app.core.dates import in_season_window
from app.promo.platforms import Budget, BudgetKind, CampaignState, Platform, SpendLimit

PER_CLICK_DAY_PERMILLE = 455  # Direct, paying per click: 35% of (the week + at most 30% carried over)
RAISE_STEP_KOP = 10_00  # raised budgets are rounded down to 10 ₽
MIN_RAISE_KOP = 50_00  # a smaller raise is not worth an admin's tap
MIN_GAMES_TO_RAISE = 2


class PausedBy(StrEnum):
    AUTOPILOT = "autopilot"
    ADMIN = "admin"
    CAP = "cap"
    SEASON = "season"  # the season is over
    PRESEASON = "preseason"  # the season has not started: started again when it does


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
    budget: Budget | None  # gross; the one the autopilot may change
    limit: SpendLimit | None  # a limit on total spend, gross
    pays_per_conversion: bool
    previous_budget_kop: int | None  # what was replaced on last_budget_change_day
    last_budget_change_day: date | None
    spend_total_kop: int
    spend_matured_kop: int
    spend_yesterday_kop: int
    spend_in_limit_kop: int  # the spend that counts against ``limit``
    games3: int
    games3_matured: int
    paid_rub: int
    paused_by: PausedBy | None
    fresh: bool  # the spend was fetched recently enough to raise a budget or start the campaign

    @property
    def active(self) -> bool:
        return self.state == CampaignState.ACTIVE

    @property
    def waiting_for_season(self) -> bool:
        return self.state == CampaignState.PAUSED and self.paused_by == PausedBy.PRESEASON


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
    """Rules 1–6 in order."""
    if not in_season(situation):
        return _season(campaigns, situation)
    blind = _blind(campaigns, situation)
    campaigns = _after(campaigns, blind)
    stops = _cap(campaigns, situation)
    if stops:
        return [*blind, *stops]
    starts = _season_start(campaigns, situation)
    campaigns = _after(campaigns, starts)
    expensive = _expensive(campaigns, situation)
    return [*blind, *starts, *expensive, *_cheap(_after(campaigns, expensive), situation)]


def protect(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    """Rules 1–3 only: the season window, blind budgets and the hard cap (the guard between daily runs)."""
    if not in_season(situation):
        return _season(campaigns, situation)
    blind = _blind(campaigns, situation)
    return [*blind, *_cap(_after(campaigns, blind), situation)]


def in_season(situation: Situation) -> bool:
    return in_season_window(situation.today, situation.season_start, situation.season_end)


def season_ended(situation: Situation) -> bool:
    """Outside the window: whether its end (rather than its start) is the boundary passed last."""
    mark = (situation.today.month, situation.today.day)
    start, end = _month_day(situation.season_start), _month_day(situation.season_end)
    return mark > end or start > end


def day_exposure(campaign: CampaignFacts, today: date) -> int | None:
    """The most a running campaign may spend in one more day; None when nothing visible limits it."""
    bounds = []
    if campaign.budget is not None:
        bounds.append(_budget_day(campaign, campaign.budget, today))
    if campaign.limit is not None:
        bounds.append(_limit_left(campaign.limit, campaign.spend_in_limit_kop, today))
    return min(bounds) if bounds else None


def budget_visible(campaign: CampaignFacts, today: date) -> bool:
    return day_exposure(campaign, today) is not None


def projected_kop(campaigns: Sequence[CampaignFacts], situation: Situation) -> int:
    """Spend so far plus one more day of every active campaign with a visible budget (blind ones are
    paused on their own by rule 2)."""
    days = (day_exposure(c, situation.today) for c in campaigns if c.active)
    return situation.spent_total_kop + sum(day for day in days if day is not None)


def cap_holds(campaigns: Sequence[CampaignFacts], situation: Situation) -> bool:
    return projected_kop(campaigns, situation) <= situation.cap_kop


def with_state(campaigns: Sequence[CampaignFacts], src: str, state: CampaignState) -> list[CampaignFacts]:
    """The facts as they would be after resuming (or pausing) a campaign."""
    return [replace(c, state=state) if c.src == src else c for c in campaigns]


def with_budget(campaigns: Sequence[CampaignFacts], src: str, budget: Budget, today: date) -> list[CampaignFacts]:
    """The facts as they would be after changing a budget today: the replaced one still counts today."""
    return [replace(c, budget=budget, previous_budget_kop=_replaced_today(c, today), last_budget_change_day=today)
            if c.src == src else c for c in campaigns]


# --- one more day ---------------------------------------------------------------------------------------


def _budget_day(campaign: CampaignFacts, budget: Budget, today: date) -> int:
    if budget.kind == BudgetKind.DAY:
        return budget.kop
    week = budget.kop + (campaign.previous_budget_kop or 0 if campaign.last_budget_change_day == today else 0)
    return week if campaign.pays_per_conversion else -(-week * PER_CLICK_DAY_PERMILLE // 1000)


def _limit_left(limit: SpendLimit, spent_kop: int, today: date) -> int:
    if limit.start is not None and limit.end is not None and not limit.start <= today <= limit.end:
        return limit.kop  # a period that is not the stored one: the whole limit
    return max(limit.kop - spent_kop, 0)


def _replaced_today(campaign: CampaignFacts, today: date) -> int:
    """The budgets that still count today once the current one is replaced."""
    earlier = campaign.previous_budget_kop or 0 if campaign.last_budget_change_day == today else 0
    return earlier + (campaign.budget.kop if campaign.budget else 0)


def _largest_budget(campaign: CampaignFacts, budget: Budget, room_kop: int) -> int:
    """The largest budget whose day — changed today, so the current one still counts — fits ``room_kop``."""
    if budget.kind == BudgetKind.DAY:
        return room_kop
    per_mille = 1000 if campaign.pays_per_conversion else PER_CLICK_DAY_PERMILLE
    return room_kop * 1000 // per_mille - budget.kop


# --- the rules ---------------------------------------------------------------------------------------------


def _season(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    ended = season_ended(situation)
    paused_by = PausedBy.SEASON if ended else PausedBy.PRESEASON
    reason = texts.promo_reason_season(ended=ended)
    return [_pause(c, paused_by, reason) for c in campaigns if c.active]


def _blind(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    return [_pause(c, PausedBy.AUTOPILOT, texts.PROMO_REASON_BLIND) for c in campaigns
            if c.active and not budget_visible(c, situation.today)]


def _cap(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    projected = projected_kop(campaigns, situation)
    if projected <= situation.cap_kop:
        return []
    reason = texts.promo_reason_cap(cap_kop=situation.cap_kop, spent_kop=situation.spent_total_kop,
                                    projected_kop=projected)
    return [_pause(c, PausedBy.CAP, reason) for c in campaigns if c.active or c.waiting_for_season]


def _season_start(campaigns: Sequence[CampaignFacts], situation: Situation) -> list[Decision]:
    """Campaigns paused before the season start again while the room under the cap lasts."""
    room = situation.cap_kop - projected_kop(campaigns, situation)
    decisions = []
    for c in campaigns:
        if not c.waiting_for_season:
            continue
        day = day_exposure(c, situation.today)
        if day is None:
            decisions.append(_pause(c, PausedBy.AUTOPILOT, texts.PROMO_REASON_BLIND))
        elif not c.fresh:
            continue  # started once its numbers are fresh again
        elif day <= room:
            decisions.append(Decision(c.src, ActionKind.RESUME, texts.PROMO_REASON_SEASON_STARTED))
            room -= day
        else:
            reason = texts.promo_reason_cap(cap_kop=situation.cap_kop, spent_kop=situation.spent_total_kop,
                                            projected_kop=situation.cap_kop - room + day)
            decisions.append(_pause(c, PausedBy.CAP, reason))
    return decisions


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
    rules, today = situation.thresholds, situation.today
    room = situation.cap_kop - projected_kop(campaigns, situation)
    decisions = []
    for c in _managed(campaigns):
        budget, day = c.budget, day_exposure(c, today)
        if budget is None or day is None or not c.fresh or c.last_budget_change_day == today:
            continue
        if c.games3_matured < MIN_GAMES_TO_RAISE or c.spend_matured_kop >= rules.scale_cpa_kop * c.games3_matured:
            continue
        keeps_cap = _largest_budget(c, budget, room + day)
        target = _raise_target(budget, keeps_cap, rules.max_raise_pct, situation.min_budget_kop)
        if target is None:
            continue
        reason = texts.promo_reason_cheap(until=_last_matured_day(situation), spend_kop=c.spend_matured_kop,
                                          games=c.games3_matured, scale_cpa_kop=rules.scale_cpa_kop)
        params = {"from_kop": budget.kop, "to_kop": target, "kind": budget.kind}
        decisions.append(Decision(c.src, ActionKind.SET_BUDGET, reason, params, needs_approval=True))
        (raised,) = with_budget([c], c.src, Budget(target, budget.kind), today)
        room -= (day_exposure(raised, today) or 0) - day
    return decisions


def _raise_target(budget: Budget, keeps_cap: int, max_raise_pct: int, min_budget_kop: int) -> int | None:
    """The raised budget: +max_raise_pct at most, at most ``keeps_cap``, rounded down to 10 ₽, not below
    the platform minimum; None below a 50 ₽ raise."""
    target = min(budget.kop * (100 + max_raise_pct) // 100, keeps_cap)
    target -= target % RAISE_STEP_KOP
    if target < min_budget_kop <= keeps_cap:
        target = min_budget_kop
    return target if target - budget.kop >= MIN_RAISE_KOP else None


def _managed(campaigns: Sequence[CampaignFacts]) -> list[CampaignFacts]:
    """Active campaigns the rules may act on: not the ones an admin paused."""
    return [c for c in campaigns if c.active and c.paused_by != PausedBy.ADMIN]


def _after(campaigns: Sequence[CampaignFacts], decisions: Sequence[Decision]) -> list[CampaignFacts]:
    """The facts as the pauses and starts of ``decisions`` leave them."""
    changes: dict[str, dict[str, Any]] = {}
    for decision in decisions:
        if decision.action == ActionKind.PAUSE:
            changes[decision.src] = {"state": CampaignState.PAUSED, "paused_by": decision.params["paused_by"]}
        elif decision.action == ActionKind.RESUME:
            changes[decision.src] = {"state": CampaignState.ACTIVE, "paused_by": None}
    return [replace(c, **changes[c.src]) if c.src in changes else c for c in campaigns]


def _pause(campaign: CampaignFacts, paused_by: PausedBy, reason: str) -> Decision:
    return Decision(campaign.src, ActionKind.PAUSE, reason, {"paused_by": paused_by})


def _last_matured_day(situation: Situation) -> date:
    return situation.cutoff - timedelta(days=1)


def _month_day(text: str) -> tuple[int, int]:
    month, day = text.split("-")
    return int(month), int(day)
