from __future__ import annotations

from dataclasses import replace

from app.core.models import Settings, Tier
from app.core.pricing import PriceList, Upgrade, next_upgrade, upgrade_options, upgrade_price, validate_price_list

SETTINGS = Settings(free_limit=10, price_S=490, price_M=990, price_L=2490, limit_S=30, limit_M=100, limit_L=300,
                    maintenance=False)
PRICES = PriceList.from_settings(SETTINGS)


def test_limits_and_tiers() -> None:
    assert PRICES.limit_for(Tier.FREE) == 10
    assert PRICES.limit_for(Tier.M) == 100
    assert PRICES.max_limit == 300
    assert PRICES.tier_for_size(10) == Tier.FREE
    assert PRICES.tier_for_size(11) == Tier.S
    assert PRICES.tier_for_size(100) == Tier.M
    assert PRICES.tier_for_size(300) == Tier.L
    assert PRICES.tier_for_size(301) is None


def test_upgrade_price_subtracts_what_was_paid() -> None:
    assert upgrade_price(PRICES, Tier.S, 0) == 490
    assert upgrade_price(PRICES, Tier.M, 490) == 500
    assert upgrade_price(PRICES, Tier.L, 990) == 1500
    assert upgrade_price(PRICES, Tier.S, 990) == 0


def test_upgrade_options_from_current_limit() -> None:
    assert upgrade_options(PRICES, 10, 0) == [
        Upgrade(Tier.S, 30, 490), Upgrade(Tier.M, 100, 990), Upgrade(Tier.L, 300, 2490)]
    assert upgrade_options(PRICES, 30, 490) == [Upgrade(Tier.M, 100, 500), Upgrade(Tier.L, 300, 2000)]
    assert upgrade_options(PRICES, 300, 2490) == []
    assert next_upgrade(PRICES, 30, 490) == Upgrade(Tier.M, 100, 500)
    assert next_upgrade(PRICES, 300, 0) is None


def test_zero_difference_needs_no_payment() -> None:
    cheaper = PriceList.from_settings(replace(SETTINGS, price_M=400))
    upgrade = next_upgrade(cheaper, 30, 490)
    assert upgrade is not None and upgrade.amount == 0 and not upgrade.needs_payment


def test_tiers_follow_runtime_settings() -> None:
    changed = PriceList.from_settings(replace(SETTINGS, price_S=590, limit_S=40, free_limit=12))
    assert changed.limit_for(Tier.FREE) == 12
    assert next_upgrade(changed, 12, 0) == Upgrade(Tier.S, 40, 590)


def test_validate_price_list() -> None:
    assert validate_price_list(PRICES) == []
    broken = PriceList.from_settings(replace(SETTINGS, limit_M=20, price_L=500))
    problems = validate_price_list(broken)
    assert any("M" in problem for problem in problems)
    assert any("L" in problem for problem in problems)
