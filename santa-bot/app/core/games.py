"""Platform-agnostic game logic (§5.2–§5.5) over the data layer.

Every operation re-reads the game and re-checks permissions from the database
(§5 global rules): callers pass the acting user's id, never trusted payload
state. Expected refusals raise ``GameError`` subclasses which handlers map to
Russian texts. Callers serialize work on one game with the per-game lock
(``app.locks``); each operation is additionally one DB transaction.
"""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from enum import StrEnum
from typing import Any

from app import repo
from app.core import texts
from app.core.analytics import Event, record
from app.core.clock import from_iso, to_iso
from app.core.draw import MIN_PARTICIPANTS, SEARCH_BUDGET_SECONDS, draw_cycle
from app.core.inputs import MAX_BUDGET, MAX_NAME, MAX_TITLE, MAX_WISHES
from app.core.models import (
    Game,
    GameStatus,
    JoinVia,
    Participant,
    ParticipantStatus,
    Settings,
    Tier,
    User,
)
from app.core.payloads import generate_code
from app.core.users import FALLBACK_NAME, display_name_from_profile
from app.db import Db

DAILY_GAME_LIMIT = 20
MAX_EXCLUSIONS = 50
MAX_REDRAWS = 2
WISH_REMINDER_INTERVAL = timedelta(hours=24)
_CODE_ATTEMPTS = 20

ACTIVE = ParticipantStatus.ACTIVE
WAITING = ParticipantStatus.WAITING


class GameError(Exception):
    """An expected refusal; handlers turn it into a message for the user."""


class GameNotFound(GameError):
    pass


class PermissionDenied(GameError):
    pass


class WrongStatus(GameError):
    """The game is not in a status that allows this action (e.g. already drawn)."""


class DailyLimitReached(GameError):
    pass


class UserBlocked(GameError):
    pass


class NotEnoughParticipants(GameError):
    pass


class DrawImpossible(GameError):
    """No cycle satisfies the exclusions."""


class RedrawLimitReached(GameError):
    pass


class RedrawAlreadyDone(GameError):
    """The redraw this confirmation was shown for has already happened (double tap, stale button)."""


class TooSoon(GameError):
    """A throttled action (the wish reminder) was used less than 24 h ago."""


class RevealTooEarly(GameError):
    """The pairs can be revealed from the exchange date on."""


class AlreadyRevealed(GameError):
    pass


class ExclusionProblem(StrEnum):
    SAME_PERSON = "same_person"
    NOT_PARTICIPANT = "not_participant"
    LIMIT = "limit"


class ExclusionRejected(GameError):
    def __init__(self, reason: ExclusionProblem) -> None:
        super().__init__(reason.value)
        self.reason = reason


class JoinOutcome(StrEnum):
    JOINED = "joined"
    WAITING = "waiting"
    ALREADY_IN = "already_in"
    DRAWN = "drawn"
    CANCELLED = "cancelled"
    REMOVED = "removed"


@dataclass(frozen=True, slots=True)
class GameDraft:
    """Answers collected by the creation wizard (§5.2), already validated by the handler."""

    title: str
    budget_text: str
    exchange_date: date | None
    organizer_participates: bool
    source_game_id: int | None = None
    group_chat_id: int | None = None


@dataclass(frozen=True, slots=True)
class JoinResult:
    outcome: JoinOutcome
    game: Game
    participant: Participant | None


@dataclass(frozen=True, slots=True)
class Splice:
    """After someone leaves a drawn game, their giver now gifts their receiver (§5.5)."""

    giver_id: int
    receiver_id: int
    excluded: bool
    too_few: bool

    @property
    def needs_redraw(self) -> bool:
        return self.excluded or self.too_few


@dataclass(frozen=True, slots=True)
class Departure:
    participant: Participant
    activated: list[Participant]
    splice: Splice | None


@dataclass(frozen=True, slots=True)
class Erasure:
    """What deleting a user's data changed in games that were still running."""

    cancelled: list[tuple[Game, list[Participant]]]  # games they organized, and who must be told
    departures: list[tuple[Game, Departure]]  # games they played in


@dataclass(frozen=True, slots=True)
class GameCounts:
    active: int
    waiting: int
    without_wishes: int
    exclusions: int
    gifts_ready: int


@dataclass(frozen=True, slots=True)
class DrawResult:
    game: Game
    pairs: dict[int, int]
    participants: dict[int, Participant]

    def receiver_for(self, giver_id: int) -> Participant:
        return self.participants[self.pairs[giver_id]]


