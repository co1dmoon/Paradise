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
from app.core.games import MAX_REDRAWS, GameCounts
from app.core.kb import Button
from app.core.models import (
    Game,
    GameStatus,
    Participant,
    ParticipantStatus,
    Payment,
    Relay,
    RelayDirection,
    Report,
    Settings,
    Tier,
)
from app.core.payloads import deep_link, group_payload, join_payload, new_game_payload
from app.core.pricing import PAID_TIERS, Upgrade
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
    GIFT_READY = "gr"
    REDRAW = "rd"
    REDRAW_CONFIRM = "rdy"
    REVEAL = "rv"
    REVEAL_CONFIRM = "rvy"
    UNBLOCK = "ub"
    ADMIN_GRANT = "ag"
    ADMIN_CANCEL = "ac"
    ADMIN_CANCEL_CONFIRM = "acy"
    ADMIN_FORGET_CONFIRM = "afy"
    ADMIN_CLEAR_REPORTED = "acr"
    ADMIN_RESET_TITLE = "art"
    PROMO_APPROVE = "pa"
    PROMO_DECLINE = "pd"
    PROMO_STOP_REMOVED = "px"


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


def gift_ready_button(game_id: int) -> kb.CallbackButton:
    return button(texts.BTN_GIFT_READY, Action.GIFT_READY, game_id)


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
    if game.group_chat_id is not None:
        text = texts.game_created_in_group(participates=game.organizer_participates)
    elif game.organizer_participates:
        text = texts.GAME_CREATED
    else:
        text = texts.GAME_CREATED_NOT_PARTICIPATING
    buttons = [button(texts.BTN_MY_WISHES, Action.WISHES, game.id)] if game.organizer_participates else []
    return OutMessage(text, kb.keyboard([*buttons, panel_button(game.id)]))


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


def waiting(game: Game, upgrade: Upgrade | None) -> OutMessage:
    """§5.6: the queue message; ``upgrade`` is None when nobody can pay (top tier, payments off)."""
    if upgrade is None:
        return OutMessage(texts.WAITING_LIST_FULL)
    text = texts.waiting_list(free=game.tier == Tier.FREE, current_limit=game.participant_limit,
                              limit=upgrade.limit, price=upgrade.amount)
    return OutMessage(text, kb.keyboard(pay_button(game, upgrade)))


def spot_opened(game: Game) -> OutMessage:
    """A waiting person got a place (after a payment or when someone left)."""
    return OutMessage(
        texts.spot_opened(game.title),
        kb.keyboard([button(texts.BTN_WRITE_WISHES, Action.WISHES, game.id), surprise_button(game.id)]),
    )


def confirm_leave(game: Game, me: Participant) -> OutMessage:
    """Leaving an active place after the draw re-links the pairs, so the text says so."""
    splices = game.status == GameStatus.DRAWN and me.status == ParticipantStatus.ACTIVE
    return OutMessage(
        texts.confirm_leave_after_draw(game.title) if splices else texts.confirm_leave(game.title),
        kb.keyboard([button(texts.BTN_CONFIRM_LEAVE, Action.LEAVE_CONFIRM, game.id),
                     button(texts.BTN_CANCEL, Action.OPEN_GAME, game.id)]),
    )


# --- §5.8 my games and the participant's view ------------------------------------------------------


def my_games(user_id: int, entries: Sequence[tuple[Game, Participant | None]], page: int) -> OutMessage:
    if not entries:
        return menu(texts.NO_GAMES)
    current = kb.paginate(entries, page)
    first = current.number * kb.PAGE_SIZE + 1  # numbers tie each line to its button: titles repeat
    numbered = list(enumerate(current.items, start=first))
    lines = [
        f"{number}. " + texts.my_game_line(title=game.title, status=game.status, role=_role(user_id, game, participant))
        for number, (game, participant) in numbered
    ]
    buttons = [button(f"{number}. {game.title}", Action.OPEN_GAME, game.id) for number, (game, _) in numbered]
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
            rows.append(relay_buttons(game))
            ready = [] if me.gift_ready else [gift_ready_button(game.id)]
            rows.append([button(texts.BTN_EDIT_WISHES, Action.WISHES, game.id), *ready,
                         button(texts.BTN_LEAVE, Action.LEAVE, game.id)])
    elif game.status == GameStatus.DRAWN and me.status == ParticipantStatus.WAITING:
        rows.append(button(texts.BTN_LEAVE, Action.LEAVE, game.id))
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


