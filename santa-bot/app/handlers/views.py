"""Screens of the bot: ``OutMessage`` builders and the callback payload vocabulary (§5).

Every inline button payload is ``Action[:arg...]`` (see ``kb.cb``). Game-bound
payloads carry the game id (short and ASCII); ``pay`` and ``ref`` carry the game
code as the spec prescribes. Payloads are never trusted for authorization:
handlers re-check everything in the database.

Builders are pure: they take records and numbers, never touch the database.
Other stages (payments, scheduler, admin) should reuse them for their messages,
e.g. ``spot_opened`` after a payment or ``draw_button`` in the organizer nudge.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import date
from enum import StrEnum

from app.config import Config
from app.core import kb, texts
from app.core.dates import format_date_button, suggest_dates
from app.core.games import GameCounts
from app.core.kb import Button
from app.core.models import Game, GameStatus, Participant, ParticipantStatus, Relay, RelayDirection, Report, Settings
from app.core.payloads import deep_link, join_payload
from app.core.pricing import Upgrade
from app.max_api import OutMessage


class Action(StrEnum):
    """Callback payload prefixes."""

    CONSENT = "c"
    MENU = "m"
    CREATE = "new"
    MY_GAMES = "my"
    JOIN_BY_CODE = "code"
    HELP = "help"
    CANCEL_INPUT = "x"
    WIZARD_SKIP_TITLE = "ws"
    WIZARD_BUDGET = "wb"
    WIZARD_BUDGET_CUSTOM = "wbc"
    WIZARD_DATE = "wd"
    WIZARD_DATE_CUSTOM = "wdc"
    WIZARD_DATE_UNKNOWN = "wdn"
    WIZARD_PARTICIPATES = "wp"
    OPEN_GAME = "g"
    MY_PARTICIPATION = "gv"
    PANEL = "pn"
    WISHES = "ww"
    SURPRISE = "sm"
    KEEP_NAME = "nk"
    CHANGE_NAME = "nc"
    LEAVE = "lv"
    LEAVE_CONFIRM = "lvy"
    PARTICIPANTS = "pl"
    REMOVE_PICK = "rp"
    REMOVE = "rm"
    REMOVE_CONFIRM = "rmy"
    EXCLUSIONS = "ex"
    EXCLUSION_ADD = "ea"
    EXCLUSION_FIRST = "e1"
    EXCLUSION_SECOND = "e2"
    EXCLUSION_DELETE = "exd"
    REMIND = "rw"
    DRAW = "dr"
    DRAW_CONFIRM = "dry"
    INVITE = "inv"
    NOT_RECEIVED = "nr"
    SETTINGS = "st"
    SET_TITLE = "stt"
    SET_BUDGET = "stb"
    SET_BUDGET_PRESET = "sb"
    SET_BUDGET_CUSTOM = "sbc"
    SET_DATE = "std"
    SET_DATE_PICK = "sd"
    SET_DATE_CUSTOM = "sdc"
    SET_DATE_UNKNOWN = "sdn"
    TOGGLE_PARTICIPATION = "stp"
    TOGGLE_ANON_CHAT = "sta"
    TOGGLE_REMINDER = "str"
    CANCEL_GAME = "stc"
    CANCEL_GAME_CONFIRM = "stcy"
    UPGRADE = "up"
    PAY = "pay"
    WHOM = "who"
    ASK_RECEIVER = "ask"
    WRITE_SANTA = "ts"
    REPLY = "rr"
    REPORT = "rep"
    REF = "ref"
    BLOCK_REPORTED = "rb"
    CLOSE_REPORT = "rc"


def button(text: str, action: Action, *args: str | int) -> kb.CallbackButton:
    return kb.callback(text, kb.cb(action, *args))


def date_arg(value: date) -> str:
    return f"{value:%Y%m%d}"


def cancel_button() -> kb.CallbackButton:
    return button(texts.BTN_CANCEL, Action.CANCEL_INPUT)


def panel_button(game_id: int) -> kb.CallbackButton:
    return button(texts.BTN_PANEL, Action.PANEL, game_id)


def draw_button(game_id: int) -> kb.CallbackButton:
    """[Провести жеребьёвку]: also for the organizer nudge (§5.9 b)."""
    return button(texts.BTN_DRAW, Action.DRAW, game_id)


def whom_button(game_id: int) -> kb.CallbackButton:
    """[Кому я дарю?]: also for the pre-exchange reminder (§5.9 c)."""
    return button(texts.BTN_WHOM_DO_I_GIFT, Action.WHOM, game_id)


def upgrade_button(game_id: int, text: str = texts.BTN_UPGRADE_SHORT) -> kb.CallbackButton:
    """[Расширить]: opens the tier choice; also for the waiting notice (§5.6)."""
    return button(text, Action.UPGRADE, game_id)


def pay_button(game: Game, upgrade: Upgrade) -> kb.CallbackButton:
    return button(texts.btn_pay(upgrade.amount), Action.PAY, game.code, upgrade.tier)


def ref_button(game: Game, text: str = texts.BTN_NEW_GAME_ELSEWHERE) -> kb.CallbackButton:
    """[Устроить игру в другом чате] (§6.3): a new game referred by ``game``."""
    return button(text, Action.REF, game.code)


def surprise_button(game_id: int) -> kb.CallbackButton:
    return button(texts.BTN_SURPRISE_ME, Action.SURPRISE, game_id)


def invite_link(config: Config, game: Game) -> str:
    return deep_link(config.max_bot_username, join_payload(game.code))


# --- §5.1 consent, menu, help ---------------------------------------------------------------


def consent(config: Config) -> OutMessage:
    return OutMessage(texts.consent(config.public_base_url), kb.keyboard(button(texts.BTN_CONSENT, Action.CONSENT)))


def menu_keyboard() -> kb.Keyboard:
    return kb.keyboard(
        [button(texts.BTN_CREATE_GAME, Action.CREATE), button(texts.BTN_MY_GAMES, Action.MY_GAMES)],
        [button(texts.BTN_JOIN_BY_CODE, Action.JOIN_BY_CODE), button(texts.BTN_HOW_IT_WORKS, Action.HELP)],
    )


def menu(text: str = texts.MENU) -> OutMessage:
    return OutMessage(text, menu_keyboard())


def help_message(settings: Settings, config: Config) -> OutMessage:
    text = texts.help_text(
        free_limit=settings.free_limit, price_S=settings.price_S, limit_S=settings.limit_S,
        price_M=settings.price_M, limit_M=settings.limit_M, price_L=settings.price_L, limit_L=settings.limit_L,
        support_email=config.support_email, base_url=config.public_base_url,
    )
    return OutMessage(text, kb.keyboard(button(texts.BTN_MENU, Action.MENU)))


def prompt(text: str, *extra: Button) -> OutMessage:
    """A question that waits for text, with optional extra buttons and [Отмена]."""
    return OutMessage(text, kb.keyboard(list(extra), cancel_button()))


# --- §5.2 creation wizard and the invite -------------------------------------------------------


def ask_title() -> OutMessage:
    return OutMessage(texts.ASK_TITLE, kb.keyboard([button(texts.BTN_SKIP, Action.WIZARD_SKIP_TITLE), cancel_button()]))


def ask_budget(preset: Callable[[int], kb.CallbackButton], custom: kb.CallbackButton) -> OutMessage:
    """Budget presets (``preset(index)`` builds each button) plus [Свой вариант] and [Отмена]."""
    presets = [preset(index) for index in range(len(texts.BUDGET_PRESETS))]
    return OutMessage(texts.ASK_BUDGET, kb.keyboard(presets[:3], [*presets[3:], custom], cancel_button()))


def ask_date(
    today: date,
    pick: Callable[[date], kb.CallbackButton],
    custom: kb.CallbackButton,
    unknown: kb.CallbackButton,
) -> OutMessage:
    dates = [pick(value) for value in suggest_dates(today)]
    rows = [dates[i : i + 3] for i in range(0, len(dates), 3)]
    return OutMessage(texts.ASK_DATE, kb.keyboard(*rows, [custom, unknown], cancel_button()))


def wizard_date_button(value: date) -> kb.CallbackButton:
    return button(format_date_button(value), Action.WIZARD_DATE, date_arg(value))


def ask_participates() -> OutMessage:
    return OutMessage(
        texts.ASK_PARTICIPATES,
        kb.keyboard(
            [button(texts.BTN_I_PARTICIPATE, Action.WIZARD_PARTICIPATES, 1),
             button(texts.BTN_ONLY_ORGANIZE, Action.WIZARD_PARTICIPATES, 0)],
            cancel_button(),
        ),
    )


def invite(config: Config, game: Game, participants: Sequence[str] = ()) -> OutMessage:
    """The forwardable invite: plain text only, so forwarding keeps the link and the code."""
    return OutMessage(
        texts.invite(
            title=game.title, budget=game.budget_text, exchange_date=game.exchange_date,
            link=invite_link(config, game), code=game.code, participants=participants,
        )
    )


def game_created(game: Game) -> OutMessage:
    if game.organizer_participates:
        return OutMessage(
            texts.GAME_CREATED,
            kb.keyboard([button(texts.BTN_MY_WISHES, Action.WISHES, game.id), panel_button(game.id)]),
        )
    return OutMessage(texts.GAME_CREATED_NOT_PARTICIPATING, kb.keyboard(panel_button(game.id)))


# --- §5.3 joining, names and wishes -------------------------------------------------------------


def joined(game: Game, organizer: str, name: str) -> OutMessage:
    return OutMessage(
        texts.joined(title=game.title, organizer=organizer, budget=game.budget_text,
                     exchange_date=game.exchange_date, name=name),
        kb.keyboard([button(texts.BTN_KEEP_NAME, Action.KEEP_NAME, game.id),
                     button(texts.BTN_CHANGE_NAME, Action.CHANGE_NAME, game.id)]),
    )


def already_drawn(game: Game) -> OutMessage:
    return OutMessage(texts.ALREADY_DRAWN, kb.keyboard(ref_button(game, texts.BTN_CREATE_GAME)))


def ask_wishes(game_id: int, current: str | None = None) -> OutMessage:
    text = texts.ASK_WISHES if not current else f"{texts.ASK_WISHES}\n{texts.current_wishes(current)}"
    return prompt(text, surprise_button(game_id))


def wishes_saved(game: Game) -> OutMessage:
    return OutMessage(
        texts.WISHES_SAVED,
        kb.keyboard(
            [button(texts.BTN_EDIT_WISHES, Action.WISHES, game.id), button(texts.BTN_LEAVE, Action.LEAVE, game.id)],
            ref_button(game),
        ),
    )


def waiting(game: Game, settings: Settings, upgrade: Upgrade | None) -> OutMessage:
    """§5.6: the queue message; ``upgrade`` is None when nobody can pay (top tier, payments off)."""
    if upgrade is None:
        return OutMessage(texts.WAITING_LIST_FULL)
    text = texts.waiting_list(free_limit=settings.free_limit, limit=upgrade.limit, price=upgrade.amount)
    return OutMessage(text, kb.keyboard(pay_button(game, upgrade)))


def spot_opened(game: Game) -> OutMessage:
    """A waiting person got a place (after a payment or when someone left)."""
    return OutMessage(
        texts.spot_opened(game.title),
        kb.keyboard([button(texts.BTN_WRITE_WISHES, Action.WISHES, game.id), surprise_button(game.id)]),
    )


def confirm_leave(game: Game) -> OutMessage:
    return OutMessage(
        texts.confirm_leave(game.title),
        kb.keyboard([button(texts.BTN_CONFIRM_LEAVE, Action.LEAVE_CONFIRM, game.id),
                     button(texts.BTN_CANCEL, Action.OPEN_GAME, game.id)]),
    )


# --- §5.8 my games and the participant's view ------------------------------------------------------


def my_games(user_id: int, entries: Sequence[tuple[Game, Participant | None]], page: int) -> OutMessage:
    if not entries:
        return menu(texts.NO_GAMES)
    current = kb.paginate(entries, page)
    lines = [
        texts.my_game_line(title=game.title, status=game.status, role=_role(user_id, game, participant))
        for game, participant in current.items
    ]
    buttons = [button(game.title, Action.OPEN_GAME, game.id) for game, _ in current.items]
    keyboard = kb.paged_keyboard(
        buttons, current.number, lambda number: kb.cb(Action.MY_GAMES, number),
        footer=[button(texts.BTN_MENU, Action.MENU)],
    )
    return OutMessage(texts.fit_lines(lines, header=texts.MY_GAMES_HEADER), keyboard)


def _role(user_id: int, game: Game, participant: Participant | None) -> str:
    if game.organizer_id == user_id:
        return texts.ROLE_ORGANIZER
    if participant is not None and participant.status == ParticipantStatus.WAITING:
        return texts.ROLE_WAITING
    return texts.ROLE_PARTICIPANT


def participant_view(game: Game, me: Participant, organizer: str, upgrade: Upgrade | None) -> OutMessage:
    """The game as a participant sees it, with the buttons that fit its status (§5.8)."""
    text = texts.game_view(
        title=game.title, organizer=organizer, budget=game.budget_text, exchange_date=game.exchange_date,
        status=game.status, name=me.display_name, wishes=me.wishes, waiting=me.status == ParticipantStatus.WAITING,
    )
    rows: list[Sequence[Button] | Button] = []
    if game.status == GameStatus.COLLECTING:
        rows.append([button(texts.BTN_EDIT_WISHES, Action.WISHES, game.id),
                     button(texts.BTN_CHANGE_NAME, Action.CHANGE_NAME, game.id),
                     button(texts.BTN_LEAVE, Action.LEAVE, game.id)])
        if me.status == ParticipantStatus.WAITING and upgrade is not None:
            rows.append(pay_button(game, upgrade))
    elif game.status in (GameStatus.DRAWN, GameStatus.FINISHED) and me.status == ParticipantStatus.ACTIVE:
        rows.append(whom_button(game.id))
        if game.status == GameStatus.DRAWN:
            rows.extend(relay_buttons(game))
            rows.append(button(texts.BTN_EDIT_WISHES, Action.WISHES, game.id))
    if game.organizer_id == me.user_id:
        rows.append(panel_button(game.id))
    rows.append(ref_button(game))
    return OutMessage(text, kb.keyboard(*rows))


def relay_buttons(game: Game) -> list[kb.CallbackButton]:
    if not game.anon_chat:
        return []
    return [button(texts.BTN_ASK_RECEIVER, Action.ASK_RECEIVER, game.id),
            button(texts.BTN_WRITE_SANTA, Action.WRITE_SANTA, game.id)]


# --- §5.4 organizer panel --------------------------------------------------------------------------


def panel(game: Game, counts: GameCounts, upgrade: Upgrade | None) -> OutMessage:
    """The organizer's panel. ``upgrade`` is the next paid tier, or None (top tier or payments off)."""
    if game.status == GameStatus.COLLECTING:
        text = texts.panel(
            title=game.title, code=game.code, active=counts.active, limit=game.participant_limit,
            waiting=counts.waiting, without_wishes=counts.without_wishes, exclusions=counts.exclusions,
            budget=game.budget_text, exchange_date=game.exchange_date,
        )
        rows: list[Sequence[Button] | Button] = [
            [button(texts.BTN_PARTICIPANTS, Action.PARTICIPANTS, game.id),
             button(texts.BTN_EXCLUSIONS, Action.EXCLUSIONS, game.id, 0)],
            button(texts.BTN_REMIND_WISHES, Action.REMIND, game.id),
            draw_button(game.id),
            [button(texts.BTN_INVITE, Action.INVITE, game.id), button(texts.BTN_SETTINGS, Action.SETTINGS, game.id)],
        ]
        crowded = counts.active >= game.participant_limit - 2 or counts.waiting > 0
        if upgrade is not None and crowded:
            rows.append(upgrade_button(game.id, texts.BTN_UPGRADE))
    else:
        text = texts.panel_drawn(title=game.title, code=game.code, active=counts.active,
                                 budget=game.budget_text, exchange_date=game.exchange_date)
        if game.status == GameStatus.CANCELLED:
            text = f"{text}\n{texts.GAME_CANCELLED}"
        rows = [button(texts.BTN_PARTICIPANTS, Action.PARTICIPANTS, game.id)]
        if game.status in (GameStatus.DRAWN, GameStatus.FINISHED):
            rows.append(button(texts.BTN_NOT_RECEIVED, Action.NOT_RECEIVED, game.id))
        if game.status == GameStatus.DRAWN:
            rows.append([button(texts.BTN_REMIND_WISHES, Action.REMIND, game.id),
                         button(texts.BTN_SETTINGS, Action.SETTINGS, game.id)])
    if game.organizer_participates and game.status != GameStatus.CANCELLED:
        rows.append(button(texts.BTN_MY_PARTICIPATION, Action.MY_PARTICIPATION, game.id))
    return OutMessage(text, kb.keyboard(*rows))