@dataclass(frozen=True, slots=True)
class Reveal:
    """Who was whose Santa (§5.11): ``chain`` holds (giver, receiver) names in cycle order."""

    game: Game
    chain: list[tuple[str, str]]
    people: list[Participant]


@dataclass(frozen=True, slots=True)
class DeliverySummary:
    """All draw results were sent or failed: what to tell the organizer."""

    game: Game
    sent: int
    total: int
    failed_names: list[str]


# --- loading and permissions -------------------------------------------------------


async def load_game(db: Db, game_id: int) -> Game:
    game = await repo.get_game(db, game_id)
    if game is None:
        raise GameNotFound(game_id)
    return game


def require_organizer(game: Game, user_id: int) -> None:
    if game.organizer_id != user_id:
        raise PermissionDenied(f"user {user_id} is not the organizer of game {game.id}")


def require_status(game: Game, *allowed: GameStatus) -> None:
    if game.status not in allowed:
        raise WrongStatus(game.status)


async def require_participant(
    db: Db, game_id: int, user_id: int, *statuses: ParticipantStatus
) -> Participant:
    """The user's participant row with one of ``statuses`` (default: active)."""
    participant = await repo.get_participant(db, game_id, user_id)
    if participant is None or participant.status not in (statuses or (ACTIVE,)):
        raise PermissionDenied(f"user {user_id} is not an eligible participant of game {game_id}")
    return participant


async def _load_organized(db: Db, game_id: int, organizer_id: int, *allowed: GameStatus) -> Game:
    game = await load_game(db, game_id)
    require_organizer(game, organizer_id)
    if allowed:
        require_status(game, *allowed)
    return game


def _require_text(value: str, limit: int) -> None:
    if not value or len(value) > limit:
        raise ValueError(f"text must be 1-{limit} characters")


# --- creation --------------------------------------------------------------------


async def create_game(
    db: Db,
    organizer_id: int,
    draft: GameDraft,
    settings: Settings,
    *,
    now: datetime,
    today: date,
    rng: random.Random,
) -> Game:
    """Create a collecting free game (§5.2); the organizer joins if they participate."""
    title = draft.title or texts.DEFAULT_TITLE
    _require_text(title, MAX_TITLE)
    _require_text(draft.budget_text, MAX_BUDGET)
    async with db.transaction() as tx:
        user = await repo.get_user(tx, organizer_id)
        if user is None:
            raise PermissionDenied(f"user {organizer_id} does not exist")
        check_can_create(user, today)
        await repo.bump_games_created(tx, organizer_id, today.isoformat())
        source = await _game_source(tx, user, draft.source_game_id)
        game_id = await repo.insert_game(
            tx,
            {
                "code": await _unique_code(tx, rng),
                "title": title,
                "organizer_id": organizer_id,
                "organizer_participates": draft.organizer_participates,
                "budget_text": draft.budget_text,
                "exchange_date": draft.exchange_date.isoformat() if draft.exchange_date else None,
                "status": GameStatus.COLLECTING,
                "tier": Tier.FREE,
                "participant_limit": settings.free_limit,
                "group_chat_id": draft.group_chat_id,
                "source_game_id": draft.source_game_id,
                "source": source,
                "created_at": to_iso(now),
            },
        )
        if draft.organizer_participates:
            await repo.upsert_participant(
                tx, game_id, organizer_id, _name_of(user), ACTIVE, JoinVia.ORGANIZER, now
            )
        await record(tx, Event.GAME_CREATED, now, user_id=organizer_id, game_id=game_id, source=source)
        return await load_game(tx, game_id)


def check_can_create(user: User, today: date) -> None:
    """Raise when ``user`` may not create a game today: no consent, blocked, or 20 games already."""
    if not user.has_consent:
        raise PermissionDenied(f"user {user.user_id} has not consented")
    if user.blocked:
        raise UserBlocked(user.user_id)
    if user.games_created_day == today.isoformat() and user.games_created_today >= DAILY_GAME_LIMIT:
        raise DailyLimitReached(user.user_id)


async def _unique_code(db: Db, rng: random.Random) -> str:
    for _ in range(_CODE_ATTEMPTS):
        code = generate_code(rng)
        if not await repo.code_exists(db, code):
            return code
    raise RuntimeError("could not generate a unique game code")