def panel(game: Game, counts: GameCounts, upgrade: Upgrade | None, *, can_reveal: bool = False) -> OutMessage:
    """The organizer's panel. ``upgrade`` is the next paid tier, or None (top tier or payments off);
    ``can_reveal`` adds [Раскрыть, кто чей Санта] (§5.11)."""
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
                                 gifts_ready=counts.gifts_ready, budget=game.budget_text,
                                 exchange_date=game.exchange_date)
        if game.status == GameStatus.CANCELLED:
            text = f"{text}\n{texts.GAME_CANCELLED}"
        rows = [button(texts.BTN_PARTICIPANTS, Action.PARTICIPANTS, game.id)]
        if game.status in (GameStatus.DRAWN, GameStatus.FINISHED):
            rows.append(button(texts.BTN_NOT_RECEIVED, Action.NOT_RECEIVED, game.id))
        if game.status == GameStatus.DRAWN:
            rows.append([button(texts.BTN_REMIND_WISHES, Action.REMIND, game.id),
                         button(texts.BTN_SETTINGS, Action.SETTINGS, game.id)])
        if can_reveal:
            rows.append(button(texts.BTN_REVEAL, Action.REVEAL, game.id))
        if game.status == GameStatus.DRAWN and game.redraw_count < MAX_REDRAWS:
            rows.append(button(texts.BTN_REDRAW, Action.REDRAW, game.id))
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
    if game.status in (GameStatus.COLLECTING, GameStatus.DRAWN) and removable:
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
    drawn = game.status == GameStatus.DRAWN and person.status == ParticipantStatus.ACTIVE
    return OutMessage(
        texts.confirm_remove_after_draw(person.display_name) if drawn else texts.confirm_remove(person.display_name),
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


# --- §5.9 notices the scheduler sends to organizers and participants --------------------------------


def join_notice(game: Game, names: Sequence[str], active: int) -> OutMessage:
    """§5.4: who joined since the last notice (at most one per 10 minutes)."""
    text = texts.join_notice(title=game.title, names=names, active=active, limit=game.participant_limit)
    return OutMessage(text, kb.keyboard(panel_button(game.id)))


def waiting_notice(
    game: Game, waiting_count: int, upgrade: Upgrade | None, *, contact_email: str | None = None
) -> OutMessage:
    """§5.6: people are queued (at most one per hour); ``upgrade`` is None when nobody can pay.

    ``contact_email`` is set at the top tier, where a bigger game is arranged by e-mail (§4).
    """
    if upgrade is None:
        text = texts.waiting_notice_full(title=game.title, waiting=waiting_count)
        if contact_email is not None:
            contact = texts.upgrade_contact_us(max_limit=game.participant_limit, support_email=contact_email)
            text = f"{text} {contact}"
        return OutMessage(text, kb.keyboard(panel_button(game.id)))
    text = texts.waiting_notice(title=game.title, waiting=waiting_count, limit=upgrade.limit, price=upgrade.amount)
    return OutMessage(text, kb.keyboard(upgrade_button(game.id)))


def organizer_nudge(game: Game, days: int, active: int) -> OutMessage:
    """§5.9 b: the exchange is close and there was no draw yet."""
    return OutMessage(texts.organizer_nudge(title=game.title, days=days, active=active),
                      kb.keyboard(draw_button(game.id)))


def pre_exchange_reminder(game: Game, receiver: Participant) -> OutMessage:
    """§5.9 c: the day before the exchange, at 12:00."""
    return OutMessage(texts.pre_exchange_reminder(title=game.title, receiver=receiver.display_name),
                      kb.keyboard(whom_button(game.id)))


def wish_reminder(game: Game) -> OutMessage:
    return OutMessage(
        texts.wish_reminder(game.title),
        kb.keyboard([button(texts.BTN_WRITE_WISHES, Action.WISHES, game.id), surprise_button(game.id)]),
    )


# --- §5.5 draw results -------------------------------------------------------------------------------


def draw_result(game: Game, receiver: Participant, *, redraw: bool = False) -> OutMessage:
    text = texts.draw_result(title=game.title, receiver=receiver.display_name, wishes=receiver.wishes,
                             budget=game.budget_text, exchange_date=game.exchange_date, anon_chat=game.anon_chat,
                             redraw=redraw)
    return OutMessage(text, kb.keyboard(relay_buttons(game), gift_ready_button(game.id), ref_button(game)))


def confirm_redraw(game: Game) -> OutMessage:
    return OutMessage(
        texts.confirm_redraw(title=game.title, left=MAX_REDRAWS - game.redraw_count),
        kb.keyboard([button(texts.BTN_CONFIRM_REDRAW, Action.REDRAW_CONFIRM, game.id, game.redraw_count),
                     button(texts.BTN_CANCEL, Action.PANEL, game.id)]),
    )


def receiver_left(game: Game, receiver: Participant) -> OutMessage:
    """P1: the giver's receiver left after the draw; the giver now gifts the leaver's receiver."""
    return OutMessage(texts.receiver_left(receiver=receiver.display_name, wishes=receiver.wishes,
                                          anon_chat=game.anon_chat),
                      kb.keyboard(relay_buttons(game)))


def splice_problem(game: Game, *, too_few: bool) -> OutMessage:
    """After a splice the pairs need the organizer: a redraw, or (when none is possible) another way out."""
    if too_few:
        text = texts.splice_too_few(game.title)
    elif game.redraw_count >= MAX_REDRAWS:
        text = texts.splice_no_redraws_left(game.title)
    else:
        text = texts.splice_needs_redraw(game.title)
    return OutMessage(text, kb.keyboard(panel_button(game.id)))


# --- §5.11 reveal ---------------------------------------------------------------------------------------


def confirm_reveal(game: Game) -> OutMessage:
    return OutMessage(
        texts.confirm_reveal(game.title),
        kb.keyboard([button(texts.BTN_CONFIRM_REVEAL, Action.REVEAL_CONFIRM, game.id),
                     button(texts.BTN_CANCEL, Action.PANEL, game.id)]),
    )


def reveal(game: Game, chain: Sequence[tuple[str, str]]) -> OutMessage:
    """Link mode: every participant gets the chain with the viral button."""
    return OutMessage(texts.reveal_chain(title=game.title, pairs=chain), kb.keyboard(ref_button(game)))


def reveal_in_group(config: Config, game: Game, chain: Sequence[tuple[str, str]]) -> OutMessage:
    """Group mode: the chain in the chat, followed by a link to start a game elsewhere."""
    footer = texts.reveal_group_footer(deep_link(config.max_bot_username, new_game_payload(game.code)))
    return OutMessage(texts.reveal_chain(title=game.title, pairs=chain, footer=footer))


# --- §5.10 group mode ------------------------------------------------------------------------------------


def group_hello(config: Config, chat_id: int) -> OutMessage:
    link = deep_link(config.max_bot_username, group_payload(chat_id))
    return OutMessage(texts.GROUP_HELLO, kb.keyboard(kb.link(texts.BTN_CREATE_IN_CHAT, link)))


def group_card(config: Config, game: Game, names: Sequence[str]) -> OutMessage:
    """The live card in the group chat. Only a LINK button: strangers never trigger bot actions."""
    if game.status == GameStatus.COLLECTING:
        text = texts.group_card(title=game.title, budget=game.budget_text, exchange_date=game.exchange_date,
                                names=names, limit=game.participant_limit)
        return OutMessage(text, kb.keyboard(kb.link(texts.BTN_JOIN_GROUP_GAME, invite_link(config, game))))
    if game.status == GameStatus.CANCELLED:
        return OutMessage(texts.game_cancelled(game.title))
    return OutMessage(texts.GROUP_CARD_DRAWN)


def whom_do_i_gift(game: Game, receiver: Participant) -> OutMessage:
    text = texts.whom_do_i_gift(title=game.title, receiver=receiver.display_name, wishes=receiver.wishes,
                                anon_chat=game.anon_chat)
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
                     button(texts.BTN_CLOSE_REPORT, Action.CLOSE_REPORT, report.id)],
                    button(texts.BTN_CLEAR_REPORTED, Action.ADMIN_CLEAR_REPORTED, report.id)),
    )