def participants_screen(
    game: Game, active: Sequence[Participant], queue: Sequence[Participant], *, notice: str | None = None
) -> OutMessage:
    """The numbered list with 'пожелания есть/нет'; ``notice`` (e.g. who was removed) goes on top."""
    entries = [(p.display_name, bool(p.wishes)) for p in active]
    rows: list[Sequence[Button] | Button] = []
    removable = [p for p in (*active, *queue) if p.user_id != game.organizer_id]
    if game.status == GameStatus.COLLECTING and removable:
        rows.append(button(texts.BTN_REMOVE_PARTICIPANT, Action.REMOVE_PICK, game.id, 0))
    rows.append(panel_button(game.id))
    text = texts.participants_list(entries, [p.display_name for p in queue])
    return OutMessage(_with_notice(notice, text), kb.keyboard(*rows))


def _with_notice(notice: str | None, text: str) -> str:
    return texts.fit_lines(text.split("\n"), header=f"{notice}\n") if notice else text


def person_picker(
    text: str,
    people: Sequence[Participant],
    page: int,
    pick: Callable[[Participant], kb.CallbackButton],
    turn_page: Callable[[int], str],
    back: kb.CallbackButton,
) -> OutMessage:
    """Eight people per page with Назад/Дальше, then a back button."""
    buttons = [pick(person) for person in people]
    return OutMessage(text, kb.paged_keyboard(buttons, page, turn_page, footer=[back]))


