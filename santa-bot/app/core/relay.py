"""Anonymous messages between a Santa and their receiver (§5.7) and complaints.

The Santa's identity never leaves this module except as a user id used for
delivery: texts shown to a receiver must never include the sender's name.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from app import repo
from app.core.analytics import Event, record
from app.core.games import GameError, PermissionDenied, UserBlocked, load_game, require_participant, require_status
from app.core.inputs import MAX_RELAY
from app.core.models import Game, GameStatus, Participant, Relay, RelayDirection, Report
from app.db import Db

RELAYS_PER_DAY = 20
RELAY_WINDOW = timedelta(days=1)


class AnonChatOff(GameError):
    pass


class RelayLimitReached(GameError):
    pass


class AlreadyReported(GameError):
    """The same person already complained about this message."""


@dataclass(frozen=True, slots=True)
class Route:
    """Who a relay goes to. ``sender``/``recipient`` carry display names for the texts."""

    game: Game
    sender: Participant
    recipient: Participant
    direction: RelayDirection


async def route_new(db: Db, game_id: int, user_id: int, direction: RelayDirection) -> Route:
    """[Спросить получателя анонимно] (to_receiver) or [Написать своему Санте] (to_santa)."""
    game = await _open_game(db, game_id, user_id)
    sender = await require_participant(db, game_id, user_id)
    if direction == RelayDirection.TO_RECEIVER:
        counterpart = await repo.receiver_of(db, game_id, user_id)
    else:
        counterpart = await repo.giver_of(db, game_id, user_id)
    if counterpart is None:
        raise PermissionDenied(f"user {user_id} has no counterpart in game {game_id}")
    return Route(game, sender, await require_participant(db, game_id, counterpart), direction)


async def route_reply(db: Db, relay_id: int, user_id: int) -> Route:
    """A reply always goes back to whoever sent ``relay_id``; only its recipient may reply."""
    relay = await repo.get_relay(db, relay_id)
    if relay is None or relay.to_id != user_id:
        raise PermissionDenied(f"user {user_id} cannot reply to relay {relay_id}")
    game = await _open_game(db, relay.game_id, user_id)
    direction = (
        RelayDirection.TO_SANTA if relay.direction == RelayDirection.TO_RECEIVER else RelayDirection.TO_RECEIVER
    )
    sender = await require_participant(db, relay.game_id, user_id)
    recipient = await require_participant(db, relay.game_id, relay.from_id)
    return Route(game, sender, recipient, direction)


async def send_relay(db: Db, route: Route, text: str, now: datetime) -> Relay:
    """Store the relay (at most 20 per user per game per 24 h). The caller delivers it."""
    if not text or len(text) > MAX_RELAY:
        raise ValueError(f"relay text must be 1-{MAX_RELAY} characters")
    async with db.transaction() as tx:
        await _open_game(tx, route.game.id, route.sender.user_id)
        sent = await repo.count_relays_since(tx, route.game.id, route.sender.user_id, now - RELAY_WINDOW)
        if sent >= RELAYS_PER_DAY:
            raise RelayLimitReached(sent)
        relay = await repo.insert_relay(
            tx, route.game.id, route.sender.user_id, route.recipient.user_id, route.direction, text, now
        )
        await record(
            tx, Event.RELAY_SENT, now, user_id=route.sender.user_id, game_id=route.game.id,
            direction=route.direction,
        )
        return relay


async def report_relay(db: Db, relay_id: int, reporter_id: int, now: datetime) -> Report:
    """[Пожаловаться]: only the recipient of a relay can report it, once."""
    async with db.transaction() as tx:
        relay = await repo.get_relay(tx, relay_id)
        if relay is None or relay.to_id != reporter_id:
            raise PermissionDenied(f"user {reporter_id} cannot report relay {relay_id}")
        if await repo.report_exists(tx, relay_id, reporter_id):
            raise AlreadyReported(relay_id)
        report = await repo.insert_report(
            tx, relay.id, relay.game_id, reporter_id, relay.from_id, relay.text, now
        )
        await record(tx, Event.REPORT, now, user_id=reporter_id, game_id=relay.game_id)
        return report


async def _open_game(db: Db, game_id: int, user_id: int) -> Game:
    """A drawn game with anonymous chat on, and a sender who is not blocked."""
    user = await repo.get_user(db, user_id)
    if user is None or user.blocked:
        raise UserBlocked(user_id)
    game = await load_game(db, game_id)
    require_status(game, GameStatus.DRAWN)
    if not game.anon_chat:
        raise AnonChatOff(game_id)
    return game
