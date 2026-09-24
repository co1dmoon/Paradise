"""Tiers, limits and upgrade prices (§4). Values come from the ``settings`` table."""

from __future__ import annotations

from dataclasses import dataclass

from app.core.models import Settings, Tier

PAID_TIERS: tuple[Tier, ...] = (Tier.S, Tier.M, Tier.L)


@dataclass(frozen=True, slots=True)
class TierOffer:
    tier: Tier
    limit: int
    price: int


@dataclass(frozen=True, slots=True)
class Upgrade:
    """What a game can be upgraded to and what is left to pay (never negative)."""

    tier: Tier
    limit: int
    amount: int

    @property
    def needs_payment(self) -> bool:
        return self.amount > 0


@dataclass(frozen=True, slots=True)
class PriceList:
    free_limit: int
    offers: tuple[TierOffer, ...]

    @classmethod
    def from_settings(cls, settings: Settings) -> PriceList:
        return cls(
            free_limit=settings.free_limit,
            offers=(
                TierOffer(Tier.S, settings.limit_S, settings.price_S),
                TierOffer(Tier.M, settings.limit_M, settings.price_M),
                TierOffer(Tier.L, settings.limit_L, settings.price_L),
            ),
        )

    def offer(self, tier: Tier) -> TierOffer:
        for offer in self.offers:
            if offer.tier == tier:
                return offer
        raise ValueError(f"no paid tier {tier!r}")

    def limit_for(self, tier: Tier) -> int:
        return self.free_limit if tier == Tier.FREE else self.offer(tier).limit

    @property
    def max_limit(self) -> int:
        return max(offer.limit for offer in self.offers)

    def tier_for_size(self, participants: int) -> Tier | None:
        """Smallest tier that fits ``participants``; None above the largest tier ('напишите нам')."""
        if participants <= self.free_limit:
            return Tier.FREE
        for offer in sorted(self.offers, key=lambda o: o.limit):
            if participants <= offer.limit:
                return offer.tier
        return None


def upgrade_price(prices: PriceList, target: Tier, already_paid: int) -> int:
    """Target tier price minus what was already paid or granted for this game, floored at 0."""
    return max(0, prices.offer(target).price - already_paid)


def upgrade_options(prices: PriceList, current_limit: int, already_paid: int) -> list[Upgrade]:
    """Every paid tier that raises the game's current limit, cheapest first."""
    return [
        Upgrade(offer.tier, offer.limit, upgrade_price(prices, offer.tier, already_paid))
        for offer in sorted(prices.offers, key=lambda o: o.limit)
        if offer.limit > current_limit
    ]


def next_upgrade(prices: PriceList, current_limit: int, already_paid: int) -> Upgrade | None:
    """The smallest upgrade above the current limit, or None when the game is at the top tier."""
    options = upgrade_options(prices, current_limit, already_paid)
    return options[0] if options else None


def validate_price_list(prices: PriceList) -> list[str]:
    """Problems with a price list in Russian (empty when consistent). Used by /price."""
    problems = []
    if prices.free_limit < 3:
        problems.append("Бесплатный лимит должен быть не меньше 3.")
    previous_limit, previous_price = prices.free_limit, 0
    for offer in prices.offers:
        if offer.limit <= previous_limit:
            problems.append(f"Лимит тарифа {offer.tier} должен быть больше {previous_limit}.")
        if offer.price <= previous_price:
            problems.append(f"Цена тарифа {offer.tier} должна быть больше {previous_price} ₽.")
        previous_limit, previous_price = offer.limit, offer.price
    return problems