def confirm_remove(game: Game, person: Participant) -> OutMessage:
    return OutMessage(
        texts.confirm_remove(person.display_name),
        kb.keyboard([button(texts.BTN_CONFIRM_REMOVE, Action.REMOVE_CONFIRM, game.id, person.user_id),
                     button(texts.BTN_CANCEL, Action.PARTICIPANTS, game.id)]),
    )


def exclusions_screen(
    game: Game, pairs: Sequence[tuple[int, int]], names: dict[int, str], page: int, *, notice: str | None = None
) -> OutMessage:
    named = [(names.get(a, "?"), names.get(b, "?")) for a, b in pairs]
    text = _with_notice(notice, f"{texts.exclusions_list(named)}\n\n{texts.EXCLUSIONS_HINT}")
    remove = [
        button(texts.btn_remove_pair(number), Action.EXCLUSION_DELETE, game.id, a, b)
        for number, (a, b) in enumerate(pairs, start=1)
    ]
    footer = [[button(texts.BTN_ADD_PAIR, Action.EXCLUSION_ADD, game.id, 0), panel_button(game.id)]]
    keyboard = kb.paged_keyboard(remove, page, lambda number: kb.cb(Action.EXCLUSIONS, game.id, number),
                                 per_row=2, footer=footer)
    return OutMessage(text, keyboard)