async def _game_source(db: Db, user: User, source_game_id: int | None) -> str:
    """games.source (§6.8): ref, participant, s:src or direct."""
    if source_game_id is not None:
        return "ref"
    if user.first_source.startswith("j:"):
        return "participant"
    joined_elsewhere = await db.fetchval(
        "SELECT 1 FROM participants p JOIN games g ON g.id = p.game_id"
        " WHERE p.user_id = ? AND g.organizer_id <> ? AND NOT EXISTS ("
        "   SELECT 1 FROM games own WHERE own.organizer_id = ? AND own.created_at < p.joined_at)"
        " LIMIT 1",
        (user.user_id, user.user_id, user.user_id),
    )
    if joined_elsewhere:
        return "participant"
    if user.first_source.startswith("s:"):
        return user.first_source
    return "direct"


def _name_of(user: User) -> str:
    return display_name_from_profile(user.max_name)


# --- joining and leaving --------------------------------------------------------------


async def join_game(db: Db, game_id: int, user_id: int, via: JoinVia, now: datetime) -> JoinResult:
    """Join as active, or as waiting when the limit is reached (§5.3, §5.6)."""
    async with db.transaction() as tx:
        game = await load_game(tx, game_id)
        existing = await repo.get_participant(tx, game_id, user_id)
        if game.status == GameStatus.CANCELLED:
            return JoinResult(JoinOutcome.CANCELLED, game, existing)
        if existing is not None and existing.status in (ACTIVE, WAITING):
            return JoinResult(JoinOutcome.ALREADY_IN, game, existing)
        if game.status != GameStatus.COLLECTING:
            return JoinResult(JoinOutcome.DRAWN, game, existing)
        if existing is not None and existing.status == ParticipantStatus.REMOVED:
            return JoinResult(JoinOutcome.REMOVED, game, existing)
        user = await repo.get_user(tx, user_id)
        if user is None or not user.has_consent:
            raise PermissionDenied(f"user {user_id} has not consented")
        has_room = await repo.count_participants(tx, game_id, ACTIVE) < game.participant_limit
        status = ACTIVE if has_room else WAITING
        name = existing.display_name if existing else _name_of(user)
        participant = await repo.upsert_participant(tx, game_id, user_id, name, status, via, now)
        event = Event.JOIN if has_room else Event.JOIN_WAITING
        await record(tx, event, now, user_id=user_id, game_id=game_id, via=via)
        return JoinResult(JoinOutcome.JOINED if has_room else JoinOutcome.WAITING, game, participant)


async def leave_game(db: Db, game_id: int, user_id: int, now: datetime) -> Departure:
    """The participant leaves: before the draw frees a place; after it splices the cycle."""
    async with db.transaction() as tx:
        game = await load_game(tx, game_id)
        participant = await require_participant(tx, game_id, user_id, ACTIVE, WAITING)
        return await _depart(tx, game, participant, ParticipantStatus.LEFT, now)


async def remove_participant(
    db: Db, game_id: int, organizer_id: int, user_id: int, now: datetime
) -> Departure:
    """The organizer removes someone else; the organizer toggles their own participation instead."""
    async with db.transaction() as tx:
        game = await _load_organized(tx, game_id, organizer_id)
        if user_id == organizer_id:
            raise PermissionDenied("the organizer cannot remove themselves")
        participant = await require_participant(tx, game_id, user_id, ACTIVE, WAITING)
        return await _depart(tx, game, participant, ParticipantStatus.REMOVED, now)


async def set_organizer_participation(
    db: Db, game_id: int, organizer_id: int, participates: bool, now: datetime
) -> Departure | None:
    """'участвую/не участвую' in the settings (collecting), or the organizer leaving the exchange
    after the draw. Returns the departure when the organizer stopped taking part."""
    allowed = (GameStatus.COLLECTING,) if participates else (GameStatus.COLLECTING, GameStatus.DRAWN)
    async with db.transaction() as tx:
        game = await _load_organized(tx, game_id, organizer_id, *allowed)
        await repo.update_game(tx, game_id, organizer_participates=participates)
        existing = await repo.get_participant(tx, game_id, organizer_id)
        if participates:
            if existing is None or existing.status not in (ACTIVE, WAITING):
                user = await repo.get_user(tx, organizer_id)
                assert user is not None
                has_room = await repo.count_participants(tx, game_id, ACTIVE) < game.participant_limit
                name = existing.display_name if existing else _name_of(user)
                await repo.upsert_participant(
                    tx, game_id, organizer_id, name, ACTIVE if has_room else WAITING, JoinVia.ORGANIZER, now
                )
            return None
        if existing is None or existing.status not in (ACTIVE, WAITING):
            return None
        return await _depart(tx, game, existing, ParticipantStatus.LEFT, now)