# --- §9 admin -----------------------------------------------------------------------------------------


def unblock_button(user_id: int) -> kb.CallbackButton:
    return button(texts.BTN_UNBLOCK, Action.UNBLOCK, user_id)


def admin_game(game: Game, counts: GameCounts, payments: Sequence[Payment]) -> OutMessage:
    """/game CODE: the summary (never wishes or messages) with [Выдать S/M/L] [Отменить игру]
    and [Сбросить название] for a title that must go (moderation, §11)."""
    text = texts.admin_game_summary(
        code=game.code, title=game.title, status=game.status, tier=game.tier, active=counts.active,
        limit=game.participant_limit, waiting=counts.waiting, organizer_id=game.organizer_id,
        created_at=game.created_at,
        payments=[(p.inv_id, p.tier, p.amount_rub, p.status) for p in payments],
    )
    rows: list[Sequence[Button] | Button] = []
    if game.status == GameStatus.COLLECTING:
        rows.append([button(texts.btn_grant(tier), Action.ADMIN_GRANT, game.id, tier) for tier in PAID_TIERS])
    if game.status in (GameStatus.COLLECTING, GameStatus.DRAWN):
        rows.append(button(texts.BTN_ADMIN_CANCEL_GAME, Action.ADMIN_CANCEL, game.id))
    if game.title != texts.DEFAULT_TITLE:
        rows.append(button(texts.BTN_RESET_TITLE, Action.ADMIN_RESET_TITLE, game.id))
    return OutMessage(text, kb.keyboard(*rows) if rows else None)