def draw_confirm(game: Game, active: int, without_wishes: int, waiting_count: int) -> OutMessage:
    return OutMessage(
        texts.draw_confirm(active=active, without_wishes=without_wishes, waiting=waiting_count),
        kb.keyboard([button(texts.BTN_CONFIRM_DRAW, Action.DRAW_CONFIRM, game.id),
                     button(texts.BTN_CANCEL, Action.PANEL, game.id)]),
    )


def settings_screen(game: Game, *, notice: str | None = None) -> OutMessage:
    rows: list[Sequence[Button] | Button] = [
        [button(texts.BTN_SET_TITLE, Action.SET_TITLE, game.id),
         button(texts.BTN_SET_BUDGET, Action.SET_BUDGET, game.id),
         button(texts.BTN_SET_DATE, Action.SET_DATE, game.id)],
    ]
    if game.status == GameStatus.COLLECTING:
        rows.append(button(texts.btn_participation(game.organizer_participates), Action.TOGGLE_PARTICIPATION, game.id))
    rows += [
        button(texts.btn_anon_chat(game.anon_chat), Action.TOGGLE_ANON_CHAT, game.id),
        button(texts.btn_reminder(game.reminder_on), Action.TOGGLE_REMINDER, game.id),
        button(texts.BTN_CANCEL_GAME, Action.CANCEL_GAME, game.id),
        panel_button(game.id),
    ]
    return OutMessage(_with_notice(notice, texts.SETTINGS_TITLE), kb.keyboard(*rows))