async def _depart(
    db: Db, game: Game, participant: Participant, status: ParticipantStatus, now: datetime
) -> Departure:
    if game.status == GameStatus.COLLECTING:
        await repo.set_participant_status(db, game.id, participant.user_id, status)
        await repo.delete_exclusions_of(db, game.id, participant.user_id)
        activated = await activate_waiting(db, game.id) if participant.status == ACTIVE else []
        return Departure(participant, activated, None)
    if game.status == GameStatus.DRAWN:
        await repo.set_participant_status(db, game.id, participant.user_id, status)
        splice = await _splice(db, game.id, participant.user_id) if participant.status == ACTIVE else None
        return Departure(participant, [], splice)
    raise WrongStatus(game.status)


async def _splice(db: Db, game_id: int, leaving_id: int) -> Splice | None:
    giver = await repo.giver_of(db, game_id, leaving_id)
    receiver = await repo.receiver_of(db, game_id, leaving_id)
    await repo.delete_assignments_of(db, game_id, leaving_id)
    if giver is None or receiver is None or giver == receiver:
        return None
    await repo.insert_assignment(db, game_id, giver, receiver)
    await repo.set_gift_ready(db, game_id, giver, False)  # the gift was for the old receiver
    excluded = (min(giver, receiver), max(giver, receiver)) in set(await repo.exclusions(db, game_id))
    remaining = await repo.count_participants(db, game_id, ACTIVE)
    return Splice(giver, receiver, excluded=excluded, too_few=remaining < MIN_PARTICIPANTS)


async def activate_waiting(db: Db, game_id: int) -> list[Participant]:
    """Move waiting participants to active in join order while there is room (collecting only)."""
    game = await load_game(db, game_id)
    if game.status != GameStatus.COLLECTING:
        return []
    room = game.participant_limit - await repo.count_participants(db, game_id, ACTIVE)
    if room <= 0:
        return []
    promoted = (await repo.participants(db, game_id, WAITING))[:room]
    for participant in promoted:
        await repo.set_participant_status(db, game_id, participant.user_id, ACTIVE)
    return [await require_participant(db, game_id, p.user_id) for p in promoted]


# --- names and wishes -------------------------------------------------------------------


async def set_display_name(db: Db, game_id: int, user_id: int, name: str) -> None:
    _require_text(name, MAX_NAME)
    async with db.transaction() as tx:
        await require_participant(tx, game_id, user_id, ACTIVE, WAITING)
        await repo.set_display_name(tx, game_id, user_id, name)


async def set_wishes(db: Db, game_id: int, user_id: int, wishes: str, now: datetime) -> None:
    """Save wishes; they stay editable after the draw (§5.5)."""
    _require_text(wishes, MAX_WISHES)
    async with db.transaction() as tx:
        game = await load_game(tx, game_id)
        require_status(game, GameStatus.COLLECTING, GameStatus.DRAWN)
        await require_participant(tx, game_id, user_id, ACTIVE, WAITING)
        await repo.set_wishes(tx, game_id, user_id, wishes)
        await record(tx, Event.WISHES_SAVED, now, user_id=user_id, game_id=game_id)


# --- the user's data: deletion on request and moderation (§11) ---------------------------


async def forget_user(db: Db, user_id: int, now: datetime) -> Erasure | None:
    """Delete everything about a user, as the privacy policy promises (consent withdrawn,
    deletion asked). None when the user is unknown.

    Running games they organize are cancelled and then deleted with the rest of their
    games; from running games they play in they leave (a drawn cycle is spliced). Then
    their participations, wishes, pairs, exclusions, anonymous messages, complaints,
    pending input, queued messages and profile go; payments and events keep only the
    amounts and counts. The caller tells the people in the returned ``Erasure``.
    """
    async with db.transaction() as tx:
        if await repo.get_user(tx, user_id) is None:
            return None
        cancelled: list[tuple[Game, list[Participant]]] = []
        departures: list[tuple[Game, Departure]] = []
        for game, participant in await repo.games_of_user(tx, user_id):
            if game.status not in (GameStatus.COLLECTING, GameStatus.DRAWN):
                continue
            if game.organizer_id == user_id:
                people = [p for p in await _cancel(tx, game.id, now) if p.user_id != user_id]
                cancelled.append((await load_game(tx, game.id), people))
            elif participant is not None:
                departures.append((game, await _depart(tx, game, participant, ParticipantStatus.LEFT, now)))
        await repo.erase_user(tx, user_id)
        return Erasure(cancelled, departures)


