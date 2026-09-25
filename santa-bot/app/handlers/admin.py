"""Admin commands and buttons for ADMIN_USER_IDS (§9). /whoami (in ``private``) works for anyone.

Admin-only commands are invisible to everyone else (they get the usual 'I don't
understand' reply) and work before consent. Replies never show wishes or
anonymous messages. Grants and cancellations take the game lock, like payments.

Deletion and moderation (§11, the privacy policy): /forget USERID deletes a person's
data on request; /clean CODE USERID and the report's [Стереть тексты отправителя]
remove what someone wrote in a game; /game's [Сбросить название] removes a title.

The ad autopilot (PROMO_SPEC §7), with PROMO_ENABLED=1: /ads and its subcommands,
/link and /links (manual tracking links), /channel (posts in the owner's MAX channel),
and the report's [Поднять до X ₽] [Не надо] buttons. These handlers only parse; the
work and the re-checks live in ``app.promo``.
"""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Awaitable, Callable

from app import repo
from app.config import MAX_LAG_DAYS, MAX_RAISE_PCT
from app.core import analytics, billing, games, kb, texts
from app.core.inputs import MAX_TITLE, clean_text, shorten
from app.core.models import Game, GameStatus, PaymentStatus, Settings, Tier
from app.core.payloads import normalize_code
from app.core.pricing import PAID_TIERS, PriceList, validate_price_list
from app.handlers import group, notices, views
from app.handlers.callbacks import Args, on
from app.handlers.private import command
from app.handlers.session import Outdated, Refusal, Session
from app.handlers.views import Action
from app.max_api import OutMessage
from app.promo import autopilot, channel, content, links
from app.promo import store as promo_store
from app.promo.attribution import cohort_cutoff
from app.promo.platforms import AdApiError, AuthError, Platform

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
    await group.card_changed(s.ctx, game_id)


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
    await group.card_changed(s.ctx, game_id)


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


def _user_id(raw: str, usage: str = texts.BLOCK_USAGE) -> int:
    if not raw.isdigit():
        raise Refusal(usage)
    return int(raw)


# --- deletion on request and moderation (§11) ------------------------------------------------------------


@command("/forget", admin_only=True)
async def _forget(s: Session, rest: str) -> None:
    user_id = _user_id(rest, texts.FORGET_USAGE)
    if await repo.get_user(s.db, user_id) is None:
        raise Refusal(texts.USER_NOT_FOUND)
    await s.say(views.confirm_forget(user_id))


@on(Action.ADMIN_FORGET_CONFIRM, admin_only=True)
async def _forget_confirm(s: Session, args: Args) -> None:
    user_id = args.number(0)
    erasure = await games.forget_user(s.db, user_id, s.now())
    if erasure is None:
        raise Refusal(texts.USER_NOT_FOUND)
    for game, people in erasure.cancelled:
        await notices.game_cancelled(s.ctx, game, people)
        await group.card_erased(s.ctx, game)
    for game, departure in erasure.departures:
        await notices.departed(s.ctx, game, departure)
        await group.card_changed(s.ctx, game.id)
    await s.say(texts.user_forgotten(user_id=user_id, cancelled=len(erasure.cancelled),
                                     left=len(erasure.departures)))


@command("/clean", admin_only=True)
async def _clean(s: Session, rest: str) -> None:
    parts = rest.split()
    if len(parts) != 2:
        raise Refusal(texts.CLEAN_USAGE)
    game = await _game_by_code(s, parts[0], texts.CLEAN_USAGE)
    await _clear_texts(s, game, _user_id(parts[1], texts.CLEAN_USAGE))


@on(Action.ADMIN_CLEAR_REPORTED, admin_only=True)
async def _clear_reported(s: Session, args: Args) -> None:
    report = await repo.get_report(s.db, args.number(0))
    game = None if report is None or report.game_id is None else await repo.get_game(s.db, report.game_id)
    if report is None or game is None:
        raise Outdated("no such report or game")
    await _clear_texts(s, game, report.reported_id)


async def _clear_texts(s: Session, game: Game, user_id: int) -> None:
    if await games.clear_texts(s.db, game.id, user_id) is None:
        raise Refusal(texts.NOT_IN_THAT_GAME)
    await s.say(texts.texts_cleared(user_id=user_id, code=game.code))
    await group.card_changed(s.ctx, game.id)


@on(Action.ADMIN_RESET_TITLE, admin_only=True)
async def _reset_title(s: Session, args: Args) -> None:
    game = await _admin_game(s, args.number(0))
    await repo.update_game(s.db, game.id, title=texts.DEFAULT_TITLE)
    await s.say(texts.title_reset(game.code))
    await group.card_changed(s.ctx, game.id)


# --- the ad autopilot, tracking links and the own channel (PROMO_SPEC §7) ----------------------------------------

