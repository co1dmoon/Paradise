"""Conversation flows shared by typed messages, start payloads and buttons (§5.1–§5.8, §6).

Handlers in ``private`` and ``callbacks`` stay thin: they parse input and call
these flows, which call ``app.core`` for every rule and permission check.
Expected refusals propagate as ``GameError`` and are turned into text by the
entry points (see ``session.explain``).
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import date
from enum import StrEnum
from typing import Any

from app import repo
from app.context import AppContext
from app.core import games, relay, texts
from app.core.analytics import Event, record
from app.core.dates import DateError, parse_user_date
from app.core.games import ACTIVE, WAITING, GameDraft, JoinOutcome
from app.core.inputs import MAX_BUDGET, MAX_NAME, MAX_RELAY, MAX_TITLE, MAX_WISHES, clean_text
from app.core.models import Game, GameStatus, JoinVia, Participant, PaymentStatus, RelayDirection, StateKind, UserState
from app.core.payloads import (
    GroupPayload,
    JoinPayload,
    NewGamePayload,
    OrganizerPayload,
    PaymentReturnPayload,
    StartPayload,
)
from app.core.pricing import Upgrade, next_upgrade
from app.core.users import display_name_from_profile
from app.handlers import group, views
from app.handlers.session import Outdated, Refusal, Session
from app.max_api import MaxApiError, Target

# --- menu and payloads ---------------------------------------------------------------------------


async def show_menu(s: Session, text: str = texts.MENU) -> None:
    await s.show(views.menu(text))


async def route_payload(s: Session, payload: StartPayload | None) -> None:
    """What a start payload leads to once the user has consented (§6.7)."""
    match payload:
        case JoinPayload(code):
            await join(s, code, JoinVia.LINK)
        case NewGamePayload(code):
            await start_referred(s, code)
        case OrganizerPayload(code):
            game = await repo.get_game_by_code(s.db, code)
            if game is None:
                await show_menu(s)
            else:
                await open_game(s, game.id)
        case PaymentReturnPayload(inv_id):
            await payment_return(s, inv_id)
        case GroupPayload(chat_id):
            await start_group_game(s, chat_id)
        case _:
            await show_menu(s)


# --- §5.2 creation wizard ---------------------------------------------------------------------------


class WizardStep(StrEnum):
    TITLE = "title"
    BUDGET = "budget"
    DATE = "date"
    PARTICIPATES = "participates"


# The step decides what typed text means; the stored state kind names the same thing.
_STEP_KINDS = {
    WizardStep.TITLE: StateKind.TITLE,
    WizardStep.BUDGET: StateKind.BUDGET_CUSTOM,
    WizardStep.DATE: StateKind.DATE_CUSTOM,
    WizardStep.PARTICIPATES: StateKind.DATE_CUSTOM,
}


@dataclass(frozen=True, slots=True)
class WizardDraft:
    """Answers so far, kept in user_state.data while the organizer goes through the steps."""

    step: WizardStep
    title: str = texts.DEFAULT_TITLE
    budget: str = ""
    exchange_date: date | None = None
    source_game_id: int | None = None
    group_chat_id: int | None = None

    def to_data(self) -> dict[str, Any]:
        return {
            "wizard": self.step.value,
            "title": self.title,
            "budget": self.budget,
            "date": self.exchange_date.isoformat() if self.exchange_date else None,
            "source_game_id": self.source_game_id,
            "group_chat_id": self.group_chat_id,
        }

    @classmethod
    def from_state(cls, state: UserState | None) -> WizardDraft:
        """The draft of a pending wizard; ``Outdated`` if the wizard is not in progress."""
        if state is None or state.game_id is not None or "wizard" not in state.data:
            raise Outdated("no creation wizard in progress")
        data = state.data
        return cls(
            step=WizardStep(data["wizard"]),
            title=data["title"],
            budget=data["budget"],
            exchange_date=date.fromisoformat(data["date"]) if data["date"] else None,
            source_game_id=data["source_game_id"],
            group_chat_id=data.get("group_chat_id"),
        )


async def load_wizard(s: Session, *steps: WizardStep) -> WizardDraft:
    draft = WizardDraft.from_state(await repo.get_state(s.db, s.user_id, s.now()))
    if steps and draft.step not in steps:
        raise Outdated(f"wizard is at {draft.step}")
    return draft


async def start_wizard(s: Session, source_game_id: int | None = None, group_chat_id: int | None = None) -> None:
    if await _maintenance(s):
        return
    games.check_can_create(s.user, s.today())
    await _save_wizard(s, WizardDraft(WizardStep.TITLE, source_game_id=source_game_id, group_chat_id=group_chat_id))
    await s.say(views.ask_title())


async def start_group_game(s: Session, chat_id: int) -> None:
    """[Создать игру в этом чате] (§5.10): the wizard for a game with a card in that chat."""
    try:
        await s.ctx.api.get_chat(chat_id)
    except MaxApiError:
        await s.say(texts.GROUP_NOT_FOUND)
        return
    await start_wizard(s, group_chat_id=chat_id)


async def start_referred(s: Session, code: str) -> None:
    """[Устроить игру в другом чате] or an n_ link: the new game remembers where it came from (§6.3)."""
    game = await repo.get_game_by_code(s.db, code)
    await record(s.db, Event.REF_CLICK, s.now(), user_id=s.user_id, game_id=game.id if game else None)
    await start_wizard(s, game.id if game else None)


async def _save_wizard(s: Session, draft: WizardDraft) -> None:
    await repo.set_state(s.db, s.user_id, _STEP_KINDS[draft.step], s.now(), data=draft.to_data())


async def wizard_set_title(s: Session, draft: WizardDraft, title: str) -> None:
    await _save_wizard(s, replace(draft, step=WizardStep.BUDGET, title=title))
    await s.say(views.ask_budget(
        lambda index: views.button(texts.BUDGET_PRESETS[index], views.Action.WIZARD_BUDGET, index),
        views.button(texts.BTN_CUSTOM_BUDGET, views.Action.WIZARD_BUDGET_CUSTOM),
    ))


async def wizard_set_budget(s: Session, draft: WizardDraft, budget: str) -> None:
    await _save_wizard(s, replace(draft, step=WizardStep.DATE, budget=budget))
    await s.say(views.ask_date(
        s.today(), views.wizard_date_button,
        views.button(texts.BTN_CUSTOM_DATE, views.Action.WIZARD_DATE_CUSTOM),
        views.button(texts.BTN_DATE_UNKNOWN, views.Action.WIZARD_DATE_UNKNOWN),
    ))


async def wizard_set_date(s: Session, draft: WizardDraft, exchange_date: date | None) -> None:
    await _save_wizard(s, replace(draft, step=WizardStep.PARTICIPATES, exchange_date=exchange_date))
    await s.say(views.ask_participates())


async def wizard_input(s: Session, state: UserState, text: str) -> None:
    """Typed text during the wizard: a title, a custom budget or a custom date."""
    draft = WizardDraft.from_state(state)
    match draft.step:
        case WizardStep.TITLE:
            title = await _limited(s, text, MAX_TITLE, texts.title_too_long(MAX_TITLE), texts.ASK_TITLE)
            if title is not None:
                await wizard_set_title(s, draft, title)
        case WizardStep.BUDGET:
            budget = await _limited(s, text, MAX_BUDGET, texts.budget_too_long(MAX_BUDGET), texts.ASK_CUSTOM_BUDGET)
            if budget is not None:
                await wizard_set_budget(s, draft, budget)
        case WizardStep.DATE:
            parsed = parse_user_date(text, s.today())
            if isinstance(parsed, DateError):
                await s.say(views.prompt(texts.DATE_ERRORS[parsed]))
            else:
                await wizard_set_date(s, draft, parsed)
        case WizardStep.PARTICIPATES:
            await s.say(views.ask_participates())


async def wizard_finish(s: Session, participates: bool) -> None:
    """Step 4 answered: create the game, send the forwardable invite, then the next steps."""
    draft = await load_wizard(s, WizardStep.PARTICIPATES)
    if await _maintenance(s):
        return
    source = draft.source_game_id
    if source is not None and await repo.get_game(s.db, source) is None:
        source = None
    game = await games.create_game(
        s.db, s.user_id,
        GameDraft(draft.title, draft.budget, draft.exchange_date, participates, source_game_id=source,
                  group_chat_id=draft.group_chat_id),
        await s.ctx.settings(), now=s.now(), today=s.today(), rng=s.ctx.rng,
    )
    await repo.clear_state(s.db, s.user_id)
    if game.group_chat_id is None:
        await s.say(views.invite(s.ctx.config, game))
    else:
        await group.card_changed(s.ctx, game.id)
    await s.say(views.game_created(game))
    if participates:
        await repo.set_state(s.db, s.user_id, StateKind.WISHES, s.now(), game_id=game.id)


def valid_suggested_date(value: date, today: date) -> bool:
    """A date from a button: still in the allowed range (the button may be from yesterday)."""
    return not isinstance(parse_user_date(f"{value:%d.%m.%Y}", today), DateError)


async def _limited(s: Session, text: str, limit: int, too_long: str, empty: str) -> str | None:
    """Cleaned one-line text within ``limit``; otherwise re-prompt and return None."""
    cleaned = clean_text(text)
    if not cleaned or len(cleaned) > limit:
        await s.say(views.prompt(too_long if cleaned else empty))
        return None
    return cleaned


async def _maintenance(s: Session) -> bool:
    if (await s.ctx.settings()).maintenance:
        await s.say(texts.MAINTENANCE)
        return True
    return False


# --- settings edits by typed text (§5.4 Настройки) ------------------------------------------------------


async def settings_input(s: Session, state: UserState, text: str) -> None:
    """A new title, budget or date for an existing game (state.game_id is set)."""
    assert state.game_id is not None
    if state.kind == StateKind.TITLE:
        value = await _limited(s, text, MAX_TITLE, texts.title_too_long(MAX_TITLE), texts.ASK_TITLE)
        if value is not None:
            await save_setting(s, state.game_id, title=value)
    elif state.kind == StateKind.BUDGET_CUSTOM:
        value = await _limited(s, text, MAX_BUDGET, texts.budget_too_long(MAX_BUDGET), texts.ASK_CUSTOM_BUDGET)
        if value is not None:
            await save_setting(s, state.game_id, budget_text=value)
    else:
        parsed = parse_user_date(text, s.today())
        if isinstance(parsed, DateError):
            await s.say(views.prompt(texts.DATE_ERRORS[parsed]))
        else:
            await save_setting(s, state.game_id, exchange_date=parsed)


async def save_setting(s: Session, game_id: int, **fields: Any) -> None:
    game = await games.update_game_settings(s.db, game_id, s.user_id, **fields)
    await repo.clear_state(s.db, s.user_id)
    await s.show(views.settings_screen(game, notice=texts.SETTINGS_SAVED))
    await group.card_changed(s.ctx, game_id)


# --- §5.3 joining -----------------------------------------------------------------------------------


async def join(s: Session, code: str, via: JoinVia) -> None:
    game = await repo.get_game_by_code(s.db, code)
    if game is None:
        await s.say(texts.GAME_NOT_FOUND)
        return
    if game.organizer_id == s.user_id:
        await show_panel(s, game.id)
        return
    existing = await repo.get_participant(s.db, game.id, s.user_id)
    if existing is not None and existing.status in (ACTIVE, WAITING):
        await show_participant_view(s, game, existing)
        return
    if game.status == GameStatus.COLLECTING and await _maintenance(s):
        return
    async with s.ctx.locks.game(game.id):
        result = await games.join_game(s.db, game.id, s.user_id, via, s.now())
    match result.outcome:
        case JoinOutcome.JOINED:
            assert result.participant is not None
            await s.say(views.joined(game, await organizer_name(s.ctx, game), result.participant.display_name))
            await ask_for_wishes(s, game.id)
            await group.card_changed(s.ctx, game.id)
        case JoinOutcome.WAITING:
            await s.say(views.waiting(game, await s.ctx.settings(), await offer_upgrade(s.ctx, game)))
        case JoinOutcome.ALREADY_IN:
            assert result.participant is not None
            await show_participant_view(s, game, result.participant)
        case JoinOutcome.DRAWN:
            await s.say(views.already_drawn(game))
        case JoinOutcome.CANCELLED:
            await s.say(texts.GAME_CANCELLED)
        case JoinOutcome.REMOVED:
            await s.say(texts.REMOVED_CANNOT_JOIN)


async def join_by_text(s: Session, code: str) -> bool:
    """A typed code joins only when such a game exists; False lets the caller treat it as other text."""
    if await repo.get_game_by_code(s.db, code) is None:
        return False
    await repo.clear_state(s.db, s.user_id)
    await join(s, code, JoinVia.CODE)
    return True


async def organizer_name(ctx: AppContext, game: Game) -> str:
    participant = await repo.get_participant(ctx.db, game.id, game.organizer_id)
    if participant is not None:
        return participant.display_name
    user = await repo.get_user(ctx.db, game.organizer_id)
    return display_name_from_profile(user.max_name if user else None)


async def offer_upgrade(ctx: AppContext, game: Game) -> Upgrade | None:
    """The next paid tier for a collecting game, or None (top tier, or payments are off)."""
    if not ctx.config.payments_enabled or game.status != GameStatus.COLLECTING:
        return None
    return next_upgrade(await ctx.prices(), game.participant_limit, await repo.paid_sum(ctx.db, game.id))


# --- names and wishes ---------------------------------------------------------------------------------


async def require_me(s: Session, game_id: int) -> Participant:
    """The user's active or waiting row; a friendly refusal for anyone who left or was removed."""
    me = await repo.get_participant(s.db, game_id, s.user_id)
    if me is None or me.status not in (ACTIVE, WAITING):
        raise Refusal(texts.NOT_IN_GAME)
    return me