async def clear_texts(db: Db, game_id: int, user_id: int) -> Participant | None:
    """Moderation: remove what a person wrote in one game (wishes, display name, anonymous
    messages). None when they never were in that game."""
    async with db.transaction() as tx:
        if await repo.get_participant(tx, game_id, user_id) is None:
            return None
        await repo.clear_participant_texts(tx, game_id, user_id, FALLBACK_NAME)
        return await repo.get_participant(tx, game_id, user_id)


# --- organizer tools --------------------------------------------------------------------


async def game_counts(db: Db, game_id: int) -> GameCounts:
    row = await db.fetchone(
        "SELECT"
        " COALESCE(SUM(status = 'active'), 0),"
        " COALESCE(SUM(status = 'waiting'), 0),"
        " COALESCE(SUM(status = 'active' AND COALESCE(wishes, '') = ''), 0),"
        " COALESCE(SUM(status = 'active' AND gift_ready = 1), 0)"
        " FROM participants WHERE game_id = ?",
        (game_id,),
    )
    assert row is not None
    return GameCounts(
        active=row[0], waiting=row[1], without_wishes=row[2], exclusions=len(await repo.exclusions(db, game_id)),
        gifts_ready=row[3],
    )


_SETTABLE = frozenset({"title", "budget_text", "exchange_date", "anon_chat", "reminder_on"})


async def update_game_settings(db: Db, game_id: int, organizer_id: int, **fields: Any) -> Game:
    """Change title, budget_text, exchange_date, anon_chat or reminder_on (§5.4 Настройки)."""
    unknown = set(fields) - _SETTABLE
    if unknown:
        raise ValueError(f"not a game setting: {sorted(unknown)}")
    if "title" in fields:
        _require_text(fields["title"], MAX_TITLE)
    if "budget_text" in fields:
        _require_text(fields["budget_text"], MAX_BUDGET)
    if "exchange_date" in fields:
        fields.update(org_nudge_sent=False, pre_exchange_sent=False)
    async with db.transaction() as tx:
        await _load_organized(tx, game_id, organizer_id, GameStatus.COLLECTING, GameStatus.DRAWN)
        await repo.update_game(tx, game_id, **fields)
        return await load_game(tx, game_id)


async def cancel_game(db: Db, game_id: int, organizer_id: int, now: datetime) -> list[Participant]:
    """Cancel the game; returns everyone to notify (active and waiting, except the organizer)."""
    async with db.transaction() as tx:
        await _load_organized(tx, game_id, organizer_id, GameStatus.COLLECTING, GameStatus.DRAWN)
        return [p for p in await _cancel(tx, game_id, now) if p.user_id != organizer_id]


async def cancel_game_as_admin(db: Db, game_id: int, now: datetime) -> list[Participant]:
    """An admin cancels any collecting or drawn game (/game); returns everyone active or waiting."""
    async with db.transaction() as tx:
        require_status(await load_game(tx, game_id), GameStatus.COLLECTING, GameStatus.DRAWN)
        return await _cancel(tx, game_id, now)


async def _cancel(db: Db, game_id: int, now: datetime) -> list[Participant]:
    await repo.update_game(db, game_id, status=GameStatus.CANCELLED, cancelled_at=now)
    return await repo.participants(db, game_id, ACTIVE, WAITING)


async def add_exclusion(db: Db, game_id: int, organizer_id: int, a: int, b: int) -> bool:
    """Save an excluded pair (both active participants). False when it already existed."""
    if a == b:
        raise ExclusionRejected(ExclusionProblem.SAME_PERSON)
    async with db.transaction() as tx:
        await _load_organized(tx, game_id, organizer_id, GameStatus.COLLECTING)
        for user_id in (a, b):
            participant = await repo.get_participant(tx, game_id, user_id)
            if participant is None or participant.status != ACTIVE:
                raise ExclusionRejected(ExclusionProblem.NOT_PARTICIPANT)
        existing = await repo.exclusions(tx, game_id)
        if (min(a, b), max(a, b)) in existing:
            return False
        if len(existing) >= MAX_EXCLUSIONS:
            raise ExclusionRejected(ExclusionProblem.LIMIT)
        return await repo.add_exclusion(tx, game_id, a, b)