Subcommand = Callable[[Session, str], Awaitable[None]]
_AD_PLATFORMS = {"yd": Platform.DIRECT, "vk": Platform.VK}
_CAMPAIGN_ID = re.compile(r"^[0-9]{1,14}$")  # the source yd<id> must fit the landing's 16 characters
_RULES = ("pause_cpa_rub", "scale_cpa_rub", "min_spend_rub", "max_raise_pct", "lag_days")  # /ads rules, in order


def _require_promo(s: Session) -> None:
    if not s.ctx.config.promo.enabled:
        raise Refusal(texts.PROMO_DISABLED)


@command("/ads", admin_only=True)
async def _ads(s: Session, rest: str) -> None:
    _require_promo(s)
    word, _, args = rest.partition(" ")
    handler = _ADS_SUBCOMMANDS.get(word.lower())
    if handler is None:
        raise Refusal(texts.PROMO_ADS_USAGE)
    await handler(s, args.strip())


async def _ads_status(s: Session, args: str) -> None:
    await s.say(await autopilot.status(s.ctx))


async def _ads_add(s: Session, args: str) -> None:
    parts = args.split(maxsplit=2)
    platform = _AD_PLATFORMS.get(parts[0].lower()) if parts else None
    if platform is None or len(parts) < 2 or not _CAMPAIGN_ID.match(parts[1]):
        raise Refusal(texts.PROMO_ADD_USAGE)
    name = shorten(clean_text(parts[2]), MAX_TITLE) if len(parts) == 3 else ""
    try:
        campaign, checked = await autopilot.register(s.ctx, platform, parts[1], name)
    except autopilot.CampaignNotFound:
        raise Refusal(texts.promo_not_found(platform=platform, external_id=parts[1])) from None
    except AuthError as error:
        raise Refusal(texts.promo_auth_alert(platform=platform, code=error.code, detail=error.detail)) from None
    except AdApiError as error:
        raise Refusal(texts.promo_unreachable(platform=platform, detail=autopilot.describe(error))) from None
    config = s.ctx.config
    await s.say(texts.promo_registered(
        platform=platform, name=campaign.name, src=campaign.src, validated=checked,
        landing_url=links.landing_url(config, campaign.src), template=links.campaign_template(config, platform),
    ))


async def _ads_remove(s: Session, args: str) -> None:
    await s.say(await autopilot.unregister(s.ctx, _src(args)))


async def _ads_auto(s: Session, args: str) -> None:
    switch = _SWITCH.get(args.lower())
    if switch is None:
        raise Refusal(texts.PROMO_AUTO_USAGE)
    await s.say(await autopilot.set_auto(s.ctx, switch))


async def _ads_stop(s: Session, args: str) -> None:
    await s.say(await autopilot.stop_all(s.ctx, s.user_id))


async def _ads_resume(s: Session, args: str) -> None:
    await s.say(await autopilot.resume(s.ctx, _src(args), s.user_id))


async def _ads_budget(s: Session, args: str) -> None:
    parts = args.split()
    if len(parts) != 2 or not _is_rubles(parts[1]):
        raise Refusal(texts.PROMO_BUDGET_USAGE)
    await s.say(await autopilot.set_budget(s.ctx, _src(parts[0]), int(parts[1]) * 100, s.user_id))


async def _ads_cap(s: Session, args: str) -> None:
    if not _is_rubles(args):
        raise Refusal(texts.PROMO_CAP_USAGE)
    await s.say(await autopilot.set_cap(s.ctx, int(args)))


async def _ads_rules(s: Session, args: str) -> None:
    parts = args.split()
    if not 2 <= len(parts) <= len(_RULES) or not all(part.isascii() and part.isdigit() for part in parts):
        raise Refusal(texts.PROMO_RULES_USAGE)
    values = dict(zip(_RULES, map(int, parts), strict=False))
    if not _rules_valid(values):
        raise Refusal(texts.PROMO_RULES_USAGE)
    await s.say(await autopilot.set_rules(s.ctx, **values))


def _rules_valid(values: dict[str, int]) -> bool:
    """Thresholds from 1 ₽ with ДЁШЕВО below ДОРОГО, a raise of 1–100%, a delay of 0–14 days (like .env)."""
    return (
        min(values["pause_cpa_rub"], values["scale_cpa_rub"], values.get("min_spend_rub", 1)) >= 1
        and values["scale_cpa_rub"] < values["pause_cpa_rub"]
        and 1 <= values.get("max_raise_pct", 1) <= MAX_RAISE_PCT
        and values.get("lag_days", 0) <= MAX_LAG_DAYS
    )


_ADS_SUBCOMMANDS: dict[str, Subcommand] = {
    "": _ads_status,
    "add": _ads_add,
    "remove": _ads_remove,
    "auto": _ads_auto,
    "stop": _ads_stop,
    "resume": _ads_resume,
    "budget": _ads_budget,
    "cap": _ads_cap,
    "rules": _ads_rules,
}


