"""Private-chat updates: bot_started, typed messages and commands (§5.1–§5.8).

Order for a typed message: commands first (/whoami works for anyone), then the
consent gate, then an explicit 'код XXXXXX', then the pending input
(``user_state``), then a bare code of an existing game, else a hint.

Extension points for other stages (register at import time of their module):
- ``@command("/stats", admin_only=True)`` adds a command; admin-only commands are
  invisible to everyone else and work before consent;
- ``@state_handler(StateKind.ADMIN_INPUT)`` handles typed text for a pending input.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from app import repo
from app.context import AppContext
from app.core import texts
from app.core.analytics import Event, record
from app.core.games import GameError
from app.core.models import StateKind, UserState
from app.core.payloads import (
    DIRECT_SOURCE,
    JoinPayload,
    extract_code,
    first_source,
    join_payload,
    parse_start_payload,
)
from app.handlers import flows, views
from app.handlers.session import Outdated, Session, open_session
from app.max_api import BotStarted, MessageCreated

CommandHandler = Callable[[Session, str], Awaitable[None]]
StateHandler = Callable[[Session, UserState, MessageCreated], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class Command:
    handler: CommandHandler
    admin_only: bool
    before_consent: bool


_COMMANDS: dict[str, Command] = {}
_STATE_HANDLERS: dict[StateKind, StateHandler] = {}


def command(
    name: str, *, admin_only: bool = False, before_consent: bool = False
) -> Callable[[CommandHandler], CommandHandler]:
    """Register ``/name``; the handler receives the session and the rest of the line."""

    def register(handler: CommandHandler) -> CommandHandler:
        _COMMANDS[name] = Command(handler, admin_only, before_consent or admin_only)
        return handler

    return register


def state_handler(*kinds: StateKind) -> Callable[[StateHandler], StateHandler]:
    def register(handler: StateHandler) -> StateHandler:
        for kind in kinds:
            _STATE_HANDLERS[kind] = handler
        return handler

    return register


# --- entry points -----------------------------------------------------------------------------------


async def on_start(ctx: AppContext, update: BotStarted) -> None:
    """The user pressed «Начать» or opened a deep link (possibly again, §2: unverified)."""
    payload = parse_start_payload(update.payload)
    s = await open_session(ctx, update.user, first_source=first_source(payload))
    if isinstance(payload, JoinPayload):
        game = await repo.get_game_by_code(ctx.db, payload.code)
        await record(ctx.db, Event.INVITE_OPEN, s.now(), user_id=s.user_id, game_id=game.id if game else None)
    await start(s, update.payload)


async def start(s: Session, raw_payload: str | None) -> None:
    """/start or bot_started: a fresh start that forgets any pending input."""
    payload = parse_start_payload(raw_payload)
    if not s.user.has_consent:
        await gate(s, raw_payload if payload is not None else None)
        return
    await repo.clear_state(s.db, s.user_id)
    await _guarded(s, flows.route_payload(s, payload))


async def gate(s: Session, raw_payload: str | None = None) -> None:
    """The consent screen; a start payload is kept and resumed after consent (§5.1)."""
    if raw_payload:
        await repo.set_state(s.db, s.user_id, StateKind.RESUME, s.now(), data={"payload": raw_payload})
    await s.say(views.consent(s.ctx.config))


async def on_message(ctx: AppContext, message: MessageCreated) -> None:
    s = await open_session(ctx, message.sender, first_source=DIRECT_SOURCE)
    text = message.text.strip()
    if text.startswith("/"):
        await _command(s, text)
        return
    if not s.user.has_consent:
        code = extract_code(text)
        if code is not None and await repo.get_game_by_code(s.db, code) is None:
            code = None
        await gate(s, None if code is None else join_payload(code))
        return
    await _guarded(s, _text(s, message, text))


async def _command(s: Session, text: str) -> None:
    word, _, rest = text.partition(" ")
    name = word.split("@", 1)[0].lower()
    entry = _COMMANDS.get(name)
    if entry is not None and entry.admin_only and not s.is_admin:
        entry = None
    if not s.user.has_consent and (entry is None or not entry.before_consent):
        await gate(s)
    elif entry is None:
        await s.say(views.menu(texts.UNKNOWN_INPUT))
    else:
        await _guarded(s, entry.handler(s, rest.strip()))


async def _text(s: Session, message: MessageCreated, text: str) -> None:
    code = extract_code(text)
    if code is not None and text.lower().startswith("код"):
        if not await flows.join_by_text(s, code):
            await s.say(texts.GAME_NOT_FOUND)
        return
    state = await repo.get_state(s.db, s.user_id, s.now())
    handler = _STATE_HANDLERS.get(state.kind) if state is not None else None
    if state is not None and handler is not None:
        try:
            await handler(s, state, message)
        except (GameError, Outdated):
            await repo.clear_state(s.db, s.user_id)
            raise
        return
    if code is not None and await flows.join_by_text(s, code):
        return
    await s.say(views.menu(texts.TEXT_ONLY if message.has_attachments and not text else texts.UNKNOWN_INPUT))


async def _guarded(s: Session, action: Awaitable[None]) -> None:
    """Run a flow, turning expected refusals into messages."""
    try:
        await action
    except GameError as error:
        await s.refuse(error)
    except Outdated:
        await s.say(texts.BUTTON_OUTDATED)


# --- commands ----------------------------------------------------------------------------------------


@command("/start", before_consent=True)
async def _start_command(s: Session, rest: str) -> None:
    await start(s, rest or None)


@command("/help")
async def _help(s: Session, rest: str) -> None:
    await s.say(views.help_message(await s.ctx.settings(), s.ctx.config))


@command("/whoami", before_consent=True)
async def _whoami(s: Session, rest: str) -> None:
    await s.say(texts.whoami(s.user_id))


@command("/cancel")
async def _cancel(s: Session, rest: str) -> None:
    cleared = await repo.clear_state(s.db, s.user_id)
    await s.say(views.menu(texts.INPUT_CANCELLED if cleared else texts.NOTHING_TO_CANCEL))


# --- pending input ----------------------------------------------------------------------------------------


def _typed(message: MessageCreated) -> str | None:
    """The text of a message, or None when it is only a picture or a file."""
    return message.text if message.text.strip() else None


@state_handler(StateKind.TITLE, StateKind.BUDGET_CUSTOM, StateKind.DATE_CUSTOM)
async def _wizard_or_settings(s: Session, state: UserState, message: MessageCreated) -> None:
    text = _typed(message)
    if text is None:
        await s.say(views.prompt(texts.TEXT_ONLY))
    elif state.game_id is None:
        await flows.wizard_input(s, state, text)
    else:
        await flows.settings_input(s, state, text)


@state_handler(StateKind.WISHES)
async def _wishes(s: Session, state: UserState, message: MessageCreated) -> None:
    assert state.game_id is not None
    text = _typed(message)
    if text is None:
        await s.say(views.prompt(texts.TEXT_ONLY, views.surprise_button(state.game_id)))
    else:
        await flows.save_wishes(s, state.game_id, text)


@state_handler(StateKind.DISPLAY_NAME)
async def _display_name(s: Session, state: UserState, message: MessageCreated) -> None:
    assert state.game_id is not None
    text = _typed(message)
    if text is None:
        await s.say(views.prompt(texts.TEXT_ONLY))
    else:
        await flows.save_name(s, state.game_id, text)


@state_handler(StateKind.CODE)
async def _code(s: Session, state: UserState, message: MessageCreated) -> None:
    code = extract_code(message.text)
    if code is None or not await flows.join_by_text(s, code):
        await s.say(views.prompt(texts.GAME_NOT_FOUND))


@state_handler(StateKind.RELAY_TO_RECEIVER, StateKind.RELAY_TO_SANTA, StateKind.REPLY_RELAY)
async def _relay(s: Session, state: UserState, message: MessageCreated) -> None:
    await flows.relay_input(s, state, message.text, message.has_attachments)