async def remove_exclusion(db: Db, game_id: int, organizer_id: int, a: int, b: int) -> bool:
    async with db.transaction() as tx:
        await _load_organized(tx, game_id, organizer_id, GameStatus.COLLECTING)
        return await repo.delete_exclusion(tx, game_id, a, b)


async def start_wish_reminder(db: Db, game_id: int, organizer_id: int, now: datetime) -> list[Participant]:
    """Active participants without wishes to remind; at most once per 24 h per game (§5.4)."""
    async with db.transaction() as tx:
        game = await _load_organized(tx, game_id, organizer_id, GameStatus.COLLECTING, GameStatus.DRAWN)
        last = game.last_wish_reminder_at
        if last is not None and from_iso(last) > now - WISH_REMINDER_INTERVAL:
            raise TooSoon(last)
        recipients = [p for p in await repo.participants(tx, game_id, ACTIVE) if not p.wishes]
        if recipients:
            await repo.update_game(tx, game_id, last_wish_reminder_at=now)
        return recipients


# --- draw ----------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class DrawPreview:
    active: int
    without_wishes: int
    waiting: int


async def draw_preview(db: Db, game_id: int, organizer_id: int) -> DrawPreview:
    """Numbers for the confirmation text; raises when the draw is not allowed yet."""
    await _load_organized(db, game_id, organizer_id, GameStatus.COLLECTING)
    counts = await game_counts(db, game_id)
    if counts.active < MIN_PARTICIPANTS:
        raise NotEnoughParticipants(counts.active)
    return DrawPreview(counts.active, counts.without_wishes, counts.waiting)


async def run_draw(
    db: Db,
    game_id: int,
    organizer_id: int,
    *,
    now: datetime,
    rng: random.Random,
    redraw_of: int | None = None,
    time_budget: float = SEARCH_BUDGET_SECONDS,
) -> DrawResult:
    """Draw (or redraw, at most twice) and store the pairs with status=drawn in one transaction.

    ``redraw_of`` makes it a redraw: the game's ``redraw_count`` the organizer saw when
    confirming. A second tap on the same confirmation finds a higher count and is refused,
    so one confirmation reshuffles the pairs exactly once.

    The search runs in a worker thread so a slow fallback never blocks the event loop.
    Sending the results is the caller's job (outbox, purpose ``draw_result``).
    """
    redraw = redraw_of is not None
    expected = GameStatus.DRAWN if redraw else GameStatus.COLLECTING
    game = await _load_organized(db, game_id, organizer_id, expected)
    if redraw:
        _check_redraw(game, redraw_of)
    active = await repo.participants(db, game_id, ACTIVE)
    if len(active) < MIN_PARTICIPANTS:
        raise NotEnoughParticipants(len(active))
    excluded = {frozenset(pair) for pair in await repo.exclusions(db, game_id)}
    ids = [p.user_id for p in active]
    pairs = await asyncio.to_thread(draw_cycle, ids, excluded, rng, time_budget=time_budget)
    if pairs is None:
        raise DrawImpossible(game_id)
    async with db.transaction() as tx:
        fresh = await load_game(tx, game_id)
        current = {p.user_id for p in await repo.participants(tx, game_id, ACTIVE)}
        if fresh.status != expected or current != set(ids):
            raise WrongStatus(fresh.status)
        if redraw:
            _check_redraw(fresh, redraw_of)
        await repo.replace_assignments(tx, game_id, pairs)
        await repo.update_game(
            tx, game_id, status=GameStatus.DRAWN, drawn_at=now, redraw_count=fresh.redraw_count + int(redraw)
        )
        await tx.execute(
            "UPDATE participants SET result_dm_ok = NULL, gift_ready = 0 WHERE game_id = ? AND status = 'active'",
            (game_id,),
        )
        await record(tx, Event.DRAW_DONE, now, user_id=organizer_id, game_id=game_id, n=len(ids), redraw=redraw)
        drawn = await load_game(tx, game_id)
        by_user = {p.user_id: p for p in await repo.participants(tx, game_id, ACTIVE)}
    return DrawResult(drawn, pairs, by_user)


