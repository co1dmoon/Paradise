"""Admin commands and buttons for ADMIN_USER_IDS (§9). /whoami (in ``private``) works for anyone.

Admin-only commands are invisible to everyone else (they get the usual 'I don't
understand' reply) and work before consent. Replies never show wishes or
anonymous messages. Grants and cancellations take the game lock, like payments.
"""

from __future__ import annotations

import dataclasses

from app import repo
from app.core import analytics, billing, games, kb, texts
from app.core.models import Game, GameStatus, PaymentStatus, Settings, Tier
from app.core.payloads import normalize_code
from app.core.pricing import PAID_TIERS, PriceList, validate_price_list
from app.handlers import notices, views
from app.handlers.callbacks import Args, on
from app.handlers.private import command
from app.handlers.session import Outdated, Refusal, Session
from app.handlers.views import Action
from app.max_api import OutMessage

_PRICE_KEYS = {
    "s": "price_S",
    "m": "price_M",
    "l": "price_L",
    "free": "free_limit",
    "limit_s": "limit_S",
    "limit_m": "limit_M",
    "limit_l": "limit_L",
}
_SWITCH = {"on": True, "вкл": True, "off": False, "выкл": False}


@command("/admin", admin_only=True)
async def _admin_help(s: Session, rest: str) -> None:
    await s.say(texts.ADMIN_HELP)


@command("/stats", admin_only=True)
async def _stats(s: Session, rest: str) -> None:
    report = await analytics.build_stats_report(s.db, s.now(), s.ctx.config.tz)
    await s.say(texts.stats(report))


# --- games and payments ---------------------------------------------------------------------------------


@command("/game", admin_only=True)
async def _game(s: Session, rest: str) -> None:
    game = await _game_by_code(s, rest, texts.GAME_USAGE)
    await s.say(await _summary(s, game))


@command("/grant", admin_only=True)
async def _grant_command(s: Session, rest: str) -> None:
    parts = rest.split()
    if len(parts) not in (2, 3) or (len(parts) == 3 and not parts[2].isdigit()):
        raise Refusal(texts.GRANT_USAGE)
    tier = _tier(parts[1])
    if tier is None:
        raise Refusal(texts.GRANT_USAGE)
    game = await _game_by_code(s, parts[0], texts.GRANT_USAGE)
    await _grant(s, game.id, tier, int(parts[2]) if len(parts) == 3 else 0)


@on(Action.ADMIN_GRANT, admin_only=True)
async def _grant_button(s: Session, args: Args) -> None:
    tier = _tier(args.text(1))
    if tier is None:
        raise Outdated(f"no tier {args.text(1)!r}")
    await _grant(s, args.number(0), tier, 0)


async def _grant(s: Session, game_id: int, tier: Tier, amount: int) -> None:
    """Record a manual 'granted' payment and apply the tier (§9), like a confirmed payment."""
    prices = await s.ctx.prices()
    async with s.ctx.locks.game(game_id), s.db.transaction() as tx:
        game = await _admin_game(s, game_id)
        if game.status != GameStatus.COLLECTING:
            raise Refusal(texts.UPGRADE_ONLY_BEFORE_DRAW)
        outcome = await billing.grant_tier(tx, game_id, tier, amount, prices, s.now())
        upgraded = outcome.game
        assert upgraded is not None
        await notices.payment_applied(s.ctx, outcome)
        await notices.game_upgraded(s.ctx, upgraded, db=tx)
    await s.say(texts.granted(code=upgraded.code, tier=upgraded.tier, limit=upgraded.participant_limit))


@on(Action.ADMIN_CANCEL, admin_only=True)
async def _cancel(s: Session, args: Args) -> None:
    game = await _admin_game(s, args.number(0))
    _require_open(game)
    await s.say(views.confirm_admin_cancel(game))


@on(Action.ADMIN_CANCEL_CONFIRM, admin_only=True)
async def _cancel_confirm(s: Session, args: Args) -> None:
    game_id = args.number(0)
    async with s.ctx.locks.game(game_id):
        _require_open(await _admin_game(s, game_id))
        people = await games.cancel_game_as_admin(s.db, game_id, s.now())
    game = await games.load_game(s.db, game_id)
    await notices.game_cancelled_by_service(s.ctx, game, people)
    await s.say(texts.admin_game_cancelled(game.code))