async def ask_for_wishes(s: Session, game_id: int) -> None:
    me = await require_me(s, game_id)
    game = await games.load_game(s.db, game_id)
    games.require_status(game, GameStatus.COLLECTING, GameStatus.DRAWN)
    await repo.set_state(s.db, s.user_id, StateKind.WISHES, s.now(), game_id=game_id)
    await s.say(views.ask_wishes(game_id, me.wishes))


async def save_wishes(s: Session, game_id: int, text: str) -> None:
    cleaned = clean_text(text, multiline=True)
    if not cleaned or len(cleaned) > MAX_WISHES:
        await s.say(views.prompt(texts.wishes_too_long(MAX_WISHES) if cleaned else texts.ASK_WISHES,
                                 views.surprise_button(game_id)))
        return
    before = await require_me(s, game_id)
    await games.set_wishes(s.db, game_id, s.user_id, cleaned, s.now())
    await repo.clear_state(s.db, s.user_id)
    game = await games.load_game(s.db, game_id)
    if before.wishes or game.status != GameStatus.COLLECTING:
        await s.say(texts.WISHES_UPDATED)
    else:
        await s.say(views.wishes_saved(game))


async def ask_for_name(s: Session, game_id: int) -> None:
    await require_me(s, game_id)
    await repo.set_state(s.db, s.user_id, StateKind.DISPLAY_NAME, s.now(), game_id=game_id)
    await s.say(views.prompt(texts.ASK_NAME))


