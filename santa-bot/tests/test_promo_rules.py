"""PROMO_SPEC §5 (and its Implementation notes): the rules engine is pure — every rule, their order,
one more day of each kind of budget, and the money edge cases."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.core import texts
from app.promo import rules
from app.promo.platforms import Budget, BudgetKind, CampaignState, Platform, SpendLimit, minimum_gross_kop
from app.promo.rules import ActionKind, CampaignFacts, Decision, PausedBy, Situation, Thresholds

TODAY = date(2026, 11, 30)
WEEK = BudgetKind.WEEK
DAY = BudgetKind.DAY
PAUSED = CampaignState.PAUSED


def facts(src: str = "yd701", **changes: object) -> CampaignFacts:
    base = CampaignFacts(
        src=src, platform=Platform.DIRECT if src.startswith("yd") else Platform.VK, name=f"Кампания {src}",
        state=CampaignState.ACTIVE, budget=Budget(3000_00, WEEK), limit=None, pays_per_conversion=False,
        previous_budget_kop=None, last_budget_change_day=None, spend_total_kop=0, spend_matured_kop=0,
        spend_yesterday_kop=0, spend_in_limit_kop=0, games3=0, games3_matured=0, paid_rub=0, paused_by=None,
        fresh=True,
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def situation(**changes: object) -> Situation:
    base = Situation(
        today=TODAY, season_start="11-24", season_end="12-12", cap_kop=15000_00, spent_total_kop=0,
        cutoff=date(2026, 11, 28),
        thresholds=Thresholds(pause_cpa_kop=250_00, scale_cpa_kop=150_00, min_spend_kop=500_00, max_raise_pct=30),
        min_budget_kop=minimum_gross_kop(22),
    )
    return replace(base, **changes)  # type: ignore[arg-type]


def actions(decisions: list[Decision]) -> list[tuple[str, ActionKind]]:
    return [(decision.src, decision.action) for decision in decisions]


def waiting(src: str = "vk5", **changes: object) -> CampaignFacts:
    """Paused by the autopilot before the season started."""
    return facts(src, **{"state": PAUSED, "paused_by": PausedBy.PRESEASON, "budget": Budget(500_00, DAY), **changes})


# --- one more day of spend ------------------------------------------------------------------------------------


@pytest.mark.parametrize(("campaign", "day_kop"), [
    (facts("vk1", budget=Budget(500_00, DAY)), 500_00),
    # Direct paying per click: 35% of the week plus the rest carried over from last week (≤ 30%)
    (facts("yd1", budget=Budget(2000_00, WEEK)), 910_00),
    (facts("yd1", budget=Budget(2000_01, WEEK)), 910_01),  # rounded up
    # paying per conversion: all the conversions may come on one day
    (facts("yd1", budget=Budget(2000_00, WEEK), pays_per_conversion=True), 2000_00),
    # the day of a change: the old budget still counts
    (facts("yd1", budget=Budget(3000_00, WEEK), previous_budget_kop=2000_00, last_budget_change_day=TODAY),
     2275_00),
    (facts("yd1", budget=Budget(3000_00, WEEK), previous_budget_kop=2000_00, last_budget_change_day=TODAY,
           pays_per_conversion=True), 5000_00),
    (facts("yd1", budget=Budget(3000_00, WEEK), previous_budget_kop=2000_00,
           last_budget_change_day=date(2026, 11, 29)), 1365_00),
    # VK's budget for the whole campaign: what is left of it, and never more than the daily budget
    (facts("vk1", budget=None, limit=SpendLimit(6000_00), spend_in_limit_kop=4000_00), 2000_00),
    (facts("vk1", budget=Budget(500_00, DAY), limit=SpendLimit(6000_00), spend_in_limit_kop=5800_00), 200_00),
    (facts("vk1", budget=None, limit=SpendLimit(6000_00), spend_in_limit_kop=7000_00), 0),
    # Direct's budget for a period: what is left in it; the whole of it for another period
    (facts("yd1", budget=None, limit=SpendLimit(5000_00, date(2026, 11, 24), date(2026, 12, 12)),
           spend_in_limit_kop=1200_00), 3800_00),
    (facts("yd1", budget=None, limit=SpendLimit(5000_00, date(2026, 11, 1), date(2026, 11, 20)),
           spend_in_limit_kop=1200_00), 5000_00),
    (facts("vk1", budget=None), None),
])
def test_one_more_day(campaign: CampaignFacts, day_kop: int | None) -> None:
    assert rules.day_exposure(campaign, TODAY) == day_kop
    assert rules.budget_visible(campaign, TODAY) == (day_kop is not None)


def test_a_change_today_keeps_the_replaced_budget_on_record() -> None:
    (changed,) = rules.with_budget([facts("yd1", budget=Budget(3000_00, WEEK))], "yd1", Budget(3500_00, WEEK), TODAY)
    assert (changed.budget, changed.previous_budget_kop, changed.last_budget_change_day) == (
        Budget(3500_00, WEEK), 3000_00, TODAY)
    (again,) = rules.with_budget([changed], "yd1", Budget(4000_00, WEEK), TODAY)
    assert again.previous_budget_kop == 6500_00, "both replaced budgets may still spend today"
    assert rules.day_exposure(again, TODAY) == (4000_00 + 6500_00) * 455 // 1000


# --- 1. season window -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("today", "reason", "paused_by"), [
    (date(2026, 11, 23), "сезон ещё не начался", PausedBy.PRESEASON),
    (date(2026, 12, 13), "сезон закончился", PausedBy.SEASON),
    (date(2027, 1, 5), "сезон ещё не начался", PausedBy.PRESEASON),
])
def test_outside_the_season_every_active_campaign_pauses_and_nothing_else_happens(
    today: date, reason: str, paused_by: PausedBy
) -> None:
    cheap = facts("yd1", games3_matured=5, spend_matured_kop=100_00)
    expensive = facts("yd2", spend_matured_kop=900_00)
    blind = facts("vk4", budget=None)
    decisions = rules.decide([cheap, expensive, facts("vk3", state=PAUSED), blind], situation(today=today))
    assert actions(decisions) == [("yd1", ActionKind.PAUSE), ("yd2", ActionKind.PAUSE), ("vk4", ActionKind.PAUSE)]
    assert {d.reason for d in decisions} == {reason}
    assert all(d.params == {"paused_by": paused_by} and not d.needs_approval for d in decisions)


@pytest.mark.parametrize("today", [date(2026, 11, 24), date(2026, 12, 12)])
def test_the_season_includes_its_first_and_last_day(today: date) -> None:
    assert rules.decide([facts()], situation(today=today)) == []


def test_a_season_across_the_new_year() -> None:
    wrapped = situation(season_start="12-20", season_end="01-10")
    assert rules.decide([facts()], replace(wrapped, today=date(2027, 1, 5))) == []
    (decision,) = rules.decide([facts()], replace(wrapped, today=date(2027, 1, 11)))
    assert decision.reason == "сезон закончился"


# --- 2. blind budgets ------------------------------------------------------------------------------------------


def test_a_campaign_without_a_visible_budget_is_paused_on_its_own() -> None:
    """Review finding 1: a blind campaign once hid behind the cap alone, or stopped all the others."""
    vk = facts("vk55", budget=None)  # e.g. budgets per ad group, or none at all
    for spent in (0, 10000_00, 14999_99):
        (decision,) = rules.protect([vk], situation(spent_total_kop=spent))
        assert (decision.src, decision.action, decision.reason, decision.params) == (
            "vk55", ActionKind.PAUSE, texts.PROMO_REASON_BLIND, {"paused_by": PausedBy.AUTOPILOT})

    yd = facts("yd701", budget=Budget(2440_00, WEEK))
    assert actions(rules.decide([vk, yd], situation())) == [("vk55", ActionKind.PAUSE)], "the others keep running"
    assert rules.cap_holds([vk, yd], situation()), "a blind campaign is not added to the others' projection"
    assert rules.projected_kop([vk, yd], situation()) == 2440_00 * 455 // 1000


def test_a_paused_blind_campaign_needs_nothing() -> None:
    assert rules.decide([facts("vk55", budget=None, state=PAUSED)], situation()) == []


# --- 3. the hard cap -------------------------------------------------------------------------------------------


def test_cap_pauses_all_active_campaigns_when_one_more_day_could_cross_it() -> None:
    weekly = facts("yd1", budget=Budget(7000_00, WEEK))  # one day: 3185 ₽
    daily = facts("vk2", budget=Budget(500_00, DAY))
    decisions = rules.decide([weekly, daily, facts("yd3", state=PAUSED)], situation(spent_total_kop=11315_01))
    assert actions(decisions) == [("yd1", ActionKind.PAUSE), ("vk2", ActionKind.PAUSE)]
    assert decisions[0].params == {"paused_by": PausedBy.CAP}
    assert decisions[0].reason == texts.promo_reason_cap(cap_kop=15000_00, spent_kop=11315_01,
                                                         projected_kop=15000_01)
    assert decisions[0].reason.startswith("достигнут лимит 15 000 ₽")


def test_cap_exactly_reached_is_still_allowed() -> None:
    weekly = facts("yd1", budget=Budget(2000_01, WEEK))  # one day: 910.01 ₽
    assert rules.decide([weekly], situation(spent_total_kop=15000_00 - 910_01)) == []
    assert actions(rules.decide([weekly], situation(spent_total_kop=15000_00 - 910_00))) == [("yd1", ActionKind.PAUSE)]


def test_the_kits_weekly_budget_under_a_small_cap() -> None:
    """Review finding 2: a day of «недельный 2 000 ₽» (2 440 ₽ gross) is not a seventh of the week."""
    kit = facts("yd701", budget=Budget(2440_00, WEEK))
    assert actions(rules.protect([kit], situation(spent_total_kop=5600_00, cap_kop=6000_00))) == [
        ("yd701", ActionKind.PAUSE)], "5 600 + 45.5% of 2 440 crosses 6 000"
    raised = rules.with_budget([kit], "yd701", Budget(3170_00, WEEK), TODAY)
    assert not rules.cap_holds(raised, situation(spent_total_kop=5000_00, cap_kop=6000_00)), (
        "the week restarts after a change and the old budget still counts that day")


def test_cap_overrides_rules_five_and_six() -> None:
    cheap = facts("yd1", games3_matured=5, spend_matured_kop=100_00, budget=Budget(700_00, DAY))
    decisions = rules.decide([cheap], situation(spent_total_kop=14500_00))
    assert actions(decisions) == [("yd1", ActionKind.PAUSE)] and decisions[0].params["paused_by"] == PausedBy.CAP


def test_campaigns_waiting_for_the_season_stay_paused_for_the_cap() -> None:
    running = facts("vk1", budget=Budget(1000_00, DAY))
    decisions = rules.decide([running, waiting("vk5")], situation(spent_total_kop=14500_00))
    assert actions(decisions) == [("vk1", ActionKind.PAUSE), ("vk5", ActionKind.PAUSE)]
    assert all(decision.params == {"paused_by": PausedBy.CAP} for decision in decisions)


# --- 4. the season starts ---------------------------------------------------------------------------------------


def test_campaigns_paused_before_the_season_start_when_it_begins() -> None:
    """Review finding 5: they used to stay paused for good."""
    decisions = rules.decide([waiting("vk5"), facts("vk6", state=PAUSED, paused_by=PausedBy.CAP)],
                             situation(today=date(2026, 11, 24)))
    assert [(d.src, d.action, d.reason, d.needs_approval) for d in decisions] == [
        ("vk5", ActionKind.RESUME, texts.PROMO_REASON_SEASON_STARTED, False)], "only the season's own pauses"


def test_the_season_start_keeps_the_cap_and_needs_fresh_numbers_and_a_budget() -> None:
    first, second = waiting("vk5", budget=Budget(1000_00, DAY)), waiting("vk6", budget=Budget(1000_00, DAY))
    decisions = rules.decide([first, second], situation(spent_total_kop=13500_00))  # room for one of them
    assert actions(decisions) == [("vk5", ActionKind.RESUME), ("vk6", ActionKind.PAUSE)]
    assert decisions[1].params == {"paused_by": PausedBy.CAP}
    assert rules.decide([waiting(fresh=False)], situation()) == [], "tried again with fresh numbers"
    (blind,) = rules.decide([waiting(budget=None)], situation())
    assert (blind.action, blind.reason, blind.params) == (
        ActionKind.PAUSE, texts.PROMO_REASON_BLIND, {"paused_by": PausedBy.AUTOPILOT})


# --- 5. expensive -------------------------------------------------------------------------------------------


def test_no_games_after_the_minimum_spend_pauses() -> None:
    (decision,) = rules.decide([facts(spend_matured_kop=500_00, games3=7)], situation())
    assert (decision.action, decision.params, decision.needs_approval) == (
        ActionKind.PAUSE, {"paused_by": PausedBy.AUTOPILOT}, False)
    assert decision.reason == "по 27 ноября потрачено 500 ₽, а игр на 3+ участника нет"


def test_below_the_minimum_spend_nothing_happens_even_without_games() -> None:
    assert rules.decide([facts(spend_matured_kop=499_99, spend_total_kop=2000_00)], situation()) == []


def test_dear_games_pause_and_the_threshold_itself_does_not() -> None:
    at_threshold = facts("yd1", spend_matured_kop=750_00, games3_matured=3)  # exactly 250 ₽
    dearer = facts("yd2", spend_matured_kop=750_03, games3_matured=3)
    decisions = rules.decide([at_threshold, dearer], situation())
    assert actions(decisions) == [("yd2", ActionKind.PAUSE)]
    assert decisions[0].reason == ("по 27 ноября потрачено 750 ₽, игр на 3+ участника: 3 — 250 ₽ за игру, "
                                   "дороже порога 250 ₽")


def test_matured_numbers_decide_not_the_raw_ones() -> None:
    fresh_games = facts(spend_matured_kop=600_00, games3=10, games3_matured=0)
    assert actions(rules.decide([fresh_games], situation())) == [("yd701", ActionKind.PAUSE)]
    fresh_spend = facts(spend_total_kop=5000_00, spend_matured_kop=100_00, games3=1)
    assert rules.decide([fresh_spend], situation()) == []


def test_admin_paused_campaigns_are_left_to_the_admin() -> None:
    resumed_elsewhere = facts(paused_by=PausedBy.ADMIN, spend_matured_kop=900_00)
    cheap = facts("yd2", paused_by=PausedBy.ADMIN, games3_matured=5, spend_matured_kop=100_00)
    assert rules.decide([resumed_elsewhere, cheap], situation()) == []
    assert actions(rules.decide([resumed_elsewhere], situation(today=date(2026, 12, 20)))) == [
        ("yd701", ActionKind.PAUSE)], "the season still applies"
    assert actions(rules.decide([replace(resumed_elsewhere, budget=None)], situation())) == [
        ("yd701", ActionKind.PAUSE)], "and so does a blind budget"


# --- 6. cheap ------------------------------------------------------------------------------------------------


def test_cheap_campaign_gets_a_raise_that_needs_approval() -> None:
    cheap = facts(games3_matured=4, spend_matured_kop=400_00, budget=Budget(3000_00, WEEK))
    (decision,) = rules.decide([cheap], situation())
    assert decision.action == ActionKind.SET_BUDGET and decision.needs_approval
    assert decision.params == {"from_kop": 3000_00, "to_kop": 3900_00, "kind": WEEK}
    assert decision.reason == "по 27 ноября игр на 3+ участника: 4, по 100 ₽ за игру — дешевле порога 150 ₽"


def test_raise_is_rounded_down_to_10_rubles_and_skipped_below_50() -> None:
    odd = facts(games3_matured=2, spend_matured_kop=100_00, budget=Budget(1234_56, WEEK))
    (decision,) = rules.decide([odd], situation())
    assert decision.params["to_kop"] == 1600_00  # 1234.56 × 1.3 = 1604.93 → 1600
    small = facts(games3_matured=2, spend_matured_kop=100_00, budget=Budget(400_00, DAY))
    assert rules.decide([small], situation(thresholds=replace(situation().thresholds, max_raise_pct=12))) == [], (
        "400 → 448 → 440 ₽ is a raise of 40 ₽")


def test_raise_keeps_the_cap() -> None:
    daily = facts("vk1", games3_matured=3, spend_matured_kop=150_00, budget=Budget(1000_00, DAY))
    (decision,) = rules.decide([daily], situation(spent_total_kop=13880_00))  # room for 1120 ₽ tomorrow
    assert decision.params["to_kop"] == 1120_00
    weekly = facts("yd1", games3_matured=3, spend_matured_kop=150_00, budget=Budget(2000_00, WEEK))
    (decision,) = rules.decide([weekly], situation(spent_total_kop=13000_00))  # 2000 ₽ of room for its day
    assert decision.params["to_kop"] == 2390_00, "45.5% of the old and the new week fit into 2000 ₽"
    (raised,) = rules.with_budget([weekly], "yd1", Budget(2390_00, WEEK), TODAY)
    assert rules.day_exposure(raised, TODAY) == 1997_45


def test_a_pay_per_conversion_raise_counts_both_weeks_whole() -> None:
    per_conversion = facts("yd1", games3_matured=3, spend_matured_kop=150_00, budget=Budget(1000_00, WEEK),
                           pays_per_conversion=True)
    (decision,) = rules.decide([per_conversion], situation(spent_total_kop=12700_00))  # room 2300 ₽ for its day
    assert decision.params["to_kop"] == 1300_00
    assert rules.decide([per_conversion], situation(spent_total_kop=13000_00)) == [], (
        "the old 1000 ₽ and a new 1300 ₽ week do not fit into 2000 ₽ of room")


def test_raises_share_the_room_under_the_cap() -> None:
    first = facts("vk1", games3_matured=3, spend_matured_kop=150_00, budget=Budget(1000_00, DAY))
    second = facts("vk2", games3_matured=3, spend_matured_kop=150_00, budget=Budget(1000_00, DAY))
    decisions = rules.decide([first, second], situation(spent_total_kop=12800_00))  # room: 200 ₽
    assert [d.params["to_kop"] for d in decisions] == [1200_00]


@pytest.mark.parametrize("campaign", [
    facts(games3_matured=1, spend_matured_kop=50_00),  # one game is not a pattern
    facts(games3_matured=3, spend_matured_kop=450_00),  # exactly 150 ₽: not cheaper
    facts(games3_matured=3, spend_matured_kop=100_00, last_budget_change_day=TODAY),
    facts(games3_matured=3, spend_matured_kop=100_00, budget=None, limit=SpendLimit(6000_00)),  # nothing to raise
    facts(games3_matured=3, spend_matured_kop=100_00, state=CampaignState.PAUSED),
    facts(games3_matured=3, spend_matured_kop=100_00, fresh=False),  # old numbers
])
def test_no_raise(campaign: CampaignFacts) -> None:
    assert rules.decide([campaign], situation()) == []


def test_raise_is_never_below_the_platform_minimum() -> None:
    assert minimum_gross_kop(22) == 366_00
    tiny = facts("vk1", games3_matured=2, spend_matured_kop=100_00, budget=Budget(300_00, DAY))
    (decision,) = rules.decide([tiny], situation())
    assert decision.params["to_kop"] == 390_00
    below = facts("vk1", games3_matured=2, spend_matured_kop=100_00, budget=Budget(200_00, DAY))
    (decision,) = rules.decide([below], situation(thresholds=replace(situation().thresholds, max_raise_pct=10)))
    assert decision.params["to_kop"] == 366_00


def test_order_pauses_first_and_a_paused_campaign_is_not_raised() -> None:
    cheap = facts("yd1", games3_matured=4, spend_matured_kop=200_00)
    expensive = facts("yd2", spend_matured_kop=800_00, games3_matured=1)
    decisions = rules.decide([cheap, expensive], situation())
    assert actions(decisions) == [("yd2", ActionKind.PAUSE), ("yd1", ActionKind.SET_BUDGET)]


def test_protect_applies_only_season_blind_budgets_and_cap() -> None:
    expensive = facts(spend_matured_kop=900_00)
    assert rules.protect([expensive, waiting()], situation()) == [], "no pauses for money, no season start"
    assert actions(rules.protect([expensive], situation(spent_total_kop=15000_00))) == [("yd701", ActionKind.PAUSE)]


def test_cap_check_for_admin_changes() -> None:
    paused = facts("vk1", state=PAUSED, budget=Budget(700_00, DAY))
    tight = situation(spent_total_kop=14400_00)
    assert rules.cap_holds([paused], tight)
    resumed = rules.with_state([paused], "vk1", CampaignState.ACTIVE)
    assert not rules.cap_holds(resumed, tight) and rules.projected_kop(resumed, tight) == 15100_00
    running = facts("vk1", budget=Budget(500_00, DAY))
    assert not rules.cap_holds(rules.with_budget([running], "vk1", Budget(700_00, DAY), TODAY), tight)
