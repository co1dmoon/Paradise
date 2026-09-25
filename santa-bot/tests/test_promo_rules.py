"""PROMO_SPEC §5: the rules engine is pure — every rule, their order and the money edge cases."""

from __future__ import annotations

from dataclasses import replace
from datetime import date

import pytest

from app.core import texts
from app.promo import rules
from app.promo.platforms import Budget, BudgetKind, CampaignState, Platform, minimum_gross_kop
from app.promo.rules import ActionKind, CampaignFacts, Decision, PausedBy, Situation, Thresholds

TODAY = date(2026, 11, 30)
WEEK = BudgetKind.WEEK
DAY = BudgetKind.DAY


def facts(src: str = "yd701", **changes: object) -> CampaignFacts:
    base = CampaignFacts(
        src=src, platform=Platform.DIRECT if src.startswith("yd") else Platform.VK, name=f"Кампания {src}",
        state=CampaignState.ACTIVE, budget=Budget(3000_00, WEEK), plan_budget=Budget(3000_00, WEEK),
        spend_total_kop=0, spend_matured_kop=0, spend_yesterday_kop=0, games3=0, games3_matured=0, paid_rub=0,
        last_budget_change_day=None, paused_by=None,
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


# --- 1. season window -------------------------------------------------------------------------------------


@pytest.mark.parametrize(("today", "reason"), [
    (date(2026, 11, 23), "сезон ещё не начался"),
    (date(2026, 12, 13), "сезон закончился"),
    (date(2027, 1, 5), "сезон ещё не начался"),
])
def test_outside_the_season_every_active_campaign_pauses_and_nothing_else_happens(today: date, reason: str) -> None:
    cheap = facts("yd1", games3_matured=5, spend_matured_kop=100_00)
    expensive = facts("yd2", spend_matured_kop=900_00)
    paused = facts("vk3", state=CampaignState.PAUSED)
    decisions = rules.decide([cheap, expensive, paused], situation(today=today))
    assert actions(decisions) == [("yd1", ActionKind.PAUSE), ("yd2", ActionKind.PAUSE)]
    assert {d.reason for d in decisions} == {reason}
    assert all(d.params == {"paused_by": PausedBy.SEASON} and not d.needs_approval for d in decisions)


@pytest.mark.parametrize("today", [date(2026, 11, 24), date(2026, 12, 12)])
def test_the_season_includes_its_first_and_last_day(today: date) -> None:
    assert rules.decide([facts()], situation(today=today)) == []


def test_a_season_across_the_new_year() -> None:
    wrapped = situation(season_start="12-20", season_end="01-10")
    assert rules.decide([facts()], replace(wrapped, today=date(2027, 1, 5))) == []
    (decision,) = rules.decide([facts()], replace(wrapped, today=date(2027, 1, 11)))
    assert decision.reason == "сезон закончился"


# --- 2. the hard cap ---------------------------------------------------------------------------------------


def test_cap_pauses_all_active_campaigns_when_one_more_day_could_cross_it() -> None:
    weekly = facts("yd1", budget=Budget(7000_00, WEEK))  # one day = 1000 ₽
    daily = facts("vk2", budget=Budget(500_00, DAY))
    decisions = rules.decide([weekly, daily, facts("yd3", state=CampaignState.PAUSED)],
                             situation(spent_total_kop=13500_01))
    assert actions(decisions) == [("yd1", ActionKind.PAUSE), ("vk2", ActionKind.PAUSE)]
    assert decisions[0].params == {"paused_by": PausedBy.CAP}
    assert decisions[0].reason == texts.promo_reason_cap(cap_kop=15000_00, spent_kop=13500_01,
                                                         projected_kop=15000_01)
    assert decisions[0].reason.startswith("достигнут лимит 15 000 ₽")


def test_cap_exactly_reached_is_still_allowed_and_week_days_round_up() -> None:
    weekly = facts("yd1", budget=Budget(7000_01, WEEK))  # ceil(700001 / 7) = 100001
    assert rules.decide([weekly], situation(spent_total_kop=15000_00 - 1000_01)) == []
    assert actions(rules.decide([weekly], situation(spent_total_kop=15000_00 - 1000_00))) == [("yd1", ActionKind.PAUSE)]


@pytest.mark.parametrize(("campaign", "day_kop"), [
    (facts("yd1", budget=None, plan_budget=Budget(3000_00, WEEK)), 1050_00),  # Direct: 35% of the weekly plan
    (facts("vk1", budget=None, plan_budget=Budget(400_00, DAY)), 400_00),  # VK: the daily plan
])
def test_unknown_budgets_use_the_registered_plan(campaign: CampaignFacts, day_kop: int) -> None:
    assert rules.day_spend_kop(campaign, remaining_kop=10**9) == day_kop
    assert rules.decide([campaign], situation(spent_total_kop=15000_00 - day_kop)) == []
    assert rules.decide([campaign], situation(spent_total_kop=15000_00 - day_kop + 1)) != []


def test_nothing_known_assumes_everything_left_under_the_cap() -> None:
    blind = facts("yd1", budget=None, plan_budget=None)
    assert rules.day_spend_kop(blind, remaining_kop=4000_00) == 4000_00
    assert rules.decide([blind], situation(spent_total_kop=11000_00)) == [], "alone it can only reach the cap"
    with_other = rules.decide([blind, facts("vk2", budget=Budget(300_00, DAY))], situation(spent_total_kop=11000_00))
    assert actions(with_other) == [("yd1", ActionKind.PAUSE), ("vk2", ActionKind.PAUSE)]
    assert actions(rules.decide([blind], situation(spent_total_kop=15000_00))) == [("yd1", ActionKind.PAUSE)]


def test_cap_overrides_rules_three_and_four() -> None:
    cheap = facts("yd1", games3_matured=5, spend_matured_kop=100_00, budget=Budget(700_00, DAY))
    decisions = rules.decide([cheap], situation(spent_total_kop=14500_00))
    assert actions(decisions) == [("yd1", ActionKind.PAUSE)] and decisions[0].params["paused_by"] == PausedBy.CAP


# --- 3. expensive -------------------------------------------------------------------------------------------


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


# --- 4. cheap ------------------------------------------------------------------------------------------------


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
    weekly = facts("yd1", games3_matured=3, spend_matured_kop=150_00, budget=Budget(5000_00, WEEK))
    (decision,) = rules.decide([weekly], situation(spent_total_kop=14200_00))  # one day: 715 ₽ → room 800 ₽
    assert decision.params["to_kop"] == 5600_00, "a weekly budget of 5600 ₽ spends 800 ₽ a day"


def test_raises_share_the_room_under_the_cap() -> None:
    first = facts("vk1", games3_matured=3, spend_matured_kop=150_00, budget=Budget(1000_00, DAY))
    second = facts("vk2", games3_matured=3, spend_matured_kop=150_00, budget=Budget(1000_00, DAY))
    decisions = rules.decide([first, second], situation(spent_total_kop=12800_00))  # room: 200 ₽
    assert [d.params["to_kop"] for d in decisions] == [1200_00]


@pytest.mark.parametrize("campaign", [
    facts(games3_matured=1, spend_matured_kop=50_00),  # one game is not a pattern
    facts(games3_matured=3, spend_matured_kop=450_00),  # exactly 150 ₽: not cheaper
    facts(games3_matured=3, spend_matured_kop=100_00, last_budget_change_day=TODAY),
    facts(games3_matured=3, spend_matured_kop=100_00, budget=None),
    facts(games3_matured=3, spend_matured_kop=100_00, state=CampaignState.PAUSED),
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


def test_protect_applies_only_season_and_cap() -> None:
    expensive = facts(spend_matured_kop=900_00)
    assert rules.protect([expensive], situation()) == []
    assert actions(rules.protect([expensive], situation(spent_total_kop=15000_00))) == [("yd701", ActionKind.PAUSE)]


def test_cap_check_for_admin_changes() -> None:
    paused = facts("yd1", state=CampaignState.PAUSED, budget=Budget(700_00, DAY))
    tight = situation(spent_total_kop=14400_00)
    assert rules.cap_holds([paused], tight)
    assert not rules.cap_holds(rules.with_change([paused], "yd1", state=CampaignState.ACTIVE), tight)
    assert rules.projected_kop(rules.with_change([paused], "yd1", state=CampaignState.ACTIVE), tight) == 15100_00
    running = facts("yd1", budget=Budget(500_00, DAY))
    assert not rules.cap_holds(rules.with_change([running], "yd1", budget=Budget(700_00, DAY)), tight)