async def save_name(s: Session, game_id: int, text: str) -> None:
    name = await _limited(s, text, MAX_NAME, texts.name_too_long(MAX_NAME), texts.ASK_NAME)
    if name is None:
        return
    await games.set_display_name(s.db, game_id, s.user_id, name)
    await repo.clear_state(s.db, s.user_id)
    await s.say(texts.name_saved(name))
    me = await require_me(s, game_id)
    game = await games.load_game(s.db, game_id)
    if not me.wishes and game.status == GameStatus.COLLECTING:
        await ask_for_wishes(s, game_id)


# --- games: views and the organizer panel -----------------------------------------------------------------


async def open_game(s: Session, game_id: int) -> None:
    """[Мои игры] → a game: the organizer gets the panel, a participant their view."""
    game = await games.load_game(s.db, game_id)
    if game.organizer_id == s.user_id:
        await show_panel(s, game_id)
        return
    await show_participant_view(s, game, await require_me(s, game_id))


async def show_participant_view(s: Session, game: Game, me: Participant) -> None:
    upgrade = await offer_upgrade(s.ctx, game) if me.status == WAITING else None
    await s.show(views.participant_view(game, me, await organizer_name(s.ctx, game), upgrade))


async def show_panel(s: Session, game_id: int) -> None:
    game = await games.load_game(s.db, game_id)
    games.require_organizer(game, s.user_id)
    counts = await games.game_counts(s.db, game_id)
    await s.show(views.panel(game, counts, await offer_upgrade(s.ctx, game),
                             can_reveal=games.can_reveal(game, s.today())))