def confirm_forget(user_id: int) -> OutMessage:
    return OutMessage(texts.confirm_forget(user_id),
                      kb.keyboard(button(texts.BTN_CONFIRM_FORGET, Action.ADMIN_FORGET_CONFIRM, user_id)))


def confirm_admin_cancel(game: Game) -> OutMessage:
    return OutMessage(
        texts.confirm_admin_cancel(code=game.code, title=game.title),
        kb.keyboard(button(texts.BTN_CONFIRM_CANCEL_GAME, Action.ADMIN_CANCEL_CONFIRM, game.id)),
    )


# --- PROMO_SPEC §6: a budget raise waiting for an admin's tap ------------------------------------------------


def promo_proposal(action_id: int, text: str, to_kop: int) -> OutMessage:
    """[Поднять до X ₽] [Не надо]; the handlers re-check everything in the database."""
    return OutMessage(text, kb.keyboard([button(texts.btn_promo_raise(to_kop), Action.PROMO_APPROVE, action_id),
                                         button(texts.BTN_PROMO_DECLINE, Action.PROMO_DECLINE, action_id)]))


def promo_budget_confirm(action_id: int, text: str, to_kop: int, kind: str) -> OutMessage:
    """A large /ads budget change waits for [Да, X ₽ в неделю] [Не надо] (the same checks as a raise)."""
    confirm = texts.btn_promo_budget_confirm(to_kop=to_kop, kind=kind)
    return OutMessage(text, kb.keyboard([button(confirm, Action.PROMO_APPROVE, action_id),
                                         button(texts.BTN_PROMO_DECLINE, Action.PROMO_DECLINE, action_id)]))


def promo_removed(src: str, text: str) -> OutMessage:
    """/ads remove of a campaign that may still run: [Остановить на площадке]."""
    return OutMessage(text, kb.keyboard(button(texts.BTN_PROMO_STOP_REMOVED, Action.PROMO_STOP_REMOVED, src)))