@command("/refund", admin_only=True)
async def _refund(s: Session, rest: str) -> None:
    if not rest.isdigit():
        raise Refusal(texts.REFUND_USAGE)
    payment = await repo.get_payment(s.db, int(rest))
    if payment is None:
        raise Refusal(texts.PAYMENT_NOT_FOUND)
    if payment.status not in (PaymentStatus.PAID, PaymentStatus.GRANTED):
        raise Refusal(texts.payment_not_refundable(inv_id=payment.inv_id, status=payment.status))
    await billing.refund_payment(s.db, payment.inv_id)
    await s.say(texts.refunded(payment.inv_id))


async def _game_by_code(s: Session, raw: str, usage: str) -> Game:
    code = normalize_code(raw)
    if code is None:
        raise Refusal(usage)
    game = await repo.get_game_by_code(s.db, code)
    if game is None:
        raise Refusal(texts.ADMIN_GAME_NOT_FOUND)
    return game


async def _admin_game(s: Session, game_id: int) -> Game:
    game = await repo.get_game(s.db, game_id)
    if game is None:
        raise Refusal(texts.ADMIN_GAME_NOT_FOUND)
    return game


def _require_open(game: Game) -> None:
    if game.status not in (GameStatus.COLLECTING, GameStatus.DRAWN):
        raise Refusal(texts.ADMIN_GAME_CLOSED)


async def _summary(s: Session, game: Game) -> OutMessage:
    counts = await games.game_counts(s.db, game.id)
    return views.admin_game(game, counts, await repo.payments_of_game(s.db, game.id))


def _tier(raw: str) -> Tier | None:
    return next((tier for tier in PAID_TIERS if tier == raw.upper()), None)


# --- settings --------------------------------------------------------------------------------------------


@command("/price", admin_only=True)
async def _price(s: Session, rest: str) -> None:
    """/price shows the prices; /price S 490, /price free 10, /price limit_S 30 change one value."""
    settings = await s.ctx.settings()
    if rest:
        parts = rest.split()
        key = _PRICE_KEYS.get(parts[0].lower()) if len(parts) == 2 else None
        if key is None or not parts[1].isdigit():
            raise Refusal(texts.PRICE_USAGE)
        settings = Settings(**{**dataclasses.asdict(settings), key: int(parts[1])})
        problems = validate_price_list(PriceList.from_settings(settings))
        if problems:
            raise Refusal("\n".join(problems))
        await repo.set_setting(s.db, key, int(parts[1]))
    await s.say(texts.price_list(
        free_limit=settings.free_limit, price_S=settings.price_S, limit_S=settings.limit_S,
        price_M=settings.price_M, limit_M=settings.limit_M, price_L=settings.price_L, limit_L=settings.limit_L,
    ))


@command("/maintenance", admin_only=True)
async def _maintenance(s: Session, rest: str) -> None:
    switch = _SWITCH.get(rest.lower())
    if switch is None:
        raise Refusal(texts.MAINTENANCE_USAGE)
    await repo.set_setting(s.db, "maintenance", switch)
    await s.say(texts.MAINTENANCE_ON if switch else texts.MAINTENANCE_OFF)


# --- blocking (§5.7) --------------------------------------------------------------------------------------


@command("/block", admin_only=True)
async def _block(s: Session, rest: str) -> None:
    user_id = _user_id(rest)
    if not await repo.set_blocked(s.db, user_id, True):
        raise Refusal(texts.USER_NOT_FOUND)
    await s.say(OutMessage(texts.user_blocked(user_id), kb.keyboard(views.unblock_button(user_id))))


@command("/unblock", admin_only=True)
async def _unblock(s: Session, rest: str) -> None:
    await _set_unblocked(s, _user_id(rest))


@on(Action.UNBLOCK, admin_only=True)
async def _unblock_button(s: Session, args: Args) -> None:
    await _set_unblocked(s, args.number(0))


async def _set_unblocked(s: Session, user_id: int) -> None:
    if not await repo.set_blocked(s.db, user_id, False):
        raise Refusal(texts.USER_NOT_FOUND)
    await s.say(texts.user_unblocked(user_id))


def _user_id(raw: str) -> int:
    if not raw.isdigit():
        raise Refusal(texts.BLOCK_USAGE)
    return int(raw)