def confirm_cancel_game(game: Game) -> OutMessage:
    return OutMessage(
        texts.confirm_cancel_game(game.title),
        kb.keyboard([button(texts.BTN_CONFIRM_CANCEL_GAME, Action.CANCEL_GAME_CONFIRM, game.id),
                     button(texts.BTN_CANCEL, Action.SETTINGS, game.id)]),
    )


def choose_tier(game: Game, options: Sequence[Upgrade]) -> OutMessage:
    buttons = [button(texts.btn_tier(option.limit, option.amount), Action.PAY, game.code, option.tier)
               for option in options]
    return OutMessage(texts.CHOOSE_TIER, kb.keyboard(*buttons))


def pay_offer(game: Game, amount: int, limit: int, url: str) -> OutMessage:
    return OutMessage(
        texts.pay_offer(amount=amount, title=game.title, limit=limit),
        kb.keyboard(kb.link(texts.btn_pay_link(amount), url)),
    )


def wish_reminder(game: Game) -> OutMessage:
    return OutMessage(
        texts.wish_reminder(game.title),
        kb.keyboard([button(texts.BTN_WRITE_WISHES, Action.WISHES, game.id), surprise_button(game.id)]),
    )


# --- §5.5 draw results -------------------------------------------------------------------------------


def draw_result(game: Game, receiver: Participant, *, redraw: bool = False) -> OutMessage:
    text = texts.draw_result(title=game.title, receiver=receiver.display_name, wishes=receiver.wishes,
                             budget=game.budget_text, exchange_date=game.exchange_date, redraw=redraw)
    return OutMessage(text, kb.keyboard(relay_buttons(game), ref_button(game)))