def _src(raw: str) -> str:
    if not raw.strip():
        raise Refusal(texts.PROMO_SRC_USAGE)
    return raw.strip().lower()


def _is_rubles(raw: str) -> bool:
    return raw.isascii() and raw.isdigit() and int(raw) >= 1


@on(Action.PROMO_APPROVE, admin_only=True)
async def _promo_approve(s: Session, args: Args) -> None:
    _require_promo(s)
    action_id = args.number(0)
    await s.acknowledge()  # the platform may take longer to answer than a button may wait
    await s.show(await autopilot.approve(s.ctx, action_id, s.user_id))


@on(Action.PROMO_DECLINE, admin_only=True)
async def _promo_decline(s: Session, args: Args) -> None:
    _require_promo(s)
    await s.show(await autopilot.decline(s.ctx, args.number(0), s.user_id))


@command("/link", admin_only=True)
async def _link(s: Session, rest: str) -> None:
    _require_promo(s)
    slug, _, title = rest.partition(" ")
    src = links.link_src(slug)
    if src is None:
        raise Refusal(texts.LINK_USAGE)
    link, created = await links.create_link(s.db, src, title, s.user_id, s.now())
    config = s.ctx.config
    await s.say(texts.link_created(title=link.title, src=src, landing_url=links.landing_url(config, src),
                                   bot_url=links.bot_url(config, src), existed=not created))


@command("/links", admin_only=True)
async def _links(s: Session, rest: str) -> None:
    _require_promo(s)
    settings = await promo_store.get_settings(s.db)
    cutoff = cohort_cutoff(s.today(), settings.lag_days, s.ctx.config.tz)
    entries = await links.links_with_metrics(s.db, cutoff)
    if not entries:
        raise Refusal(texts.LINKS_EMPTY)
    await s.say(texts.links_report([
        texts.link_line(title=link.title, entry=texts.promo_link_entry(
            slug=link.src[1:], users=m.users, games=m.games, games3=m.games3, paid_rub=m.paid_rub))
        for link, m in entries
    ]))


@command("/channel", admin_only=True)
async def _channel(s: Session, rest: str) -> None:
    _require_promo(s)
    word, _, args = rest.partition(" ")
    handler = _CHANNEL_SUBCOMMANDS.get(word.lower())
    if handler is None:
        raise Refusal(texts.CHANNEL_USAGE)
    await handler(s, args.strip())


async def _channel_status(s: Session, args: str) -> None:
    ctx, tz = s.ctx, s.ctx.config.tz
    statuses = list((await promo_store.post_statuses(s.db)).values())
    upcoming = [
        texts.channel_upcoming_line(post_id=post.id, when=channel.when_text(at, tz),
                                    first_line=post.text.split("\n", 1)[0])
        for post, at in await channel.upcoming(ctx, 3)
    ]
    await s.say(texts.channel_status(
        on=(await promo_store.get_settings(s.db)).channel_on, channel_id=ctx.config.promo.channel_id,
        sent=statuses.count(promo_store.PostStatus.SENT), skipped=statuses.count(promo_store.PostStatus.SKIPPED),
        total=len(content.CALENDAR), upcoming=upcoming, known=await promo_store.known_channels(s.db),
    ))


async def _channel_test(s: Session, args: str) -> None:
    upcoming = await channel.upcoming(s.ctx, 1)
    if not upcoming:
        raise Refusal(texts.CHANNEL_NOTHING_LEFT)
    post, at = upcoming[0]
    await s.say(texts.channel_preview(post_id=post.id, when=channel.when_text(at, s.ctx.config.tz)))
    await s.say(channel.post_message(s.ctx.config, post))


async def _channel_on(s: Session, args: str) -> None:
    channel_id = s.ctx.config.promo.channel_id
    if channel_id is None:
        raise Refusal(texts.CHANNEL_NO_ID)
    await promo_store.set_settings(s.db, channel_on=True)
    await s.say(texts.channel_on(channel_id=channel_id))


async def _channel_off(s: Session, args: str) -> None:
    await promo_store.set_settings(s.db, channel_on=False)
    await s.say(texts.CHANNEL_OFF)


async def _channel_send(s: Session, args: str) -> None:
    channel_id = s.ctx.config.promo.channel_id
    if channel_id is None:
        raise Refusal(texts.CHANNEL_NO_ID)
    post = content.post_by_id(args)
    if post is None:
        raise Refusal(texts.channel_unknown_post(args))
    if not await channel.publish(s.ctx, channel_id, post):
        raise Refusal(texts.channel_already_sent(post.id))
    await s.say(texts.channel_sent(post.id))


_CHANNEL_SUBCOMMANDS: dict[str, Subcommand] = {
    "": _channel_status,
    "test": _channel_test,
    "on": _channel_on,
    "off": _channel_off,
    "send": _channel_send,
}
