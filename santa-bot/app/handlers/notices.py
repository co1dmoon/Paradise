"""Messages to people other than the one who acted, queued through the outbox (§5.4–§5.6).

Queued messages survive restarts and respect the rate limits. Dedupe keys make
the payment notices idempotent, so the ResultURL handler may call
``payment_applied`` for every confirmation it processes.
"""

from __future__ import annotations

from collections.abc import Sequence

from app import repo
from app.context import AppContext
from app.core import texts
from app.core.billing import PaymentOutcome
from app.core.games import DrawResult
from app.core.models import Game, Participant
from app.core.users import display_name_from_profile
from app.handlers import views
from app.max_api import OutMessage, Target
from app.outbox import PURPOSE_DRAW_RESULT


async def _to(ctx: AppContext, user_id: int, message: OutMessage | str, *, dedupe_key: str | None = None,
              game_id: int | None = None) -> None:
    body = OutMessage(message) if isinstance(message, str) else message
    await ctx.outbox.enqueue(Target.user(user_id), body, disable_preview=True, dedupe_key=dedupe_key, game_id=game_id)


async def participants_activated(ctx: AppContext, game: Game, activated: Sequence[Participant]) -> None:
    """People moved from the queue into the game: 'Место появилось…' with a wishes prompt."""
    for participant in activated:
        await _to(ctx, participant.user_id, views.spot_opened(game), game_id=game.id)


async def participant_removed(ctx: AppContext, game: Game, person: Participant) -> None:
    await _to(ctx, person.user_id, texts.removed_notice(game.title), game_id=game.id)


async def game_cancelled(ctx: AppContext, game: Game, people: Sequence[Participant]) -> None:
    for person in people:
        await _to(ctx, person.user_id, texts.game_cancelled(game.title), game_id=game.id)


async def wish_reminders(ctx: AppContext, game: Game, people: Sequence[Participant]) -> None:
    for person in people:
        await _to(ctx, person.user_id, views.wish_reminder(game), game_id=game.id)


async def draw_results(ctx: AppContext, result: DrawResult, *, redraw: bool = False) -> None:
    """Queue every participant's pair in one transaction (§5.5).

    The purpose lets the delivery hook record result_dm_ok and send the organizer the
    'Пары отправлены' summary once everything is sent or dead.
    """
    game = result.game
    async with ctx.db.transaction() as tx:
        for giver_id in result.pairs:
            await ctx.outbox.enqueue(
                Target.user(giver_id), views.draw_result(game, result.receiver_for(giver_id), redraw=redraw),
                disable_preview=True, dedupe_key=f"draw:{game.id}:{game.redraw_count}:{giver_id}",
                purpose=PURPOSE_DRAW_RESULT, game_id=game.id, db=tx,
            )


async def payment_applied(ctx: AppContext, outcome: PaymentOutcome) -> None:
    """After a tier was applied (§5.6): the queue, the payer, the organizer and the admins."""
    game, payment = outcome.game, outcome.payment
    if game is None:
        return
    if outcome.duplicates:
        first, *_, last = outcome.duplicates
        await ctx.alerts.notify_admins(texts.double_payment(code=game.code, first_inv=first, second_inv=last))
    if outcome.game_not_collecting:
        await ctx.alerts.notify_admins(texts.payment_after_draw(code=game.code, inv_id=payment.inv_id))
    await participants_activated(ctx, game, outcome.activated)
    if payment.payer_id is None:
        return
    limit = game.participant_limit
    await _to(ctx, payment.payer_id, texts.payment_received(title=game.title, limit=limit),
              dedupe_key=f"paid:{payment.inv_id}:payer", game_id=game.id)
    if payment.payer_id != game.organizer_id:
        payer = await payer_name(ctx, game, payment.payer_id)
        await _to(ctx, game.organizer_id, texts.payer_paid(payer=payer, title=game.title, limit=limit),
                  dedupe_key=f"paid:{payment.inv_id}:organizer", game_id=game.id)


async def payer_name(ctx: AppContext, game: Game, user_id: int) -> str:
    """The payer's name in this game, else their MAX name."""
    participant = await repo.get_participant(ctx.db, game.id, user_id)
    if participant is not None:
        return participant.display_name
    user = await repo.get_user(ctx.db, user_id)
    return display_name_from_profile(user.max_name if user else None)