def whom_do_i_gift(game: Game, receiver: Participant) -> OutMessage:
    text = texts.whom_do_i_gift(title=game.title, receiver=receiver.display_name, wishes=receiver.wishes)
    rows: list[Sequence[Button] | Button] = []
    if game.status == GameStatus.DRAWN:
        rows.append(relay_buttons(game))
    return OutMessage(text, kb.keyboard(*rows) if rows else None)


# --- §5.7 anonymous messages -----------------------------------------------------------------------


def relay_delivery(game: Game, relay: Relay, sender_name: str) -> OutMessage:
    """What the recipient sees. To a receiver the Santa is anonymous: no name, no id."""
    if relay.direction == RelayDirection.TO_RECEIVER:
        text = texts.relay_from_santa(title=game.title, text=relay.text)
        reply = texts.BTN_REPLY_TO_SANTA
    else:
        text = texts.relay_from_receiver(receiver=sender_name, text=relay.text)
        reply = texts.BTN_REPLY_ANONYMOUSLY
    return OutMessage(
        text,
        kb.keyboard([button(reply, Action.REPLY, relay.id), button(texts.BTN_REPORT, Action.REPORT, relay.id)]),
    )


def report_to_admins(report: Report, code: str | None) -> OutMessage:
    return OutMessage(
        texts.report_to_admin(report_id=report.id, code=code, reporter_id=report.reporter_id,
                              reported_id=report.reported_id, text=report.text),
        kb.keyboard([button(texts.BTN_BLOCK_SENDER, Action.BLOCK_REPORTED, report.id),
                     button(texts.BTN_CLOSE_REPORT, Action.CLOSE_REPORT, report.id)]),
    )
