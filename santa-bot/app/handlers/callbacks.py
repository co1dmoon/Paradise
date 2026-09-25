"""Inline-button callbacks (§5): one handler per ``views.Action``.

Every callback is answered exactly once (``Session`` does the bookkeeping, and
``on_callback`` answers whatever is left). Payload arguments are only hints:
each handler reloads the game and re-checks the permission in the database.
Stale buttons (cancelled or drawn game, removed participant, finished step)
produce a friendly message, never an exception.

Other stages add buttons with ``@on("prefix", admin_only=True)``; admin-only
handlers refuse everyone not in ADMIN_USER_IDS.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import date, datetime

from app import repo
from app.context import AppContext
from app.core import billing, games, kb, relay, texts, users
from app.core.dates import DateError, format_date_button
from app.core.games import ACTIVE, WAITING
from app.core.models import Game, GameStatus, Participant, ParticipantStatus, RelayDirection, StateKind
from app.core.payloads import DIRECT_SOURCE, parse_start_payload
from app.core.pricing import PAID_TIERS, upgrade_options
from app.handlers import flows, notices, views
from app.handlers.flows import WizardStep
from app.handlers.session import Outdated, Refusal, Session, open_session
from app.handlers.views import Action, button
from app.max_api import CallbackQuery, OutMessage
from app.payments.robokassa import build_payment_url

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Args:
    """Callback payload arguments; anything malformed means the button is outdated."""

    values: tuple[str, ...]

    def number(self, index: int, default: int | None = None) -> int:
        if index >= len(self.values):
            if default is None:
                raise Outdated(f"missing argument {index}")
            return default
        value = self.values[index]
        if not value.lstrip("-").isdigit():
            raise Outdated(f"bad number {value!r}")
        return int(value)

    def text(self, index: int) -> str:
        if index >= len(self.values) or not self.values[index]:
            raise Outdated(f"missing argument {index}")
        return self.values[index]

    def date(self, index: int) -> date:
        try:
            return datetime.strptime(self.text(index), "%Y%m%d").date()
        except ValueError as error:
            raise Outdated(f"bad date {self.values[index]!r}") from error


CallbackHandler = Callable[[Session, Args], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class _Entry:
    handler: CallbackHandler
    admin_only: bool


_HANDLERS: dict[str, _Entry] = {}


def on(action: str, *, admin_only: bool = False) -> Callable[[CallbackHandler], CallbackHandler]:
    def register(handler: CallbackHandler) -> CallbackHandler:
        if action in _HANDLERS:
            raise ValueError(f"callback prefix {action!r} registered twice")
        _HANDLERS[action] = _Entry(handler, admin_only)
        return handler

    return register


async def on_callback(ctx: AppContext, callback: CallbackQuery) -> None:
    s = await open_session(ctx, callback.user, first_source=DIRECT_SOURCE, callback=callback)
    action, *values = kb.split_cb(callback.payload)
    entry = _HANDLERS.get(action)
    try:
        if entry is None:
            raise Outdated(f"unknown callback {action!r}")
        if entry.admin_only and not s.is_admin:
            await s.say(texts.NOT_ALLOWED)
        elif not s.user.has_consent and action != Action.CONSENT and not entry.admin_only:
            await s.say(views.consent(ctx.config))
        else:
            await entry.handler(s, Args(tuple(values)))
    except games.GameError as error:
        await s.refuse(error)
    except Outdated as error:
        log.info("outdated button", extra={"action": action, "reason": str(error)})
        await s.say(texts.BUTTON_OUTDATED)
    finally:
        await s.acknowledge()


# --- helpers ----------------------------------------------------------------------------------------


async def _organized(s: Session, game_id: int, *statuses: GameStatus) -> Game:
    """The game, if the user organizes it (and it is in one of ``statuses``)."""
    game = await games.load_game(s.db, game_id)
    games.require_organizer(game, s.user_id)
    if statuses:
        games.require_status(game, *statuses)
    return game


def _require_collecting(game: Game, after_draw: str) -> None:
    """Collecting only; after the draw say ``after_draw`` (a cancelled game has its own text)."""
    if game.status in (GameStatus.DRAWN, GameStatus.FINISHED):
        raise Refusal(after_draw)
    games.require_status(game, GameStatus.COLLECTING)


async def _people(s: Session, game_id: int, *statuses: ParticipantStatus) -> list[Participant]:
    return await repo.participants(s.db, game_id, *statuses)


# --- §5.1 consent and menu -----------------------------------------------------------------------------


@on(Action.CONSENT)
async def _consent(s: Session, args: Args) -> None:
    if s.user.has_consent:
        await flows.show_menu(s)
        return
    config = s.ctx.config
    s.user = await users.give_consent(s.db, s.user_id, s.profile.name, s.profile.username,
                                      config.consent_version, s.now())
    state = await repo.get_state(s.db, s.user_id, s.now())
    resume = state.data.get("payload") if state is not None and state.kind == StateKind.RESUME else None
    await repo.clear_state(s.db, s.user_id)
    await s.show(texts.CONSENT_THANKS)
    await flows.route_payload(s, parse_start_payload(resume))


@on(Action.MENU)
async def _menu(s: Session, args: Args) -> None:
    await flows.show_menu(s)


@on(Action.CREATE)
async def _create(s: Session, args: Args) -> None:
    await flows.start_wizard(s)


@on(Action.MY_GAMES)
async def _my_games(s: Session, args: Args) -> None:
    entries = await repo.games_of_user(s.db, s.user_id)
    await s.show(views.my_games(s.user_id, entries, args.number(0, default=0)))


@on(Action.JOIN_BY_CODE)
async def _join_by_code(s: Session, args: Args) -> None:
    await repo.set_state(s.db, s.user_id, StateKind.CODE, s.now())
    await s.say(views.prompt(texts.ASK_CODE))


@on(Action.HELP)
async def _help(s: Session, args: Args) -> None:
    await s.show(views.help_message(await s.ctx.settings(), s.ctx.config))


@on(Action.CANCEL_INPUT)
async def _cancel_input(s: Session, args: Args) -> None:
    cleared = await repo.clear_state(s.db, s.user_id)
    await s.show(views.menu(texts.INPUT_CANCELLED if cleared else texts.NOTHING_TO_CANCEL))


@on(Action.REF)
async def _ref(s: Session, args: Args) -> None:
    await flows.start_referred(s, args.text(0))


# --- §5.2 creation wizard ------------------------------------------------------------------------------


@on(Action.WIZARD_SKIP_TITLE)
async def _wizard_skip_title(s: Session, args: Args) -> None:
    await flows.wizard_set_title(s, await flows.load_wizard(s, WizardStep.TITLE), texts.DEFAULT_TITLE)


@on(Action.WIZARD_BUDGET)
async def _wizard_budget(s: Session, args: Args) -> None:
    draft = await flows.load_wizard(s, WizardStep.BUDGET)
    await flows.wizard_set_budget(s, draft, _budget_preset(args.number(0)))


@on(Action.WIZARD_BUDGET_CUSTOM)
async def _wizard_budget_custom(s: Session, args: Args) -> None:
    await flows.load_wizard(s, WizardStep.BUDGET)
    await s.say(views.prompt(texts.ASK_CUSTOM_BUDGET))


@on(Action.WIZARD_DATE)
async def _wizard_date(s: Session, args: Args) -> None:
    draft = await flows.load_wizard(s, WizardStep.DATE)
    await flows.wizard_set_date(s, draft, _suggested_date(s, args, 0))


@on(Action.WIZARD_DATE_CUSTOM)
async def _wizard_date_custom(s: Session, args: Args) -> None:
    await flows.load_wizard(s, WizardStep.DATE)
    await s.say(views.prompt(texts.ASK_CUSTOM_DATE))


@on(Action.WIZARD_DATE_UNKNOWN)
async def _wizard_date_unknown(s: Session, args: Args) -> None:
    await flows.wizard_set_date(s, await flows.load_wizard(s, WizardStep.DATE), None)


@on(Action.WIZARD_PARTICIPATES)
async def _wizard_participates(s: Session, args: Args) -> None:
    await flows.wizard_finish(s, args.number(0) == 1)


def _budget_preset(index: int) -> str:
    if not 0 <= index < len(texts.BUDGET_PRESETS):
        raise Outdated(f"no budget preset {index}")
    return texts.BUDGET_PRESETS[index]


def _suggested_date(s: Session, args: Args, index: int) -> date:
    """A date from a suggestion button; a button from an earlier day may be out of range now."""
    value = args.date(index)
    if not flows.valid_suggested_date(value, s.today()):
        raise Refusal(texts.DATE_ERRORS[DateError.TOO_EARLY])
    return value


# --- games, names and wishes (§5.3, §5.8) -------------------------------------------------------------------


@on(Action.OPEN_GAME)
async def _open_game(s: Session, args: Args) -> None:
    await flows.open_game(s, args.number(0))


@on(Action.MY_PARTICIPATION)
async def _my_participation(s: Session, args: Args) -> None:
    game_id = args.number(0)
    me = await flows.require_me(s, game_id)
    await flows.show_participant_view(s, await games.load_game(s.db, game_id), me)


@on(Action.PANEL)
async def _panel(s: Session, args: Args) -> None:
    await flows.show_panel(s, args.number(0))


@on(Action.WISHES)
async def _wishes(s: Session, args: Args) -> None:
    await flows.ask_for_wishes(s, args.number(0))


@on(Action.SURPRISE)
async def _surprise(s: Session, args: Args) -> None:
    await flows.save_wishes(s, args.number(0), texts.SURPRISE_WISHES)


@on(Action.KEEP_NAME)
async def _keep_name(s: Session, args: Args) -> None:
    me = await flows.require_me(s, args.number(0))
    await s.say(texts.name_saved(me.display_name))


@on(Action.CHANGE_NAME)
async def _change_name(s: Session, args: Args) -> None:
    await flows.ask_for_name(s, args.number(0))


@on(Action.LEAVE)
async def _leave(s: Session, args: Args) -> None:
    game = await games.load_game(s.db, args.number(0))
    await flows.require_me(s, game.id)
    _require_collecting(game, texts.LEAVE_AFTER_DRAW)
    await s.show(views.confirm_leave(game))


@on(Action.LEAVE_CONFIRM)
async def _leave_confirm(s: Session, args: Args) -> None:
    game_id = args.number(0)
    async with s.ctx.locks.game(game_id):
        game = await games.load_game(s.db, game_id)
        await flows.require_me(s, game_id)
        _require_collecting(game, texts.LEAVE_AFTER_DRAW)
        if game.organizer_id == s.user_id:
            activated = await games.set_organizer_participation(s.db, game_id, s.user_id, False, s.now())
        else:
            activated = (await games.leave_game(s.db, game_id, s.user_id, s.now())).activated
    await notices.participants_activated(s.ctx, game, activated)
    await s.show(OutMessage(texts.left_game(game.title), views.menu_keyboard()))


# --- §5.4 organizer panel: participants and exclusions --------------------------------------------------------


@on(Action.PARTICIPANTS)
async def _participants(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    await s.show(await _participants_screen(s, game))


async def _participants_screen(s: Session, game: Game, notice: str | None = None) -> OutMessage:
    active, queue = await _people(s, game.id, ACTIVE), await _people(s, game.id, WAITING)
    return views.participants_screen(game, active, queue, notice=notice)


@on(Action.REMOVE_PICK)
async def _remove_pick(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    _require_collecting(game, texts.ALREADY_DRAWN_ACTION)
    people = [p for p in await _people(s, game.id, ACTIVE, WAITING) if p.user_id != game.organizer_id]
    if not people:
        raise Refusal(texts.NOBODY_TO_REMOVE)
    await s.show(views.person_picker(
        texts.PICK_PARTICIPANT_TO_REMOVE, people, args.number(1, default=0),
        lambda person: button(person.display_name, Action.REMOVE, game.id, person.user_id),
        lambda page: kb.cb(Action.REMOVE_PICK, game.id, page),
        button(texts.BTN_BACK, Action.PARTICIPANTS, game.id),
    ))


@on(Action.REMOVE)
async def _remove(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    _require_collecting(game, texts.ALREADY_DRAWN_ACTION)
    await s.show(views.confirm_remove(game, await _removable(s, game, args.number(1))))


@on(Action.REMOVE_CONFIRM)
async def _remove_confirm(s: Session, args: Args) -> None:
    game_id, user_id = args.number(0), args.number(1)
    async with s.ctx.locks.game(game_id):
        game = await _organized(s, game_id)
        _require_collecting(game, texts.ALREADY_DRAWN_ACTION)
        person = await _removable(s, game, user_id)
        departure = await games.remove_participant(s.db, game_id, s.user_id, user_id, s.now())
    await notices.participant_removed(s.ctx, game, person)
    await notices.participants_activated(s.ctx, game, departure.activated)
    await s.show(await _participants_screen(s, game, texts.participant_removed(person.display_name)))


async def _removable(s: Session, game: Game, user_id: int) -> Participant:
    person = await repo.get_participant(s.db, game.id, user_id)
    if person is None or person.status not in (ACTIVE, WAITING) or user_id == game.organizer_id:
        raise Refusal(texts.PARTICIPANT_GONE)
    return person


@on(Action.EXCLUSIONS)
async def _exclusions(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    _require_collecting(game, texts.ALREADY_DRAWN_ACTION)
    await s.show(await _exclusions_screen(s, game, args.number(1, default=0)))


async def _exclusions_screen(s: Session, game: Game, page: int = 0, notice: str | None = None) -> OutMessage:
    names = {p.user_id: p.display_name for p in await _people(s, game.id)}
    return views.exclusions_screen(game, await repo.exclusions(s.db, game.id), names, page, notice=notice)


@on(Action.EXCLUSION_ADD)
async def _exclusion_add(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    _require_collecting(game, texts.ALREADY_DRAWN_ACTION)
    active = await _people(s, game.id, ACTIVE)
    if len(active) < 2:
        raise Refusal(texts.NEED_TWO_FOR_EXCLUSION)
    await s.show(views.person_picker(
        texts.EXCLUSIONS_PROMPT, active, args.number(1, default=0),
        lambda person: button(person.display_name, Action.EXCLUSION_FIRST, game.id, person.user_id),
        lambda page: kb.cb(Action.EXCLUSION_ADD, game.id, page),
        button(texts.BTN_BACK, Action.EXCLUSIONS, game.id, 0),
    ))


@on(Action.EXCLUSION_FIRST)
async def _exclusion_first(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    _require_collecting(game, texts.ALREADY_DRAWN_ACTION)
    first_id = args.number(1)
    active = await _people(s, game.id, ACTIVE)
    first = next((p for p in active if p.user_id == first_id), None)
    if first is None:
        raise Refusal(texts.EXCLUSION_NOT_PARTICIPANT)
    await s.show(views.person_picker(
        texts.pick_second_excluded(first.display_name), [p for p in active if p.user_id != first_id],
        args.number(2, default=0),
        lambda person: button(person.display_name, Action.EXCLUSION_SECOND, game.id, first_id, person.user_id),
        lambda page: kb.cb(Action.EXCLUSION_FIRST, game.id, first_id, page),
        button(texts.BTN_BACK, Action.EXCLUSION_ADD, game.id, 0),
    ))


@on(Action.EXCLUSION_SECOND)
async def _exclusion_second(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    a, b = args.number(1), args.number(2)
    added = await games.add_exclusion(s.db, game.id, s.user_id, a, b)
    names = {p.user_id: p.display_name for p in await _people(s, game.id)}
    notice = texts.exclusion_saved(names[a], names[b]) if added else texts.EXCLUSION_EXISTS
    await s.show(await _exclusions_screen(s, game, notice=notice))


@on(Action.EXCLUSION_DELETE)
async def _exclusion_delete(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    await games.remove_exclusion(s.db, game.id, s.user_id, args.number(1), args.number(2))
    await s.show(await _exclusions_screen(s, game, notice=texts.EXCLUSION_REMOVED))


# --- §5.4 organizer panel: reminder, invite, draw -------------------------------------------------------------


@on(Action.REMIND)
async def _remind(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    people = await games.start_wish_reminder(s.db, game.id, s.user_id, s.now())
    if not people:
        raise Refusal(texts.NOBODY_TO_REMIND)
    await notices.wish_reminders(s.ctx, game, people)
    await s.say(texts.reminded(len(people)))


@on(Action.INVITE)
async def _invite(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0))
    _require_collecting(game, texts.ALREADY_DRAWN_ACTION)
    names = [p.display_name for p in await _people(s, game.id, ACTIVE)]
    await s.say(views.invite(s.ctx.config, game, names))


@on(Action.DRAW)
async def _draw(s: Session, args: Args) -> None:
    game = await games.load_game(s.db, args.number(0))
    preview = await games.draw_preview(s.db, game.id, s.user_id)
    await s.show(views.draw_confirm(game, preview.active, preview.without_wishes, preview.waiting))


@on(Action.DRAW_CONFIRM)
async def _draw_confirm(s: Session, args: Args) -> None:
    game_id = args.number(0)
    await s.acknowledge()  # the search may take up to 2 s; MAX expects an answer within ~1 s
    try:
        async with s.ctx.locks.game(game_id):
            result = await games.run_draw(s.db, game_id, s.user_id, now=s.now(), rng=s.ctx.rng)
    except games.DrawImpossible:
        await s.say(OutMessage(texts.DRAW_IMPOSSIBLE, kb.keyboard(
            button(texts.BTN_EXCLUSIONS, Action.EXCLUSIONS, game_id, 0), views.panel_button(game_id))))
        return
    await notices.draw_results(s.ctx, result)
    await s.say(OutMessage(texts.DRAW_STARTED, kb.keyboard(views.panel_button(game_id))))


@on(Action.NOT_RECEIVED)
async def _not_received(s: Session, args: Args) -> None:
    people = await games.undelivered_results(s.db, args.number(0), s.user_id)
    await s.say(texts.not_received([p.display_name for p in people]))


# --- §5.4 settings ----------------------------------------------------------------------------------------

_EDITABLE = (GameStatus.COLLECTING, GameStatus.DRAWN)


@on(Action.SETTINGS)
async def _settings(s: Session, args: Args) -> None:
    await s.show(views.settings_screen(await _organized(s, args.number(0), *_EDITABLE)))


async def _edit(s: Session, args: Args, kind: StateKind, message: OutMessage) -> None:
    """Wait for a typed value of a setting (the state carries the game id)."""
    game = await _organized(s, args.number(0), *_EDITABLE)
    await repo.set_state(s.db, s.user_id, kind, s.now(), game_id=game.id)
    await s.say(message)


@on(Action.SET_TITLE)
async def _set_title(s: Session, args: Args) -> None:
    await _edit(s, args, StateKind.TITLE, views.prompt(texts.ASK_TITLE))


@on(Action.SET_BUDGET)
async def _set_budget(s: Session, args: Args) -> None:
    game_id = args.number(0)
    await _edit(s, args, StateKind.BUDGET_CUSTOM, views.ask_budget(
        lambda index: button(texts.BUDGET_PRESETS[index], Action.SET_BUDGET_PRESET, game_id, index),
        button(texts.BTN_CUSTOM_BUDGET, Action.SET_BUDGET_CUSTOM, game_id),
    ))


@on(Action.SET_BUDGET_PRESET)
async def _set_budget_preset(s: Session, args: Args) -> None:
    await flows.save_setting(s, args.number(0), budget_text=_budget_preset(args.number(1)))


@on(Action.SET_BUDGET_CUSTOM)
async def _set_budget_custom(s: Session, args: Args) -> None:
    await _edit(s, args, StateKind.BUDGET_CUSTOM, views.prompt(texts.ASK_CUSTOM_BUDGET))


@on(Action.SET_DATE)
async def _set_date(s: Session, args: Args) -> None:
    game_id = args.number(0)
    await _edit(s, args, StateKind.DATE_CUSTOM, views.ask_date(
        s.today(),
        lambda value: button(format_date_button(value), Action.SET_DATE_PICK, game_id, views.date_arg(value)),
        button(texts.BTN_CUSTOM_DATE, Action.SET_DATE_CUSTOM, game_id),
        button(texts.BTN_DATE_UNKNOWN, Action.SET_DATE_UNKNOWN, game_id),
    ))


@on(Action.SET_DATE_PICK)
async def _set_date_pick(s: Session, args: Args) -> None:
    await flows.save_setting(s, args.number(0), exchange_date=_suggested_date(s, args, 1))


@on(Action.SET_DATE_CUSTOM)
async def _set_date_custom(s: Session, args: Args) -> None:
    await _edit(s, args, StateKind.DATE_CUSTOM, views.prompt(texts.ASK_CUSTOM_DATE))


@on(Action.SET_DATE_UNKNOWN)
async def _set_date_unknown(s: Session, args: Args) -> None:
    await flows.save_setting(s, args.number(0), exchange_date=None)


@on(Action.TOGGLE_ANON_CHAT)
async def _toggle_anon_chat(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0), *_EDITABLE)
    updated = await games.update_game_settings(s.db, game.id, s.user_id, anon_chat=not game.anon_chat)
    await s.show(views.settings_screen(updated))


@on(Action.TOGGLE_REMINDER)
async def _toggle_reminder(s: Session, args: Args) -> None:
    game = await _organized(s, args.number(0), *_EDITABLE)
    updated = await games.update_game_settings(s.db, game.id, s.user_id, reminder_on=not game.reminder_on)
    await s.show(views.settings_screen(updated))


@on(Action.TOGGLE_PARTICIPATION)
async def _toggle_participation(s: Session, args: Args) -> None:
    game_id = args.number(0)
    async with s.ctx.locks.game(game_id):
        game = await _organized(s, game_id)
        _require_collecting(game, texts.PARTICIPATION_ONLY_BEFORE_DRAW)
        participates = not game.organizer_participates
        activated = await games.set_organizer_participation(s.db, game_id, s.user_id, participates, s.now())
    await notices.participants_activated(s.ctx, game, activated)
    await s.show(views.settings_screen(await games.load_game(s.db, game_id)))
    me = await repo.get_participant(s.db, game_id, s.user_id)
    if participates and me is not None and not me.wishes:
        await flows.ask_for_wishes(s, game_id)


@on(Action.CANCEL_GAME)
async def _cancel_game(s: Session, args: Args) -> None:
    await s.show(views.confirm_cancel_game(await _organized(s, args.number(0), *_EDITABLE)))


@on(Action.CANCEL_GAME_CONFIRM)
async def _cancel_game_confirm(s: Session, args: Args) -> None:
    game_id = args.number(0)
    async with s.ctx.locks.game(game_id):
        people = await games.cancel_game(s.db, game_id, s.user_id, s.now())
    game = await games.load_game(s.db, game_id)
    await notices.game_cancelled(s.ctx, game, people)
    await s.show(OutMessage(texts.game_cancelled_by_you(game.title), views.menu_keyboard()))


# --- §5.6 upgrades and payment -------------------------------------------------------------------------------


@on(Action.UPGRADE)
async def _upgrade(s: Session, args: Args) -> None:
    game = await games.load_game(s.db, args.number(0))
    _require_collecting(game, texts.UPGRADE_ONLY_BEFORE_DRAW)
    if game.organizer_id != s.user_id:
        await games.require_participant(s.db, game.id, s.user_id, ACTIVE, WAITING)
    if not s.ctx.config.payments_enabled:
        raise Refusal(texts.PAYMENTS_UNAVAILABLE)
    prices = await s.ctx.prices()
    options = upgrade_options(prices, game.participant_limit, await repo.paid_sum(s.db, game.id))
    if not options:
        raise Refusal(texts.upgrade_contact_us(max_limit=prices.max_limit, support_email=s.ctx.config.support_email))
    await s.say(views.choose_tier(game, options))


@on(Action.PAY)
async def _pay(s: Session, args: Args) -> None:
    """pay:{CODE}:{tier}: create or reuse the payment row and send the Robokassa link."""
    config = s.ctx.config
    if not config.payments_enabled:
        raise Refusal(texts.PAYMENTS_UNAVAILABLE)
    game = await repo.get_game_by_code(s.db, args.text(0))
    tier = next((t for t in PAID_TIERS if t == args.text(1)), None)
    if game is None or tier is None:
        raise Outdated("unknown game or tier")
    _require_collecting(game, texts.UPGRADE_ONLY_BEFORE_DRAW)
    prices = await s.ctx.prices()
    async with s.ctx.locks.game(game.id):
        result = await billing.request_upgrade(s.db, game.id, s.user_id, tier, prices, s.now())
    if isinstance(result, billing.PaymentOutcome):
        await notices.payment_applied(s.ctx, result)
        return
    limit = prices.limit_for(result.tier)
    url = build_payment_url(config, inv_id=result.inv_id, amount_rub=result.amount_rub,
                            description=texts.payment_description(limit))
    await s.say(views.pay_offer(game, result.amount_rub, limit, url))


# --- §5.5, §5.7 after the draw: pairs and anonymous messages ----------------------------------------------------


@on(Action.WHOM)
async def _whom(s: Session, args: Args) -> None:
    game = await games.load_game(s.db, args.number(0))
    await flows.require_me(s, game.id)
    receiver = await games.my_receiver(s.db, game.id, s.user_id)
    if receiver is None:
        raise Refusal(texts.NOT_DRAWN_YET if game.status == GameStatus.COLLECTING else texts.BUTTON_OUTDATED)
    await s.say(views.whom_do_i_gift(game, receiver))


@on(Action.ASK_RECEIVER)
async def _ask_receiver(s: Session, args: Args) -> None:
    await _start_relay(s, args.number(0), RelayDirection.TO_RECEIVER)


@on(Action.WRITE_SANTA)
async def _write_santa(s: Session, args: Args) -> None:
    await _start_relay(s, args.number(0), RelayDirection.TO_SANTA)


async def _start_relay(s: Session, game_id: int, direction: RelayDirection) -> None:
    game = await games.load_game(s.db, game_id)
    if game.status == GameStatus.COLLECTING:
        raise Refusal(texts.RELAY_NOT_AVAILABLE)
    await flows.start_relay(s, game_id, direction)


@on(Action.REPLY)
async def _reply(s: Session, args: Args) -> None:
    await flows.start_reply(s, args.number(0))


@on(Action.REPORT)
async def _report(s: Session, args: Args) -> None:
    report = await relay.report_relay(s.db, args.number(0), s.user_id, s.now())
    game = None if report.game_id is None else await repo.get_game(s.db, report.game_id)
    await s.ctx.alerts.notify_admins(views.report_to_admins(report, game.code if game else None))
    await s.say(texts.REPORT_SENT)


@on(Action.BLOCK_REPORTED, admin_only=True)
async def _block_reported(s: Session, args: Args) -> None:
    report = await repo.get_report(s.db, args.number(0))
    if report is None:
        raise Outdated("no such report")
    await repo.set_blocked(s.db, report.reported_id, True)
    await repo.resolve_report(s.db, report.id)
    await s.say(OutMessage(texts.user_blocked(report.reported_id),
                           kb.keyboard(views.unblock_button(report.reported_id))))


@on(Action.CLOSE_REPORT, admin_only=True)
async def _close_report(s: Session, args: Args) -> None:
    if await repo.get_report(s.db, args.number(0)) is None:
        raise Outdated("no such report")
    await repo.resolve_report(s.db, args.number(0))
    await s.say(texts.REPORT_CLOSED)