async def payment_return(s: Session, inv_id: int) -> None:
    """p_INVID: back from the Robokassa page. The ResultURL, not this, changes state (§7)."""
    await s.say(texts.PAY_CHECKING)
    payment = await repo.get_payment(s.db, inv_id)
    game = None if payment is None or payment.game_id is None else await repo.get_game(s.db, payment.game_id)
    if payment is None or game is None or s.user_id not in (payment.payer_id, game.organizer_id):
        await show_menu(s)
        return
    if payment.status == PaymentStatus.CREATED:
        await s.say(texts.PAYMENT_PENDING)
    await open_game(s, game.id)
    me = await repo.get_participant(s.db, game.id, s.user_id)
    if me is not None and me.status == ACTIVE and not me.wishes and game.status == GameStatus.COLLECTING:
        await ask_for_wishes(s, game.id)


# --- §5.7 anonymous messages ------------------------------------------------------------------------------

_RELAY_KINDS = {
    RelayDirection.TO_RECEIVER: StateKind.RELAY_TO_RECEIVER,
    RelayDirection.TO_SANTA: StateKind.RELAY_TO_SANTA,
}
_RELAY_DIRECTIONS = {kind: direction for direction, kind in _RELAY_KINDS.items()}


async def start_relay(s: Session, game_id: int, direction: RelayDirection) -> None:
    route = await relay.route_new(s.db, game_id, s.user_id, direction)
    await repo.set_state(s.db, s.user_id, _RELAY_KINDS[direction], s.now(), game_id=game_id)
    if direction == RelayDirection.TO_RECEIVER:
        await s.say(views.prompt(texts.relay_prompt_to_receiver(route.recipient.display_name)))
    else:
        await s.say(views.prompt(texts.RELAY_PROMPT_TO_SANTA))


