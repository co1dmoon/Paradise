"""Paid upgrades (§4, §5.6): payment rows, confirmation and applying a tier.

Provider-agnostic: the Robokassa module verifies signatures and amounts, then
calls ``confirm_payment``. Admin grants (/grant) go through ``grant_tier``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app import repo
from app.core.analytics import Event, record
from app.core.games import (
    ACTIVE,
    WAITING,
    GameError,
    activate_waiting,
    load_game,
    require_participant,
    require_status,
)
from app.core.models import Game, GameStatus, Participant, Payment, PaymentProvider, PaymentStatus, Tier
from app.core.pricing import PriceList, upgrade_price
from app.db import Db

PAYMENT_REUSE_WINDOW = timedelta(hours=24)
TIER_ORDER: tuple[Tier, ...] = (Tier.FREE, Tier.S, Tier.M, Tier.L)


class NoUpgradeAvailable(GameError):
    """The requested tier does not raise the game's current limit."""


@dataclass(frozen=True, slots=True)
class PaymentOutcome:
    """What applying a payment did.

    ``excess_rub`` is the part of a Robokassa payment that bought nothing and has to be
    refunded: all of it when the game was no longer collecting or already had this limit
    (an old link paid after another upgrade), part of it when the link was priced before
    another payment for the same game arrived. ``duplicates`` lists the paid InvIds when
    the same tier was paid more than once (§5.6).
    """

    payment: Payment
    game: Game | None
    activated: list[Participant]
    already_processed: bool = False
    duplicates: tuple[int, ...] = ()
    game_not_collecting: bool = False
    excess_rub: int = 0

    @property
    def fully_excess(self) -> bool:
        """The payment changed nothing: the payer is owed all of it back."""
        return self.excess_rub > 0 and self.excess_rub >= self.payment.amount_rub


def higher_tier(a: Tier, b: Tier) -> Tier:
    return a if TIER_ORDER.index(a) >= TIER_ORDER.index(b) else b


async def request_upgrade(
    db: Db, game_id: int, payer_id: int, tier: Tier, prices: PriceList, now: datetime
) -> Payment | PaymentOutcome:
    """Create (or reuse, §5.6) a payment row for an upgrade.

    Anyone in the game or its organizer may pay. When nothing is left to pay
    (price difference <= 0) the tier is applied at once and the outcome returned.
    """
    async with db.transaction() as tx:
        game = await load_game(tx, game_id)
        require_status(game, GameStatus.COLLECTING)
        if game.organizer_id != payer_id:
            await require_participant(tx, game_id, payer_id, ACTIVE, WAITING)
        if prices.limit_for(tier) <= game.participant_limit:
            raise NoUpgradeAvailable(tier)
        amount = upgrade_price(prices, tier, await repo.paid_sum(tx, game_id))
        await record(tx, Event.PAY_CLICK, now, user_id=payer_id, game_id=game_id, tier=tier, amount=amount)
        if amount <= 0:
            payment = await repo.insert_payment(
                tx, game_id, payer_id, tier, 0, PaymentStatus.GRANTED, PaymentProvider.MANUAL, now
            )
            return await _apply(tx, payment, prices)
        reusable = await repo.reusable_payment(tx, game_id, payer_id, tier, amount, now - PAYMENT_REUSE_WINDOW)
        if reusable is not None:
            return reusable
        return await repo.insert_payment(
            tx, game_id, payer_id, tier, amount, PaymentStatus.CREATED, PaymentProvider.ROBOKASSA, now
        )


async def confirm_payment(
    db: Db, inv_id: int, prices: PriceList, *, raw: str, now: datetime
) -> PaymentOutcome | None:
    """Mark a payment paid and apply its tier, idempotently. None if the InvId is unknown.

    A payment already paid, granted or refunded is reported with ``already_processed``.
    """
    async with db.transaction() as tx:
        payment = await repo.get_payment(tx, inv_id)
        if payment is None:
            return None
        if payment.status != PaymentStatus.CREATED:
            game = None if payment.game_id is None else await repo.get_game(tx, payment.game_id)
            return PaymentOutcome(payment, game, [], already_processed=True)
        await repo.mark_payment(tx, inv_id, PaymentStatus.PAID, now, raw)
        paid = await repo.get_payment(tx, inv_id)
        assert paid is not None
        await record(
            tx, Event.PAY_SUCCESS, now, user_id=paid.payer_id, game_id=paid.game_id, tier=paid.tier,
            amount=paid.amount_rub,
        )
        return await _apply(tx, paid, prices)


async def grant_tier(
    db: Db, game_id: int, tier: Tier, amount_rub: int, prices: PriceList, now: datetime
) -> PaymentOutcome:
    """/grant: record a manual payment with status 'granted' and apply the tier."""
    async with db.transaction() as tx:
        await load_game(tx, game_id)
        payment = await repo.insert_payment(
            tx, game_id, None, tier, amount_rub, PaymentStatus.GRANTED, PaymentProvider.MANUAL, now
        )
        return await _apply(tx, payment, prices)


async def refund_payment(db: Db, inv_id: int) -> Payment | None:
    """/refund: mark as refunded (the money goes back through the Robokassa cabinet)."""
    async with db.transaction() as tx:
        if await repo.get_payment(tx, inv_id) is None:
            return None
        await repo.mark_payment(tx, inv_id, PaymentStatus.REFUNDED)
        return await repo.get_payment(tx, inv_id)


async def _apply(db: Db, payment: Payment, prices: PriceList) -> PaymentOutcome:
    """Raise tier and limit to the max of current and paid, then activate the queue in join order.

    A game that stopped collecting (drawn or cancelled while the payer was on the payment
    page) is left as it is: the payment is reported as excess, to be refunded.
    """
    if payment.game_id is None:
        return PaymentOutcome(payment, None, [])
    game = await load_game(db, payment.game_id)
    if game.status != GameStatus.COLLECTING:
        return PaymentOutcome(payment, game, [], game_not_collecting=True, excess_rub=payment.amount_rub)
    excess = await _excess(db, game, payment, prices)
    await repo.update_game(
        db,
        game.id,
        tier=higher_tier(game.tier, payment.tier),
        participant_limit=max(game.participant_limit, prices.limit_for(payment.tier)),
    )
    activated = await activate_waiting(db, game.id)
    duplicates = await db.fetchall(
        "SELECT inv_id FROM payments WHERE game_id = ? AND tier = ? AND status = 'paid'"
        " AND provider = 'robokassa' ORDER BY inv_id",
        (game.id, payment.tier),
    )
    duplicate_ids = tuple(row[0] for row in duplicates) if len(duplicates) > 1 else ()
    return PaymentOutcome(
        payment=payment,
        game=await load_game(db, game.id),
        activated=activated,
        duplicates=duplicate_ids,
        excess_rub=excess,
    )


async def _excess(db: Db, game: Game, payment: Payment, prices: PriceList) -> int:
    """Rubles of a Robokassa payment beyond what its tier costs (§4: price minus the sum already paid).

    The amount of a payment row is fixed when the link is made, so a link opened before
    another upgrade of the same game was paid charges too much, or (when the game already
    has this limit) buys nothing at all. ``payment`` is already counted in the paid sum.
    """
    if payment.provider != PaymentProvider.ROBOKASSA:
        return 0
    if prices.limit_for(payment.tier) <= game.participant_limit:
        return payment.amount_rub
    overpaid = await repo.paid_sum(db, game.id) - prices.offer(payment.tier).price
    return min(payment.amount_rub, max(0, overpaid))