def _check_redraw(game: Game, confirmed_count: int | None) -> None:
    if game.redraw_count != confirmed_count:
        raise RedrawAlreadyDone(game.redraw_count)
    if game.redraw_count >= MAX_REDRAWS:
        raise RedrawLimitReached(game.redraw_count)


async def my_receiver(db: Db, game_id: int, user_id: int) -> Participant | None:
    """The receiver of an active participant in a drawn game ([Кому я дарю?])."""
    await require_participant(db, game_id, user_id)
    receiver_id = await repo.receiver_of(db, game_id, user_id)
    return None if receiver_id is None else await repo.get_participant(db, game_id, receiver_id)


async def mark_gift_ready(db: Db, game_id: int, user_id: int) -> None:
    """[Подарок готов]: the organizer's panel counts ready gifts."""
    async with db.transaction() as tx:
        require_status(await load_game(tx, game_id), GameStatus.DRAWN)
        await require_participant(tx, game_id, user_id)
        await repo.set_gift_ready(tx, game_id, user_id)


# --- reveal (§5.11) ----------------------------------------------------------------------------------


def can_reveal(game: Game, today: date) -> bool:
    """Once per game, after the draw, from the exchange date on (any time when there is no date)."""
    return (
        game.status in (GameStatus.DRAWN, GameStatus.FINISHED)
        and not game.reveal_done
        and (game.exchange_date is None or today >= game.exchange_date)
    )


async def reveal(db: Db, game_id: int, organizer_id: int, *, today: date, now: datetime) -> Reveal:
    """Mark the game revealed and return the chain to announce."""
    async with db.transaction() as tx:
        game = await _load_organized(tx, game_id, organizer_id, GameStatus.DRAWN, GameStatus.FINISHED)
        if game.reveal_done:
            raise AlreadyRevealed(game_id)
        if not can_reveal(game, today):
            raise RevealTooEarly(game.exchange_date)
        pairs = await repo.assignments(tx, game_id)
        everyone = await repo.participants(tx, game_id)
        await repo.update_game(tx, game_id, reveal_done=True)
        await record(tx, Event.REVEAL, now, user_id=organizer_id, game_id=game_id, n=len(pairs))
        return Reveal(game, _chain(pairs, everyone), [p for p in everyone if p.status == ACTIVE])


def _chain(pairs: dict[int, int], people: list[Participant]) -> list[tuple[str, str]]:
    """'Ольга → Иван, Иван → Мария, …': follow the cycle from the first participant who joined.

    Every participant is tried as a start, so nobody is lost if the pairs form several cycles.
    """
    names = {p.user_id: p.display_name for p in people}
    chain: list[tuple[str, str]] = []
    visited: set[int] = set()
    for start in (p.user_id for p in people):
        giver = start
        while giver in pairs and giver not in visited:
            visited.add(giver)
            chain.append((names.get(giver, "?"), names.get(pairs[giver], "?")))
            giver = pairs[giver]
    return chain


async def record_result_delivery(
    db: Db, game_id: int, user_id: int, delivered: bool, now: datetime
) -> DeliverySummary | None:
    """Store whether a draw result reached the participant.

    Returns the summary exactly once: when the last pending result resolves.
    """
    async with db.transaction() as tx:
        changed = await tx.execute(
            "UPDATE participants SET result_dm_ok = ? WHERE game_id = ? AND user_id = ?"
            " AND status = 'active' AND result_dm_ok IS NULL",
            (delivered, game_id, user_id),
        )
        if changed.rowcount == 0:
            return None
        if not delivered:
            await record(tx, Event.RESULT_DM_FAILED, now, user_id=user_id, game_id=game_id)
        active = await repo.participants(tx, game_id, ACTIVE)
        if any(p.result_dm_ok is None for p in active):
            return None
        return DeliverySummary(
            game=await load_game(tx, game_id),
            sent=sum(1 for p in active if p.result_dm_ok),
            total=len(active),
            failed_names=[p.display_name for p in active if not p.result_dm_ok],
        )


async def undelivered_results(db: Db, game_id: int, organizer_id: int) -> list[Participant]:
    """[Кто не получил пару]: active participants whose result message failed."""
    await _load_organized(db, game_id, organizer_id, GameStatus.DRAWN, GameStatus.FINISHED)
    return [p for p in await repo.participants(db, game_id, ACTIVE) if p.result_dm_ok is False]
