"""Messages to people other than the one who acted, queued through the outbox (§5.4–§5.6, §5.9).

Queued messages survive restarts and respect the rate limits. Dedupe keys make
the payment notices idempotent, so the ResultURL handler may call
``payment_applied`` for every confirmation it processes. The scheduler's notices
take ``db`` so they are queued in the same transaction that records them as sent.
"""

from __future__ import annotations

from collections.abc import Sequence

from app import repo
from app.context import AppContext
from app.core import texts
from app.core.billing import PaymentOutcome
from app.core.games import Departure, DrawResult, Reveal
from app.core.models import Game, Participant, StateKind
from app.core.pricing import Upgrade
from app.core.users import display_name_from_profile
from app.db import Db
from app.handlers import views
from app.max_api import OutMessage, Target
from app.outbox import PURPOSE_DRAW_RESULT


async def _to(ctx: AppContext, user_id: int, message: OutMessage | str, *, dedupe_key: str | None = None,
              game_id: int | None = None, db: Db | None = None) -> None:
    body = OutMessage(message) if isinstance(message, str) else message
    await ctx.outbox.enqueue(Target.user(user_id), body, disable_preview=True, dedupe_key=dedupe_key,
                             game_id=game_id, db=db)


async def participants_activated(ctx: AppContext, game: Game, activated: Sequence[Participant]) -> None:
    """People moved from the queue into the game: 'Место появилось… Напишите, что хотели бы получить.'

    Their next typed message becomes their wishes, unless they are typing something else.
    """
    for participant in activated:
        if not participant.wishes:
            await repo.set_state_if_idle(ctx.db, participant.user_id, StateKind.WISHES, ctx.clock.now(),
                                         game_id=game.id)
        await _to(ctx, participant.user_id, views.spot_opened(game), game_id=game.id)


async def departed(ctx: AppContext, game: Game, departure: Departure | None) -> None:
    """Someone left or was removed: people from the queue take the place (before the draw), or
    the leaver's Santa gets a new receiver (after it, P1); the organizer is asked to redraw
    when the new pair is excluded, or told why a redraw cannot help (fewer than 3 people
    remain, or both redraws are used up)."""
    if departure is None:
        return
    await participants_activated(ctx, game, departure.activated)
    splice = departure.splice
    if splice is None:
        return
    receiver = await repo.get_participant(ctx.db, game.id, splice.receiver_id)
    if receiver is not None:
        await _to(ctx, splice.giver_id, views.receiver_left(game, receiver), game_id=game.id)
    if splice.needs_redraw:
        await _to(ctx, game.organizer_id, views.splice_problem(game, too_few=splice.too_few), game_id=game.id)


async def participant_removed(ctx: AppContext, game: Game, person: Participant) -> None:
    await _to(ctx, person.user_id, texts.removed_notice(game.title), game_id=game.id)


async def game_cancelled(ctx: AppContext, game: Game, people: Sequence[Participant]) -> None:
    for person in people:
        await _to(ctx, person.user_id, texts.game_cancelled(game.title), game_id=game.id)


async def game_cancelled_by_service(ctx: AppContext, game: Game, people: Sequence[Participant]) -> None:
    """An admin cancelled the game: everyone in it and the organizer are told."""
    text = texts.game_cancelled_by_service(title=game.title, support_email=ctx.config.support_email)
    for user_id in dict.fromkeys([game.organizer_id, *(person.user_id for person in people)]):
        await _to(ctx, user_id, text, game_id=game.id)


async def game_upgraded(ctx: AppContext, game: Game, *, db: Db) -> None:
    """/grant: the organizer learns the game now takes more people."""
    await _to(ctx, game.organizer_id, texts.game_upgraded(title=game.title, limit=game.participant_limit),
              game_id=game.id, db=db)


async def wish_reminders(ctx: AppContext, game: Game, people: Sequence[Participant]) -> None:
    for person in people:
        await _to(ctx, person.user_id, views.wish_reminder(game), game_id=game.id)


async def draw_results(ctx: AppContext, result: DrawResult, *, redraw: bool = False) -> None:
    """Queue every participant's pair in one transaction (§5.5).

    The purpose lets the delivery hook record result_dm_ok and send the organizer the
    'Пары отправлены' summary once everything is sent or dead. A redraw drops the old
    pairs still waiting in the queue, so they can never arrive after the new ones.
    """
    game = result.game
    async with ctx.db.transaction() as tx:
        if redraw:
            await ctx.outbox.cancel_pending(PURPOSE_DRAW_RESULT, game.id, db=tx)
        for giver_id in result.pairs:
            await ctx.outbox.enqueue(
                Target.user(giver_id), views.draw_result(game, result.receiver_for(giver_id), redraw=redraw),
                disable_preview=True, dedupe_key=f"draw:{game.id}:{game.redraw_count}:{giver_id}",
                purpose=PURPOSE_DRAW_RESULT, game_id=game.id, db=tx,
            )


