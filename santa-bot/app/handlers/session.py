"""One user's interaction with the bot: who they are, how to reply, how refusals read.

A ``Session`` is opened for every private update (message, start or button). It
answers the callback exactly once, before anything else is sent: ``show`` replaces
the message with the pressed button (navigation), ``say`` sends a new message, and
``acknowledge`` answers a callback that produced neither. Replies go out directly
through the rate limiter (``Outbox.send_now``); messages to other people go
through the outbox queue.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from app import repo
from app.context import AppContext
from app.core import games, texts, users
from app.core.billing import NoUpgradeAvailable
from app.core.games import ExclusionProblem, GameError
from app.core.models import GameStatus, User
from app.core.relay import AlreadyReported, AnonChatOff, PairChanged, RelayLimitReached
from app.db import Database
from app.max_api import CallbackQuery, OutMessage, Target, UserRef


class Outdated(Exception):
    """A button or pending input that no longer makes sense (bad payload, finished step)."""


class Refusal(GameError):
    """A refusal decided by a handler, with its own message."""

    def __init__(self, text: str) -> None:
        super().__init__(text)
        self.text = text


@dataclass(slots=True)
class Session:
    ctx: AppContext
    user: User
    profile: UserRef
    callback: CallbackQuery | None = None
    _answered: bool = False

    @property
    def user_id(self) -> int:
        return self.user.user_id

    @property
    def db(self) -> Database:
        return self.ctx.db

    @property
    def target(self) -> Target:
        return Target.user(self.user_id)

    @property
    def is_admin(self) -> bool:
        return self.user_id in self.ctx.config.admin_user_ids

    def now(self) -> datetime:
        return self.ctx.clock.now()

    def today(self) -> date:
        return self.ctx.today()

    async def say(self, message: OutMessage | str) -> None:
        """Send a new message to this user (answering a pending callback first)."""
        await self.acknowledge()
        await self.ctx.outbox.send_now(self.target, _message(message), disable_preview=True)

    async def show(self, message: OutMessage | str) -> None:
        """Replace the message whose button was pressed; outside a callback, send a new one.

        The callback is answered first, then the message is edited (PUT /messages), so a
        failed edit (e.g. the message was deleted) falls back to a new message.
        """
        mid = self.callback.message_mid if self.callback is not None else None
        await self.acknowledge()
        if mid is None or not await self.ctx.outbox.edit_now(self.target, mid, _message(message)):
            await self.ctx.outbox.send_now(self.target, _message(message), disable_preview=True)

    async def acknowledge(self) -> None:
        """Answer the callback with an empty body (once). Long actions call this first."""
        if self.callback is not None and not self._answered:
            self._answered = True
            await self.ctx.outbox.answer(self.target, self.callback.callback_id)

    async def refuse(self, error: GameError) -> None:
        await self.say(explain(error, self.ctx.config.support_email))

    async def reload_user(self) -> None:
        user = await repo.get_user(self.db, self.user_id)
        assert user is not None
        self.user = user


async def open_session(
    ctx: AppContext, profile: UserRef, *, first_source: str, callback: CallbackQuery | None = None
) -> Session:
    """Load (or create, pre-consent) the user and keep a consented user's MAX name current."""
    user = await users.ensure_user(ctx.db, profile.user_id, first_source, ctx.clock.now())
    if user.has_consent:
        await users.refresh_profile(ctx.db, user, profile.name, profile.username)
    return Session(ctx, user, profile, callback)


def _message(message: OutMessage | str) -> OutMessage:
    return OutMessage(message) if isinstance(message, str) else message


_STATUS_REFUSALS = {
    GameStatus.COLLECTING: texts.NOT_DRAWN_YET,
    GameStatus.DRAWN: texts.ALREADY_DRAWN_ACTION,
    GameStatus.FINISHED: texts.GAME_FINISHED,
    GameStatus.CANCELLED: texts.GAME_CANCELLED,
}
_EXCLUSION_REFUSALS = {
    ExclusionProblem.SAME_PERSON: texts.EXCLUSION_SAME_PERSON,
    ExclusionProblem.NOT_PARTICIPANT: texts.EXCLUSION_NOT_PARTICIPANT,
    ExclusionProblem.LIMIT: texts.EXCLUSIONS_LIMIT,
}


def explain(error: GameError, support_email: str) -> str:
    """The Russian message for an expected refusal from ``app.core``."""
    match error:
        case Refusal():
            return error.text
        case games.WrongStatus():
            return _STATUS_REFUSALS.get(GameStatus(error.args[0]), texts.BUTTON_OUTDATED)
        case games.ExclusionRejected():
            return _EXCLUSION_REFUSALS[error.reason]
        case games.UserBlocked():
            return texts.blocked(support_email)
    return _REFUSALS.get(type(error), texts.BUTTON_OUTDATED)


_REFUSALS: dict[type[GameError], str] = {
    games.GameNotFound: texts.BUTTON_OUTDATED,
    games.PermissionDenied: texts.NOT_ALLOWED,
    games.DailyLimitReached: texts.DAILY_LIMIT,
    games.NotEnoughParticipants: texts.DRAW_NEEDS_THREE,
    games.DrawImpossible: texts.DRAW_IMPOSSIBLE,
    games.RedrawLimitReached: texts.REDRAW_LIMIT,
    games.TooSoon: texts.REMINDER_TOO_SOON,
    games.RevealTooEarly: texts.REVEAL_TOO_EARLY,
    games.AlreadyRevealed: texts.REVEAL_ALREADY_DONE,
    PairChanged: texts.RELAY_PAIR_CHANGED,
    AnonChatOff: texts.ANON_CHAT_OFF,
    RelayLimitReached: texts.RELAY_DAILY_LIMIT,
    AlreadyReported: texts.REPORT_SENT,
    NoUpgradeAvailable: texts.UPGRADE_NOT_NEEDED,
}