async def start_reply(s: Session, relay_id: int) -> None:
    route = await relay.route_reply(s.db, relay_id, s.user_id)
    await repo.set_state(s.db, s.user_id, StateKind.REPLY_RELAY, s.now(), game_id=route.game.id,
                         data={"relay_id": relay_id})
    await s.say(views.prompt(texts.RELAY_PROMPT_REPLY))


async def relay_input(s: Session, state: UserState, text: str, has_attachments: bool) -> None:
    """Pass a typed message to the counterpart. The route is re-checked before sending."""
    if state.kind == StateKind.REPLY_RELAY:
        route = await relay.route_reply(s.db, int(state.data["relay_id"]), s.user_id)
    else:
        assert state.game_id is not None
        route = await relay.route_new(s.db, state.game_id, s.user_id, _RELAY_DIRECTIONS[state.kind])
    cleaned = clean_text(text, multiline=True)
    if has_attachments or not cleaned:
        await s.say(views.prompt(texts.TEXT_ONLY if has_attachments else texts.RELAY_EMPTY))
        return
    if len(cleaned) > MAX_RELAY:
        await s.say(views.prompt(texts.relay_too_long(MAX_RELAY)))
        return
    message = await relay.send_relay(s.db, route, cleaned, s.now())
    await repo.clear_state(s.db, s.user_id)
    await s.ctx.outbox.enqueue(
        Target.user(route.recipient.user_id), views.relay_delivery(route.game, message, route.sender.display_name),
        disable_preview=True, game_id=route.game.id,
    )
    await s.say(texts.RELAY_SENT_ANONYMOUSLY if route.direction == RelayDirection.TO_RECEIVER
                else texts.RELAY_SENT_TO_SANTA)