async def payment_applied(ctx: AppContext, outcome: PaymentOutcome) -> None:
    """After a payment was applied (§5.6): the queue, the payer, the organizer and the admins.

    Money that bought nothing (the game was drawn or cancelled meanwhile, or an older link
    was paid after another upgrade) is announced to the admins for a refund and to the
    payer as such; the organizer hears about a payment only when it enlarged the game.
    """
    game, payment = outcome.game, outcome.payment
    if game is None:
        return
    await _alert_admins(ctx, game, outcome)
    await participants_activated(ctx, game, outcome.activated)
    if payment.payer_id is None:
        return
    await _to(ctx, payment.payer_id, _payer_text(ctx, game, outcome),
              dedupe_key=f"paid:{payment.inv_id}:payer", game_id=game.id)
    if payment.payer_id != game.organizer_id and not outcome.fully_excess:
        payer = await payer_name(ctx, game, payment.payer_id)
        text = texts.payer_paid(payer=payer, title=game.title, limit=game.participant_limit)
        await _to(ctx, game.organizer_id, text, dedupe_key=f"paid:{payment.inv_id}:organizer", game_id=game.id)


async def _alert_admins(ctx: AppContext, game: Game, outcome: PaymentOutcome) -> None:
    payment = outcome.payment
    if outcome.game_not_collecting:
        await ctx.alerts.notify_admins(texts.payment_after_draw(code=game.code, inv_id=payment.inv_id))
    elif outcome.duplicates:
        first, *_, last = outcome.duplicates
        await ctx.alerts.notify_admins(texts.double_payment(code=game.code, first_inv=first, second_inv=last))
    elif outcome.excess_rub:
        await ctx.alerts.notify_admins(texts.overpayment(
            code=game.code, inv_id=payment.inv_id, amount=payment.amount_rub, excess=outcome.excess_rub))


def _payer_text(ctx: AppContext, game: Game, outcome: PaymentOutcome) -> str:
    email, amount = ctx.config.support_email, outcome.payment.amount_rub
    if outcome.game_not_collecting:
        return texts.payment_too_late(title=game.title, amount=amount, support_email=email)
    if outcome.fully_excess:
        return texts.payment_not_needed(title=game.title, limit=game.participant_limit, amount=amount,
                                        support_email=email)
    received = texts.payment_received(title=game.title, limit=game.participant_limit)
    if outcome.excess_rub:
        return f"{received} {texts.partial_refund(excess=outcome.excess_rub, support_email=email)}"
    return received


async def payer_name(ctx: AppContext, game: Game, user_id: int) -> str:
    """The payer's name in this game, else their MAX name."""
    participant = await repo.get_participant(ctx.db, game.id, user_id)
    if participant is not None:
        return participant.display_name
    user = await repo.get_user(ctx.db, user_id)
    return display_name_from_profile(user.max_name if user else None)


async def reveal(ctx: AppContext, revealed: Reveal) -> None:
    """§5.11 in link mode: every participant gets the chain with [Устроить игру в другом чате]."""
    game = revealed.game
    message = views.reveal(game, revealed.chain)
    for person in revealed.people:
        await _to(ctx, person.user_id, message, dedupe_key=f"reveal:{game.id}:{person.user_id}", game_id=game.id)


# --- §5.9 the scheduler's notices --------------------------------------------------------------------


async def join_notice(ctx: AppContext, game: Game, newcomers: Sequence[Participant], active: int, *, db: Db) -> None:
    names = [person.display_name for person in newcomers]
    await _to(ctx, game.organizer_id, views.join_notice(game, names, active), game_id=game.id, db=db)


async def waiting_notice(ctx: AppContext, game: Game, waiting: int, upgrade: Upgrade | None, *, db: Db) -> None:
    """Without an upgrade to offer, a game at the top tier points to the e-mail for bigger games (§4)."""
    top_tier = upgrade is None and ctx.config.payments_enabled
    contact = ctx.config.support_email if top_tier else None
    await _to(ctx, game.organizer_id, views.waiting_notice(game, waiting, upgrade, contact_email=contact),
              game_id=game.id, db=db)


async def organizer_nudge(ctx: AppContext, game: Game, days: int, active: int, *, db: Db) -> None:
    await _to(ctx, game.organizer_id, views.organizer_nudge(game, days, active),
              dedupe_key=f"nudge:{game.id}:{game.exchange_date}", game_id=game.id, db=db)


async def pre_exchange_reminders(
    ctx: AppContext, game: Game, pairs: Sequence[tuple[int, Participant]], *, db: Db
) -> None:
    """One reminder per giver: ``pairs`` are (giver id, receiver)."""
    for giver_id, receiver in pairs:
        await _to(ctx, giver_id, views.pre_exchange_reminder(game, receiver),
                  dedupe_key=f"pre:{game.id}:{game.exchange_date}:{giver_id}", game_id=game.id, db=db)
