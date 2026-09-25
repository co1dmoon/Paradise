"""ALL Russian copy of the bot (§5, §9). The owner edits wording here only.

Rules (§11): Russian only, no English loanwords ('пожелания', not a loanword);
never use the words for a lottery, contest or prize — say 'обмен подарками' and
'жеребьёвка пар'. Messages are plain text (no markup) and at most 4000 characters;
lists are cut with 'и ещё N' by ``fit_lines``.

Constants are button labels and fixed messages; functions fill in values.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date
from typing import TYPE_CHECKING

from app.core.dates import DateError, format_date
from app.core.inputs import shorten

if TYPE_CHECKING:
    from app.core.analytics import StatsBlock, StatsReport

MAX_MESSAGE = 4000

# --- helpers -------------------------------------------------------------------------


def fit_lines(lines: Sequence[str], *, header: str = "", footer: str = "", limit: int = MAX_MESSAGE) -> str:
    """Join lines under ``header`` and above ``footer``; if too long, cut and end with 'и ещё N'."""

    def compose(shown: Sequence[str], hidden: int) -> str:
        body = list(shown) + ([f"и ещё {hidden}"] if hidden else [])
        return "\n".join(part for part in (header, "\n".join(body), footer) if part)

    for count in range(len(lines), -1, -1):
        text = compose(lines[:count], len(lines) - count)
        if len(text) <= limit:
            return text
    return compose([], len(lines))[:limit]


def date_text(value: date | None) -> str:
    return format_date(value) if value else "дату сообщит организатор"


def names_preview(names: Sequence[str], limit: int = 15) -> str:
    shown = ", ".join(names[:limit])
    return f"{shown}…" if len(names) > limit else shown


def plural(n: int, one: str, few: str, many: str) -> str:
    """Russian plural form: 1 жеребьёвка, 2 жеребьёвки, 5 жеребьёвок."""
    if n % 10 == 1 and n % 100 != 11:
        return one
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return few
    return many


def _q(text: str) -> str:
    return f"«{text}»"


# --- common buttons ---------------------------------------------------------------------

BTN_CANCEL = "Отмена"
BTN_BACK = "Назад"
BTN_NEXT_PAGE = "Дальше"
BTN_PREVIOUS_PAGE = "Назад"
BTN_MENU = "Меню"

# --- §5.1 consent and menu -----------------------------------------------------------------

BTN_CONSENT = "Согласен(на)"


def consent(base_url: str) -> str:
    return (
        "Привет! Я помогу провести «Тайного Санту» — обмен подарками в семье, на работе или в классе.\n"
        "Чтобы участвовать, нужно согласие на обработку данных: имени из MAX и ваших пожеланий к подарку. "
        f"Подробно: {base_url}/consent · Политика: {base_url}/privacy · Правила: {base_url}/terms"
    )


CONSENT_THANKS = "Спасибо! Начинаем."
MENU = "Что сделаем?"
BTN_CREATE_GAME = "Создать игру"
BTN_MY_GAMES = "Мои игры"
BTN_JOIN_BY_CODE = "Вступить по коду"
BTN_HOW_IT_WORKS = "Как это работает"


def whoami(user_id: int) -> str:
    return f"Ваш id в MAX: {user_id}"


COMMAND_MENU: tuple[tuple[str, str], ...] = (
    ("start", "Главное меню"),
    ("help", "Как это работает"),
    ("cancel", "Отменить ввод"),
    ("whoami", "Мой id в MAX"),
)
INPUT_CANCELLED = "Хорошо, отменил."
NOTHING_TO_CANCEL = "Отменять нечего. Открыть меню — /start."
UNKNOWN_INPUT = "Я понимаю кнопки и код игры из 6 символов. Открыть меню — /start."
TEXT_ONLY = "Пожалуйста, напишите текстом — картинки и файлы я не передаю."
BUTTON_OUTDATED = "Эта кнопка устарела. Откройте «Мои игры»."
NOT_ALLOWED = "Это действие вам недоступно."
MAINTENANCE = "Идут технические работы. Попробуйте через 15 минут."
SOMETHING_WENT_WRONG = "Что-то пошло не так. Попробуйте ещё раз чуть позже."
GAME_FINISHED = "Эта игра уже завершена."
ALREADY_DRAWN_ACTION = "Жеребьёвка в этой игре уже проведена — это действие больше недоступно."
NOT_DRAWN_YET = "Жеребьёвки ещё не было — пара появится после неё."
NOT_IN_GAME = "Вы больше не участвуете в этой игре."


def blocked(support_email: str) -> str:
    return f"Доступ ограничен из-за жалоб. Вопросы: {support_email}."


# --- §5.2 creating a game ------------------------------------------------------------------

DEFAULT_TITLE = "Тайный Санта"
ASK_TITLE = "Как назовём игру? Например: «Отдел продаж», «Семья Ивановых», «5Б класс»."
BTN_SKIP = "Пропустить"


def title_too_long(limit: int) -> str:
    return f"Слишком длинно — уложитесь в {limit} символов."


ASK_BUDGET = "Бюджет подарка?"
BUDGET_PRESETS: tuple[str, ...] = ("до 500 ₽", "до 1000 ₽", "до 1500 ₽", "до 3000 ₽", "Без ограничений")
BTN_CUSTOM_BUDGET = "Свой вариант"
ASK_CUSTOM_BUDGET = "Напишите бюджет, например «до 2000 ₽»."


def budget_too_long(limit: int) -> str:
    return f"Слишком длинно — уложитесь в {limit} символов, например «до 2000 ₽»."


ASK_DATE = "Когда обмен подарками?"
BTN_CUSTOM_DATE = "Своя дата"
BTN_DATE_UNKNOWN = "Пока не знаю"
ASK_CUSTOM_DATE = "Напишите дату в формате ДД.ММ, например 25.12."
DATE_ERRORS: dict[DateError, str] = {
    DateError.FORMAT: "Не понял дату. Напишите в формате ДД.ММ, например 25.12.",
    DateError.TOO_EARLY: "Дата должна быть не раньше завтрашнего дня.",
    DateError.TOO_LATE: "Дата должна быть не позже чем через 180 дней.",
}

ASK_PARTICIPATES = "Вы тоже участвуете в обмене?"
BTN_I_PARTICIPATE = "Да, участвую"
BTN_ONLY_ORGANIZE = "Нет, только организую"


def invite(*, title: str, budget: str, exchange_date: date | None, link: str, code: str,
           participants: Sequence[str] = ()) -> str:
    """The forwardable invite (§5.2); with ``participants`` it shows live progress (§5.4)."""
    lines = [
        f"Тайный Санта {_q(title)}",
        f"Бюджет: {budget}. Обмен подарками: {date_text(exchange_date)}.",
    ]
    if participants:
        lines.append(f"Уже участвуют: {len(participants)} — {names_preview(participants)}")
    lines += [
        "Участвовать: нажмите ссылку, затем «Начать»",
        link,
        f"Если бот у вас уже открыт — просто отправьте ему код: {code}",
    ]
    return "\n".join(lines)


GAME_CREATED = (
    "Готово! Перешлите приглашение выше в общий чат (нажмите на сообщение → «Переслать») "
    "или скопируйте ссылку. Когда кто-то вступит — я напишу. Если вы участвуете — напишите свои пожелания."
)
BTN_MY_WISHES = "Мои пожелания"
BTN_PANEL = "Пульт игры"
DAILY_LIMIT = "Сегодня вы уже создали 20 игр — это максимум на день. Попробуйте завтра."
GAME_CREATED_NOT_PARTICIPATING = (
    "Готово! Перешлите приглашение выше в общий чат (нажмите на сообщение → «Переслать») "
    "или скопируйте ссылку. Когда кто-то вступит — я напишу."
)


def game_created_in_group(*, participates: bool) -> str:
    """§5.10: the game card is already in the group chat, nothing to forward."""
    text = "Готово! Карточка игры уже в чате: участники нажимают «Участвую». Когда кто-то вступит — я напишу."
    return text + (" Напишите свои пожелания." if participates else "")

# --- §5.3 joining ------------------------------------------------------------------------------

ASK_CODE = "Отправьте код игры — 6 символов из приглашения."
GAME_NOT_FOUND = "Игра с таким кодом не найдена. Проверьте код в приглашении."
GAME_CANCELLED = "Эта игра отменена организатором."
ALREADY_DRAWN = "Жеребьёвка в этой игре уже прошла — вступить нельзя. Можно устроить свою игру."
REMOVED_CANNOT_JOIN = "Организатор убрал вас из этой игры, поэтому вступить снова нельзя."


def joined(*, title: str, organizer: str, budget: str, exchange_date: date | None, name: str) -> str:
    return (
        f"Вы в игре {_q(title)}! Организатор: {organizer}. Бюджет: {budget}. "
        f"Обмен: {date_text(exchange_date)}.\n"
        f"Как вас подписать в игре? Сейчас: {_q(name)}. "
        "(Если играет ребёнок без MAX — вступите сами и укажите, например: «Маша, 5Б».)"
    )


BTN_KEEP_NAME = "Оставить так"
BTN_CHANGE_NAME = "Изменить имя"
ASK_NAME = "Напишите, как вас подписать в игре."


def name_too_long(limit: int) -> str:
    return f"Имя должно быть до {limit} символов."


def name_saved(name: str) -> str:
    return f"Записал: {_q(name)}."


ASK_WISHES = "Напишите, что хотели бы получить: 2–5 идей, можно ссылки на товары. Или нажмите «Удивите меня»."


def current_wishes(wishes: str) -> str:
    return f"Сейчас: {wishes}"
BTN_SURPRISE_ME = "Удивите меня"
SURPRISE_WISHES = "Удивите меня!"


def wishes_too_long(limit: int) -> str:
    return f"Пожелания должны быть до {limit} символов. Сократите, пожалуйста."


WISHES_SAVED = "Записал! Когда организатор проведёт жеребьёвку, я пришлю, кому вы дарите."
WISHES_UPDATED = "Пожелания обновлены. Ваш Санта увидит новую версию."
BTN_EDIT_WISHES = "Изменить пожелания"
BTN_LEAVE = "Выйти из игры"
BTN_NEW_GAME_ELSEWHERE = "Устроить игру в другом чате"


def confirm_leave(title: str) -> str:
    return f"Выйти из игры {_q(title)}?"


BTN_CONFIRM_LEAVE = "Да, выйти"


def confirm_leave_after_draw(title: str) -> str:
    return (
        f"Выйти из игры {_q(title)}? Жеребьёвка уже прошла: я перестрою пары, "
        "и ваш Тайный Санта будет дарить другому участнику."
    )



def left_game(title: str) -> str:
    return f"Вы вышли из игры {_q(title)}."


# --- §5.4 organizer panel -------------------------------------------------------------------


def panel(*, title: str, code: str, active: int, limit: int, waiting: int, without_wishes: int,
          exclusions: int, budget: str, exchange_date: date | None) -> str:
    queue = f"; в очереди: {waiting}" if waiting else ""
    return (
        f"Игра {_q(title)} · код {code}\n"
        f"Участников: {active} из {limit}{queue}\n"
        f"Без пожеланий: {without_wishes} · Исключений: {exclusions}\n"
        f"Бюджет: {budget} · Обмен: {date_text(exchange_date)}"
    )


def panel_drawn(*, title: str, code: str, active: int, gifts_ready: int, budget: str,
                exchange_date: date | None) -> str:
    return (
        f"Игра {_q(title)} · код {code}\n"
        f"Жеребьёвка проведена, участников: {active}. Подарки готовы: {gifts_ready} из {active}.\n"
        f"Бюджет: {budget} · Обмен: {date_text(exchange_date)}"
    )


BTN_PARTICIPANTS = "Участники"
BTN_EXCLUSIONS = "Исключения"
BTN_REMIND_WISHES = "Напомнить о пожеланиях"
BTN_DRAW = "Провести жеребьёвку"
BTN_INVITE = "Приглашение"
BTN_SETTINGS = "Настройки"
BTN_UPGRADE = "Расширить игру"
BTN_NOT_RECEIVED = "Кто не получил пару"
BTN_REVEAL = "Раскрыть, кто чей Санта"
BTN_REDRAW = "Перезапустить жеребьёвку"


def participants_list(entries: Sequence[tuple[str, bool]], waiting: Sequence[str] = ()) -> str:
    """Numbered names with 'пожелания есть/нет'; the queue is listed after them."""
    lines = [
        f"{number}. {name} — пожелания {'есть' if has_wishes else 'нет'}"
        for number, (name, has_wishes) in enumerate(entries, start=1)
    ]
    lines += [f"В очереди: {name}" for name in waiting]
    return fit_lines(lines, header=f"Участники ({len(entries)}):")


BTN_REMOVE_PARTICIPANT = "Убрать участника"
PICK_PARTICIPANT_TO_REMOVE = "Кого убрать из игры?"


def confirm_remove(name: str) -> str:
    return f"Убрать {_q(name)} из игры?"


def confirm_remove_after_draw(name: str) -> str:
    return f"Убрать {_q(name)} из игры? Жеребьёвка уже прошла: его или её Санте я назначу другого получателя."


BTN_CONFIRM_REMOVE = "Да, убрать"


def participant_removed(name: str) -> str:
    return f"{name} больше не в игре."


PARTICIPANT_GONE = "Этого человека уже нет в игре."
NOBODY_TO_REMOVE = "Убирать некого: кроме вас, в игре никого нет."


def removed_notice(title: str) -> str:
    return f"Организатор убрал вас из игры {_q(title)}."


EXCLUSIONS_PROMPT = "Кто не должен дарить друг другу (например, супруги)? Выберите первого человека."


def pick_second_excluded(first: str) -> str:
    return f"Первый в паре: {first}. Выберите второго человека."


def exclusions_list(pairs: Sequence[tuple[str, str]]) -> str:
    if not pairs:
        return "Исключений пока нет."
    lines = [f"{number}. {a} и {b}" for number, (a, b) in enumerate(pairs, start=1)]
    return fit_lines(lines, header="Эти пары не будут дарить друг другу:")


def exclusion_saved(a: str, b: str) -> str:
    return f"Записал: {a} и {b} не будут дарить друг другу."


def btn_remove_pair(number: int) -> str:
    return f"Убрать пару {number}"


BTN_ADD_PAIR = "Добавить пару"
EXCLUSION_EXISTS = "Такая пара уже есть."
EXCLUSION_SAME_PERSON = "Выберите двух разных людей."
EXCLUSION_NOT_PARTICIPANT = "Этого человека уже нет в игре."
EXCLUSIONS_LIMIT = "Не больше 50 пар исключений."
EXCLUSION_REMOVED = "Пара убрана."
NEED_TWO_FOR_EXCLUSION = "Для исключений нужно хотя бы два участника."
EXCLUSIONS_HINT = "Нажмите «Добавить пару», чтобы два человека не дарили друг другу."


def wish_reminder(title: str) -> str:
    return f"Организатор игры {_q(title)} просит написать пожелания к подарку — так вашему Санте будет проще."


BTN_WRITE_WISHES = "Написать пожелания"


def reminded(count: int) -> str:
    return f"Напомнил {count} чел."


REMINDER_TOO_SOON = "Напоминать можно раз в сутки. Попробуйте позже."
NOBODY_TO_REMIND = "Все участники уже написали пожелания."

SETTINGS_TITLE = "Настройки игры. Что поменять?"
BTN_SET_TITLE = "Название"
BTN_SET_BUDGET = "Бюджет"
BTN_SET_DATE = "Дата обмена"


def btn_participation(participates: bool) -> str:
    return f"Участвую: {'да' if participates else 'нет'}"


def btn_anon_chat(enabled: bool) -> str:
    return f"Анонимные вопросы: {'вкл' if enabled else 'выкл'}"


def btn_reminder(enabled: bool) -> str:
    return f"Напоминание за день: {'вкл' if enabled else 'выкл'}"


BTN_CANCEL_GAME = "Отменить игру"
SETTINGS_SAVED = "Сохранил."
PARTICIPATION_ONLY_BEFORE_DRAW = "Участие организатора можно менять только до жеребьёвки."


def confirm_cancel_game(title: str) -> str:
    return f"Отменить игру {_q(title)}? Участники получат уведомление. Это нельзя вернуть."


BTN_CONFIRM_CANCEL_GAME = "Да, отменить игру"


def game_cancelled(title: str) -> str:
    return f"Игра {_q(title)} отменена организатором."


def game_cancelled_by_you(title: str) -> str:
    return f"Игра {_q(title)} отменена. Участники получили уведомление."


def join_notice(*, title: str, names: Sequence[str], active: int, limit: int) -> str:
    return f"В игру {_q(title)} вступили: {names_preview(names)}. Всего: {active} из {limit}."


# --- §5.5 draw ---------------------------------------------------------------------------------

DRAW_NEEDS_THREE = "Для жеребьёвки нужно минимум 3 участника."


def draw_confirm(*, active: int, without_wishes: int, waiting: int) -> str:
    people = plural(active, "участника", "участников", "участников")
    text = f"Провести жеребьёвку для {active} {people}? После этого вступить будет нельзя."
    if without_wishes:
        text += f" У {without_wishes} {plural(without_wishes, 'человека', 'человек', 'человек')} нет пожеланий."
    if waiting:
        queued = plural(waiting, "человек", "человека", "человек")
        outcome = "он не попадёт" if waiting == 1 else "они не попадут"
        text += f" В очереди {waiting} {queued} — {outcome} в игру."
    return text


BTN_CONFIRM_DRAW = "Да, провести"
DRAW_IMPOSSIBLE = "Не получается учесть все исключения — уберите часть пар."
DRAW_STARTED = "Жеребьёвка проведена! Рассылаю пары участникам — сообщу, когда всё дойдёт."
REDRAW_PREFIX = "Жеребьёвка проведена заново — старую пару не учитывайте."
REDRAW_LIMIT = "Перезапускать жеребьёвку можно не больше 2 раз."
REDRAW_ALREADY_DONE = "Жеребьёвку по этому подтверждению уже перезапустили — новые пары разосланы."


def confirm_redraw(*, title: str, left: int) -> str:
    times = "раз" if left == 1 else "раза"
    return (
        f"Перезапустить жеребьёвку в игре {_q(title)}? Все получат новые пары, старые перестанут действовать. "
        f"Перезапустить можно ещё {left} {times}."
    )


BTN_CONFIRM_REDRAW = "Да, перезапустить"
REDRAW_STARTED = "Жеребьёвка проведена заново! Рассылаю новые пары — сообщу, когда всё дойдёт."
REDRAW_IMPOSSIBLE = "Не получается составить новые пары с учётом исключений."


def draw_result(*, title: str, receiver: str, wishes: str | None, budget: str,
                exchange_date: date | None, anon_chat: bool = True, redraw: bool = False) -> str:
    text = (
        f"Жеребьёвка в игре {_q(title)} проведена!\n"
        f"Вы — Тайный Санта для: {receiver.rstrip('.')}.\n"
        f"Пожелания: {_wishes(wishes, anon_chat)}\n"
        f"Бюджет: {budget}. Обмен: {date_text(exchange_date)}.\n"
        "Никому не говорите, кто вам выпал."
    )
    return f"{REDRAW_PREFIX}\n{text}" if redraw else text


BTN_ASK_RECEIVER = "Спросить получателя анонимно"
BTN_WRITE_SANTA = "Написать своему Санте"
BTN_GIFT_READY = "Подарок готов"
GIFT_READY_SAVED = "Отметил: подарок готов."


def draw_summary(*, sent: int, total: int, failed_names: Sequence[str]) -> str:
    text = f"Готово! Пары отправлены {sent} из {total}."
    if failed_names:
        text += (
            f" Не дошло: {names_preview(failed_names)} — попросите их открыть бота и нажать «Мои игры», "
            "там будет их пара."
        )
    return text


BTN_WHOM_DO_I_GIFT = "Кому я дарю?"


def whom_do_i_gift(*, title: str, receiver: str, wishes: str | None, anon_chat: bool = True) -> str:
    return f"Игра {_q(title)}. Вы дарите: {receiver}.\nПожелания: {_wishes(wishes, anon_chat)}"


def not_received(names: Sequence[str]) -> str:
    if not names:
        return "Пары дошли до всех участников."
    return fit_lines(
        list(names),
        header="Не получили пару (попросите их открыть бота и нажать «Мои игры»):",
    )


def receiver_left(*, receiver: str, wishes: str | None, anon_chat: bool = True) -> str:
    return f"Ваш получатель выбыл. Теперь вы дарите: {receiver}.\nПожелания: {_wishes(wishes, anon_chat)}"


def _wishes(wishes: str | None, anon_chat: bool) -> str:
    """The receiver's wishes; without them, point to the anonymous question only when it is on."""
    return wishes or ("«не написаны — можно спросить анонимно»" if anon_chat else "«не написаны»")


def splice_needs_redraw(title: str) -> str:
    return (
        f"В игре {_q(title)} кто-то выбыл после жеребьёвки, и пары теперь не складываются как надо. "
        "Перезапустите жеребьёвку в пульте игры."
    )


def splice_too_few(title: str) -> str:
    return (
        f"В игре {_q(title)} кто-то выбыл после жеребьёвки, и участников осталось меньше трёх — "
        "обмен так не сложится. Договоритесь о подарках сами или отмените игру в настройках пульта."
    )


def splice_no_redraws_left(title: str) -> str:
    return (
        f"В игре {_q(title)} кто-то выбыл после жеребьёвки, и новая пара попала в исключения. "
        "Перезапустить жеребьёвку больше нельзя — предупредите этих участников сами."
    )


# --- §5.6 free limit, waiting list, payment -----------------------------------------------------


def waiting_list(*, free: bool, current_limit: int, limit: int, price: int) -> str:
    """§5.6; a game that was already paid for says how many places it has instead of 'бесплатной'."""
    full = (f"Мест нет: в бесплатной игре до {current_limit} участников." if free
            else f"Мест нет: все {current_limit} мест в игре заняты.")
    return (
        f"{full} Я поставил вас в очередь и сообщил организатору. "
        f"Расширить игру до {limit} человек может любой участник — {price} ₽ один раз."
    )


WAITING_LIST_FULL = "Мест нет: игра заполнена. Я поставил вас в очередь и сообщил организатору."


def btn_pay(price: int) -> str:
    return f"Расширить за {price} ₽"


def waiting_notice(*, title: str, waiting: int, limit: int, price: int) -> str:
    return (
        f"В игру {_q(title)} хотят вступить ещё {waiting} чел., но мест нет. "
        f"Расширить до {limit} — {price} ₽."
    )


def waiting_notice_full(*, title: str, waiting: int) -> str:
    """The waiting notice when the game cannot grow any more (top tier or payments off)."""
    return f"В игру {_q(title)} хотят вступить ещё {waiting} чел., но мест нет."


BTN_UPGRADE_SHORT = "Расширить"
CHOOSE_TIER = "До скольких участников расширить игру? Платится один раз за игру."


def btn_tier(limit: int, amount: int) -> str:
    return f"До {limit} — {amount} ₽"


def pay_offer(*, amount: int, title: str, limit: int) -> str:
    return (
        f"Оплата {amount} ₽ — расширение игры {_q(title)} до {limit} участников. "
        "Можно через СБП или картой на странице Robokassa. После оплаты игра расширится сама, "
        "а люди из очереди попадут в игру. Если страница не открывается — включите Wi-Fi."
    )


def btn_pay_link(amount: int) -> str:
    return f"Оплатить {amount} ₽"


def payment_description(limit: int) -> str:
    """Robokassa Description: up to 100 characters, no quotes or special symbols (§7)."""
    return f"Расширение игры Тайный Санта до {limit} участников"


PAY_CHECKING = "Проверяю оплату…"
PAYMENT_PENDING = "Оплата ещё не пришла. Если вы оплатили — подождите пару минут, я напишу сам."
UPGRADE_ONLY_BEFORE_DRAW = "Расширить игру можно только до жеребьёвки."
UPGRADE_NOT_NEEDED = "Игра уже расширена до этого размера."
PAYMENTS_UNAVAILABLE = "Оплата пока недоступна. Попробуйте позже."


def upgrade_contact_us(*, max_limit: int, support_email: str) -> str:
    return f"Для игр больше {max_limit} участников напишите нам: {support_email} — подключим вручную."


def spot_opened(title: str) -> str:
    return f"Место появилось — вы в игре {_q(title)}! Напишите, что хотели бы получить."


def payment_received(*, title: str, limit: int) -> str:
    return f"Оплата получена, спасибо! Игра {_q(title)} теперь до {limit} участников."


def payer_paid(*, payer: str, title: str, limit: int) -> str:
    return f"{payer} оплатил(а) расширение игры {_q(title)} до {limit} участников."


def double_payment(*, code: str, first_inv: int, second_inv: int) -> str:
    return f"Двойная оплата {code}: InvId {first_inv} и {second_inv} — верните одну в кабинете Robokassa."


def payment_after_draw(*, code: str, inv_id: int) -> str:
    return (
        f"Оплата InvId {inv_id} пришла, когда игра {code} уже не набирает участников, — расширение не "
        f"применено. Верните деньги в кабинете Robokassa и отметьте /refund {inv_id}."
    )


def overpayment(*, code: str, inv_id: int, amount: int, excess: int) -> str:
    """A link made before another upgrade of the same game was paid later (§4: pay only the difference)."""
    if excess >= amount:
        return (
            f"Лишняя оплата {code}: InvId {inv_id} ({amount} ₽) пришла, когда игра уже была расширена "
            f"по другой оплате. Верните её в кабинете Robokassa и отметьте /refund {inv_id}."
        )
    return (
        f"Переплата {code}: InvId {inv_id} ({amount} ₽) оплачена по ссылке, выданной до другой оплаты этой "
        f"игры. Верните {excess} ₽ в кабинете Robokassa (частичный возврат)."
    )


def payment_too_late(*, title: str, amount: int, support_email: str) -> str:
    return (
        f"Оплата {amount} ₽ пришла, когда игра {_q(title)} уже не набирала участников, — расширение не "
        f"понадобилось. Мы вернём деньги; если возврата не будет в течение 10 дней, напишите {support_email}."
    )


def payment_not_needed(*, title: str, limit: int, amount: int, support_email: str) -> str:
    return (
        f"Оплата {amount} ₽ получена, но игра {_q(title)} уже расширена до {limit} участников по другой "
        f"оплате. Мы вернём деньги; если возврата не будет в течение 10 дней, напишите {support_email}."
    )


def partial_refund(*, excess: int, support_email: str) -> str:
    return (
        f"Часть игры уже была оплачена раньше, поэтому {excess} ₽ мы вернём; если возврата не будет "
        f"в течение 10 дней, напишите {support_email}."
    )


# --- §5.7 anonymous messages ----------------------------------------------------------------------


def relay_prompt_to_receiver(receiver: str) -> str:
    return f"Напишите сообщение своему получателю ({receiver}). Я передам его без вашего имени (до 500 символов)."


RELAY_PROMPT_TO_SANTA = "Напишите сообщение своему Тайному Санте (до 500 символов). Я передам его."
RELAY_PROMPT_REPLY = "Напишите ответ (до 500 символов)."


def relay_from_santa(*, title: str, text: str) -> str:
    return f"Сообщение от вашего Тайного Санты (игра {_q(title)}): «{text}»"


def relay_from_receiver(*, receiver: str, text: str) -> str:
    return f"Сообщение от {receiver} — человека, которому вы дарите: «{text}»"


BTN_REPLY_TO_SANTA = "Ответить Санте"
BTN_REPLY_ANONYMOUSLY = "Ответить анонимно"
BTN_REPORT = "Пожаловаться"
RELAY_SENT_ANONYMOUSLY = "Передал. Ваше имя не видно."
RELAY_SENT_TO_SANTA = "Передал вашему Санте."


def relay_too_long(limit: int) -> str:
    return f"Сообщение должно быть до {limit} символов."


RELAY_DAILY_LIMIT = "На сегодня хватит сообщений в этой игре — не больше 20 в сутки."
ANON_CHAT_OFF = "Организатор выключил анонимные сообщения в этой игре."
RELAY_NOT_AVAILABLE = "Сообщения доступны только после жеребьёвки."
REPORT_SENT = "Спасибо, жалоба отправлена. Мы проверим."
RELAY_EMPTY = "Напишите текст сообщения."
RELAY_PAIR_CHANGED = "Пары изменились — ответить на это сообщение уже нельзя. Нажмите «Кому я дарю?» в своей игре."


def report_to_admin(*, report_id: int, code: str | None, reporter_id: int, reported_id: int, text: str) -> str:
    return (
        f"Жалоба №{report_id} · игра {code or '—'}\n"
        f"Пожаловался: {reporter_id} · отправитель: {reported_id}\n"
        f"Текст: «{text}»"
    )


BTN_BLOCK_SENDER = "Заблокировать отправителя"
BTN_CLOSE_REPORT = "Закрыть жалобу"
BTN_CLEAR_REPORTED = "Стереть тексты отправителя"
REPORT_CLOSED = "Жалоба закрыта."

# --- §5.8 my games and help -----------------------------------------------------------------------

NO_GAMES = "У вас пока нет игр. Создайте свою или вступите по коду из приглашения."
BTN_MY_PARTICIPATION = "Моё участие"
MY_GAMES_HEADER = "Ваши игры:"
STATUS_LABELS = {
    "collecting": "идёт набор",
    "drawn": "жеребьёвка проведена",
    "finished": "завершена",
    "cancelled": "отменена",
}
ROLE_ORGANIZER = "организатор"
ROLE_PARTICIPANT = "участник"
ROLE_WAITING = "в очереди"


def my_game_line(*, title: str, status: str, role: str) -> str:
    return f"{title} — {STATUS_LABELS.get(status, status)}, {role}"


def game_view(*, title: str, organizer: str, budget: str, exchange_date: date | None, status: str,
              name: str, wishes: str | None, waiting: bool) -> str:
    lines = [
        f"Игра {_q(title)} — {STATUS_LABELS.get(status, status)}",
        f"Организатор: {organizer}. Бюджет: {budget}. Обмен: {date_text(exchange_date)}.",
        f"Вы в игре как: {name}",
        f"Ваши пожелания: {wishes or 'пока не написаны'}",
    ]
    if waiting and status == "collecting":
        lines.append("Вы в очереди: как только появится место, я напишу.")
    elif waiting and status in ("drawn", "finished"):
        lines.append("Вы остались в очереди: жеребьёвка прошла без вас.")
    return "\n".join(lines)


def help_text(*, free_limit: int, price_S: int, limit_S: int, price_M: int, limit_M: int,
              price_L: int, limit_L: int, support_email: str, base_url: str) -> str:
    return (
        "Как это работает:\n"
        "1) Организатор нажимает «Создать игру» и пересылает приглашение в общий чат.\n"
        "2) Участники нажимают ссылку, затем «Начать», и пишут, что хотят получить.\n"
        "3) Организатор нажимает «Провести жеребьёвку» — каждому приходит, кому он дарит, "
        "и пожелания этого человека.\n"
        "4) Своему получателю можно задать вопрос анонимно.\n"
        f"До {free_limit} участников — бесплатно. Больше — один раз за игру: {price_S} ₽ (до {limit_S}), "
        f"{price_M} ₽ (до {limit_M}), {price_L} ₽ (до {limit_L}).\n"
        "Ребёнок без MAX? Родитель вступает сам и меняет имя, например «Маша, 5Б».\n"
        f"Вопросы: {support_email}. Правила: {base_url}/terms · Политика: {base_url}/privacy"
    )


# --- §5.9 the only unsolicited messages --------------------------------------------------------------


def organizer_nudge(*, title: str, days: int, active: int) -> str:
    return f"До обмена в {_q(title)} {days} дн., а жеребьёвки ещё не было. Участников: {active}."


def pre_exchange_reminder(*, title: str, receiver: str) -> str:
    return f"Завтра обмен подарками в игре {_q(title)}. Вы дарите: {receiver}."


# --- §5.10 group mode ------------------------------------------------------------------------------

GROUP_HELLO = (
    "Привет! Я помогу провести Тайного Санту в этом чате. Кто организует — нажмите кнопку, "
    "настройка займёт минуту."
)
BTN_CREATE_IN_CHAT = "Создать игру в этом чате"
GROUP_NOT_FOUND = "Не вижу этот чат. Добавьте бота в чат ещё раз и нажмите кнопку в нём."
BTN_JOIN_GROUP_GAME = "Участвую"


def group_card(*, title: str, budget: str, exchange_date: date | None, names: Sequence[str], limit: int) -> str:
    return (
        f"Тайный Санта {_q(title)}\n"
        f"Бюджет: {budget} · Обмен: {date_text(exchange_date)}\n"
        f"Участвуют: {len(names)} из {limit}: {names_preview(names)}\n"
        "Нажмите «Участвую», затем «Начать» в боте — туда придёт ваша пара."
    )


GROUP_CARD_DRAWN = (
    "Жеребьёвка проведена! Каждому участнику бот прислал пару в личные сообщения. "
    "Не пришло? Откройте бота → «Мои игры»."
)

# --- §5.11 reveal ------------------------------------------------------------------------------------

REVEAL_TOO_EARLY = "Раскрыть пары можно после даты обмена."
REVEAL_ALREADY_DONE = "Пары уже раскрыты."
REVEAL_SENT = "Готово! Все участники узнали, кто чей Санта."
REVEAL_SENT_TO_GROUP = "Готово! Цепочку «кто чей Санта» я отправил в чат."


def confirm_reveal(title: str) -> str:
    return f"Раскрыть всем участникам игры {_q(title)}, кто чей Санта? Это можно сделать один раз."


BTN_CONFIRM_REVEAL = "Да, раскрыть"


def reveal_chain(*, title: str, pairs: Iterable[tuple[str, str]], footer: str = "") -> str:
    return fit_lines([f"{giver} → {receiver}" for giver, receiver in pairs],
                     header=f"Кто чей Санта в игре {_q(title)}:", footer=footer)


def reveal_group_footer(link: str) -> str:
    return f"Провести такую же игру в другом чате: {link}"


# --- §9 admin -------------------------------------------------------------------------------------------


def _percent(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%"


def _number(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}".replace(".", ",")


def stats(report: StatsReport) -> str:
    """The /stats reply for ``core.analytics.StatsReport``."""
    parts = []
    for period, block in report.blocks:
        parts.append(
            f"{period.label}:\n"
            f"новые пользователи {block.new_users}, согласий {block.consents}\n"
            f"игр создано {block.games_created}, из них с 3+ участниками {block.games_3plus}\n"
            f"жеребьёвок {block.draws}, участников в среднем {_number(block.draw_size_avg)}, "
            f"медиана {_number(block.draw_size_median)}\n"
            f"упёрлись в бесплатный лимит {block.games_hit_limit}\n"
            f"оплачено игр {block.paid_games}, выручка {block.revenue_rub} ₽, "
            f"конверсия {_percent(block.conversion)}\n"
            f"участник → организатор {_percent(block.participant_to_organizer)}\n"
            f"игр по кнопке «в другом чате» {block.ref_games}\n"
            f"пары не дошли {_percent(block.result_dm_failure_rate)}"
        )
    sources = ", ".join(f"{source} {count}" for source, count in report.organizer_sources) or "—"
    parts.append(f"Открытых жалоб: {report.open_reports}\nОткуда организаторы (7 дней): {sources}")
    return "\n\n".join(parts)


def admin_game_summary(*, code: str, title: str, status: str, tier: str, active: int, limit: int,
                       waiting: int, organizer_id: int, created_at: str,
                       payments: Sequence[tuple[int, str, int, str]]) -> str:
    """/game CODE: no wishes and no relay texts. ``payments`` are (inv_id, tier, amount, status)."""
    lines = [
        f"Игра {code} · {_q(title)}",
        f"Статус: {STATUS_LABELS.get(status, status)} · тариф {tier}",
        f"Участников: {active} из {limit}, в очереди {waiting}",
        f"Организатор: {organizer_id} · создана {created_at[:10]}",
    ]
    payment_lines = [f"InvId {inv} · {t} · {amount} ₽ · {state}" for inv, t, amount, state in payments]
    return fit_lines(payment_lines or ["Платежей нет."], header="\n".join(lines))


def btn_grant(tier: str) -> str:
    return f"Выдать {tier}"


BTN_ADMIN_CANCEL_GAME = "Отменить игру"
ADMIN_GAME_CLOSED = "Игра уже отменена или завершена — это действие недоступно."


def confirm_admin_cancel(*, code: str, title: str) -> str:
    return f"Отменить игру {code} {_q(title)}? Участники и организатор получат уведомление. Это нельзя вернуть."


def admin_game_cancelled(code: str) -> str:
    return f"Игра {code} отменена, участники получили уведомление."


def game_cancelled_by_service(*, title: str, support_email: str) -> str:
    return f"Игра {_q(title)} отменена администрацией сервиса. Вопросы: {support_email}."


def game_upgraded(*, title: str, limit: int) -> str:
    """To the organizer after /grant (a manual payment, e.g. a bank transfer)."""
    return f"Игра {_q(title)} расширена до {limit} участников."


def granted(*, code: str, tier: str, limit: int) -> str:
    return f"Игра {code}: выдан тариф {tier}, лимит {limit}."


def refunded(inv_id: int) -> str:
    return f"Платёж {inv_id} отмечен как возвращённый. Деньги верните в кабинете Robokassa."


PAYMENT_NOT_FOUND = "Платёж не найден."


def payment_not_refundable(*, inv_id: int, status: str) -> str:
    return f"Платёж {inv_id} в статусе {status}: вернуть можно только оплаченный (paid) или выданный (granted)."


ADMIN_GAME_NOT_FOUND = "Игра не найдена."


def price_list(*, free_limit: int, price_S: int, limit_S: int, price_M: int, limit_M: int,
               price_L: int, limit_L: int) -> str:
    return (
        "Цены сейчас:\n"
        f"бесплатно — до {free_limit}\n"
        f"S — до {limit_S} за {price_S} ₽\n"
        f"M — до {limit_M} за {price_M} ₽\n"
        f"L — до {limit_L} за {price_L} ₽"
    )


PRICE_USAGE = "Формат: /price S 490 или /price free 10 (также M, L, limit_S, limit_M, limit_L)."
GRANT_USAGE = "Формат: /grant КОД S|M|L [сумма]"
REFUND_USAGE = "Формат: /refund INVID"
GAME_USAGE = "Формат: /game КОД"
BLOCK_USAGE = "Формат: /block USERID или /unblock USERID"
MAINTENANCE_USAGE = "Формат: /maintenance on или /maintenance off"


def user_blocked(user_id: int) -> str:
    return f"Пользователь {user_id} заблокирован."


def user_unblocked(user_id: int) -> str:
    return f"Пользователь {user_id} разблокирован."


USER_NOT_FOUND = "Пользователь не найден."
FORGET_USAGE = "Формат: /forget USERID (id человек узнаёт командой /whoami)"
CLEAN_USAGE = "Формат: /clean КОД USERID"
BTN_CONFIRM_FORGET = "Да, удалить всё"
BTN_RESET_TITLE = "Сбросить название"
NOT_IN_THAT_GAME = "Этот человек не участвовал в этой игре."


def confirm_forget(user_id: int) -> str:
    return (
        f"Удалить все данные пользователя {user_id}? Его имя, пожелания, пары, сообщения и жалобы "
        "пропадут. Игры, которые он организует и которые ещё идут, отменятся (участники получат "
        "уведомление), а из идущих игр он выйдет. Вернуть данные будет нельзя."
    )


def user_forgotten(*, user_id: int, cancelled: int, left: int) -> str:
    return (
        f"Данные пользователя {user_id} удалены. Отменено его игр: {cancelled}, вышел из игр: {left}. "
        "Если он снова напишет боту, всё начнётся с согласия."
    )


def texts_cleared(*, user_id: int, code: str) -> str:
    return f"Пожелания, имя и сообщения пользователя {user_id} в игре {code} стёрты."


def title_reset(code: str) -> str:
    return f"Название игры {code} сброшено на «{DEFAULT_TITLE}»."
BTN_UNBLOCK = "Разблокировать"
MAINTENANCE_ON = "Режим технических работ включён: новые игры и вступления временно закрыты."
MAINTENANCE_OFF = "Режим технических работ выключен."


ADMIN_HELP = (
    "Команды администратора:\n"
    "/stats — статистика за сегодня, 7 дней и сезон\n"
    "/game КОД — сводка по игре, выдать тариф, отменить игру\n"
    "/grant КОД S|M|L [сумма] — расширить игру вручную (оплата по счёту)\n"
    "/refund INVID — отметить платёж возвращённым\n"
    "/price — цены; /price S 490, /price free 10, /price limit_S 30 — изменить\n"
    "/block USERID, /unblock USERID — блокировка\n"
    "/clean КОД USERID — стереть пожелания, имя и сообщения человека в игре\n"
    "/forget USERID — удалить все данные человека по его просьбе\n"
    "/maintenance on|off — технические работы\n"
    "/ads — реклама: сводка и управление (все команды: /ads help)\n"
    "/link СЛОВО [название] — ссылка для ручного продвижения; /links — что принесли ссылки\n"
    "/channel — посты в ваш канал MAX\n"
    "/whoami — ваш id"
)


def digest(*, yesterday: StatsBlock, today: StatsBlock) -> str:
    def line(label: str, block: StatsBlock) -> str:
        return (
            f"{label}: игр {block.games_created}, жеребьёвок {block.draws}, "
            f"оплат {block.paid_games} на {block.revenue_rub} ₽, новых пользователей {block.new_users}"
        )

    return "Сводка\n" + line("Вчера", yesterday) + "\n" + line("Сегодня", today)


def error_alert(summary: str) -> str:
    return f"Ошибка в боте: {summary}"


def with_suppressed(text: str, suppressed: int) -> str:
    """An admin alert followed by how many similar alerts were held back."""
    return text + (f"\n(похожих сообщений пропущено: {suppressed})" if suppressed else "")


def backup_upload_failed(reason: str) -> str:
    return (
        f"Не удалось отправить резервную копию в хранилище S3 ({reason}). Копия на сервере сохранена. "
        "Проверьте S3_ENDPOINT, S3_BUCKET, S3_KEY, S3_SECRET и S3_REGION."
    )


TOKEN_REJECTED = "Токен бота не принят — проверьте MAX_BOT_TOKEN"
WEBHOOK_RESTORED = "Подписка вебхука была потеряна и восстановлена"
WEBHOOK_WORKS = "Подписка вебхука оформлена — бот снова получает сообщения."


def webhook_failed(detail: str) -> str:
    return (
        f"Не удалось подписать бота на сообщения MAX: {shorten(detail, 200)}. Пока это так, бот ничего "
        "не получает. Проверьте, что сайт открывается по https (README, раздел 11); я пробую снова "
        "каждые 10 минут."
    )


def username_mismatch(*, actual: str, configured: str) -> str:
    return f"Имя бота в MAX — {actual}, а в настройках MAX_BOT_USERNAME={configured}. Ссылки не будут работать."


def certificate_expiring(*, name: str, file: str, expires: date, days: int) -> str:
    when = f"{format_date(expires)} {expires.year}"
    status = f"истекает {when} (через {days} дн.)" if days > 0 else f"истёк {when}"
    return (
        f"Сертификат {name} {status}. Скачайте новый с gu-st.ru, сохраните его как certs/{file} "
        "вместо старого (имя файла должно заканчиваться на .pem) и пересоберите бота — README, раздел 10."
    )


def bad_signature_alert(inv_id: str) -> str:
    return f"Robokassa прислала уведомление с неверной подписью (InvId {inv_id}). Проверьте пароли в .env."


def unknown_invoice_alert(inv_id: int) -> str:
    return (
        f"Robokassa подтвердила оплату InvId {inv_id}, но такого платежа нет в базе "
        "(например, база восстановлена из старой копии). Найдите платёж в кабинете Robokassa "
        "и выдайте тариф командой /grant или верните деньги."
    )


def amount_mismatch_alert(*, inv_id: int, received: str, expected: int) -> str:
    return (
        f"Robokassa прислала оплату InvId {inv_id} на сумму {received}, а ожидалось {expected} ₽. "
        "Тариф не выдан — проверьте платёж в кабинете Robokassa."
    )


def message_rejected(*, target: str, error: str) -> str:
    return f"MAX отклонил сообщение для {target}: {error}"



# --- PROMO_SPEC: the ad autopilot, tracking links and the own MAX channel (admins only) -------------------
# Keys are the stored values: platforms 'yd'/'vk', states, 'paused_by' causes, budget periods.

PROMO_DISABLED = (
    "Автопилот рекламы выключен. Чтобы включить, поставьте PROMO_ENABLED=1 в .env и выполните "
    "docker compose up -d. Инструкция — docs/PROMO_KIT_RU.md."
)
_PROMO_PLATFORMS = {"yd": "Яндекс", "vk": "VK"}
_PROMO_PLATFORMS_FULL = {"yd": "Яндекс Директ", "vk": "VK Реклама"}
_PROMO_STATES = {"active": "идёт", "paused": "на паузе", "other": "не показывается"}
_PROMO_PAUSED_BY = {"autopilot": "остановил автопилот", "admin": "остановили вы", "cap": "лимит расходов",
                   "season": "вне сезона"}
_PROMO_PERIODS = {"week": "неделю", "day": "день"}
_PROMO_MODE_DRY = "тест — ничего не меняю"
_PROMO_MODE_AUTO = "автопилот"
BTN_PROMO_DECLINE = "Не надо"
BTN_PROMO_CHANNEL = "Провести Тайного Санту"


def _rub(kop: int) -> str:
    """Whole rubles, thousands apart: 1234560 kopecks → '12 346'."""
    rubles = (abs(kop) + 50) // 100
    return ("-" if kop < 0 else "") + f"{rubles:,}".replace(",", " ")


def _rub_exact(kop: int) -> str:
    """Rubles and kopecks, as the cabinets show net budgets: 38524 → '385,24'."""
    return f"{kop // 100:,}".replace(",", " ") + f",{kop % 100:02d}"


def btn_promo_raise(to_kop: int) -> str:
    return f"Поднять до {_rub(to_kop)} ₽"


def _promo_state(state: str, paused_by: str | None) -> str:
    label = _PROMO_STATES[state]
    if paused_by and state == "paused":
        return f"{label} ({_PROMO_PAUSED_BY[paused_by]})"
    if paused_by == "admin":
        return f"{label}, без автопилота: вы её останавливали (/ads resume вернёт автопилот)"
    return label


# reasons of the rules (PROMO_SPEC §5)

def promo_reason_season(*, ended: bool) -> str:
    return "сезон закончился" if ended else "сезон ещё не начался"


def promo_reason_cap(*, cap_kop: int, spent_kop: int, projected_kop: int) -> str:
    return (f"достигнут лимит {_rub(cap_kop)} ₽: потрачено {_rub(spent_kop)} ₽, а ещё день с текущими "
            f"бюджетами — это до {_rub(projected_kop)} ₽")


def promo_reason_expensive(*, until: date, spend_kop: int, games: int, pause_cpa_kop: int) -> str:
    spent = f"по {format_date(until)} потрачено {_rub(spend_kop)} ₽"
    if not games:
        return f"{spent}, а игр на 3+ участника нет"
    return (f"{spent}, игр на 3+ участника: {games} — {_rub(spend_kop // games)} ₽ за игру, "
            f"дороже порога {_rub(pause_cpa_kop)} ₽")


def promo_reason_cheap(*, until: date, spend_kop: int, games: int, scale_cpa_kop: int) -> str:
    return (f"по {format_date(until)} игр на 3+ участника: {games}, по {_rub(spend_kop // games)} ₽ за игру — "
            f"дешевле порога {_rub(scale_cpa_kop)} ₽")


# the daily report (PROMO_SPEC §7) and /ads

def promo_report_header(*, day: date, dry: bool, spent_kop: int, cap_kop: int) -> str:
    mode = _PROMO_MODE_DRY if dry else _PROMO_MODE_AUTO
    return f"Реклама — {format_date(day)}. Режим: {mode}.\nПотрачено всего {_rub(spent_kop)} ₽ из {_rub(cap_kop)} ₽."


def promo_campaign_line(*, platform: str, name: str, state: str, paused_by: str | None, yesterday_kop: int,
                        total_kop: int, games3: int, cpa_kop: int | None, paid_rub: int) -> str:
    cpa = "—" if cpa_kop is None else _rub(cpa_kop)
    return (f"{_PROMO_PLATFORMS[platform]} «{name}»: вчера {_rub(yesterday_kop)} ₽, всего {_rub(total_kop)} ₽ · "
            f"игр на 3+: {games3} · {cpa} ₽ за игру · оплат {paid_rub} ₽ · {_promo_state(state, paused_by)}")


def promo_paused_line(*, name: str, reason: str, dry: bool) -> str:
    return f"{'Сделал бы' if dry else 'Сделал'}: «{name}» — пауза: {reason}."


def promo_pause_failed_line(*, name: str, reason: str, error: str) -> str:
    return (f"Не получилось: «{name}» — пауза ({reason}) не удалась: {error}. "
            "Остановите кампанию вручную в кабинете или командой /ads stop.")


def promo_proposal_line(*, name: str, from_kop: int, to_kop: int, kind: str, reason: str, dry: bool) -> str:
    verb = "Предложил бы" if dry else "Предлагаю"
    return (f"{verb}: поднять бюджет «{name}» с {_rub(from_kop)} до {_rub(to_kop)} ₽ в {_PROMO_PERIODS[kind]}: "
            f"{reason}.")


def promo_link_entry(*, slug: str, users: int, games: int, games3: int, paid_rub: int) -> str:
    return (f"{slug}: {users} чел., {games} {plural(games, 'игра', 'игры', 'игр')}, {games3} на 3+, "
            f"оплат {paid_rub} ₽")


def promo_links_line(entries: Sequence[str]) -> str:
    return "Ссылки: " + " · ".join(entries)


def promo_channel_line(*, posts: int, users: int, games: int) -> str:
    return f"Канал MAX: постов {posts}, пришло {users} чел., игр {games}."


def promo_no_credentials(platform: str) -> str:
    keys = {"yd": "YANDEX_DIRECT_TOKEN", "vk": "VK_ADS_CLIENT_ID и VK_ADS_CLIENT_SECRET"}
    return f"{_PROMO_PLATFORMS_FULL[platform]}: в .env нет {keys[platform]} — эти кампании не проверяю и не трогаю."


def promo_auth_note(platform: str) -> str:
    return (f"{_PROMO_PLATFORMS_FULL[platform]}: доступ не принят (как исправить — в отдельном сообщении), "
            "цифры не обновлены.")


def promo_fetch_failed(platform: str, detail: str, *, sandbox: bool = False) -> str:
    text = (f"{_PROMO_PLATFORMS_FULL[platform]}: не удалось обновить данные ({shorten(detail, 150)}); "
            "цифры — прошлые, попробую снова.")
    if sandbox:
        text += (" Включена песочница Директа (YANDEX_DIRECT_SANDBOX=1): возможно, она не работает с версией "
                 "API v501 — проверьте с боевым доступом.")
    return text


PROMO_ERROR_NO_KEYS = "в .env нет ключей доступа к API этой площадки"
PROMO_ERROR_AUTH = "площадка не приняла ключи доступа"
PROMO_ERROR_BUSY = "площадка просит подождать: исчерпан лимит запросов"
PROMO_ERROR_DOWN = "площадка не ответила"
PROMO_ERROR_BUDGET_KIND = (
    "у кампании нет бюджета, который я умею менять: в Директе нужен недельный бюджет стратегии, "
    "в VK Рекламе — дневной бюджет кампании"
)
PROMO_ERROR_NOT_FOUND = "кампания не найдена в кабинете"
PROMO_ERROR_UNREADABLE = "ответ площадки не удалось разобрать"


def promo_error_platform(message: str) -> str:
    return f"площадка ответила: {shorten(message, 150)}"


def promo_campaign_missing(*, name: str, src: str) -> str:
    return f"«{name}» не нашлась в кабинете — может быть, её удалили. Убрать из автопилота: /ads remove {src}"


def promo_guard_report(lines: Sequence[str]) -> str:
    return fit_lines(list(lines), header="Защита расходов:")


# approvals of budget raises

def promo_proposal(*, name: str, from_kop: int, to_kop: int, kind: str, net_kop: int, reason: str) -> str:
    return (f"Предлагаю поднять бюджет «{name}» с {_rub(from_kop)} до {_rub(to_kop)} ₽ в {_PROMO_PERIODS[kind]}: "
            f"{reason}. В кабинете это будет {_rub_exact(net_kop)} ₽ без НДС. Предложение действует сутки.")


def promo_raise_applied(*, name: str, to_kop: int, kind: str, net_kop: int) -> str:
    return (f"Готово: бюджет «{name}» теперь {_rub(to_kop)} ₽ в {_PROMO_PERIODS[kind]} "
            f"(в кабинете {_rub_exact(net_kop)} ₽ без НДС).")


def promo_raise_declined(*, name: str, from_kop: int) -> str:
    return f"Хорошо, бюджет «{name}» оставляю {_rub(from_kop)} ₽."


def promo_proposal_expired(reason: str) -> str:
    return f"Не поднимаю бюджет: {reason}. Завтрашний отчёт всё посчитает заново."


PROMO_PROPOSAL_DECIDED = {
    "applied": "Этот бюджет уже подняли.",
    "declined": "От этого предложения уже отказались.",
    "failed": "Это предложение уже пробовали выполнить, но не получилось — подробности были в ответе.",
    "expired": "Это предложение устарело — ждите завтрашний отчёт.",
}
PROMO_PROPOSAL_DRY = "Это предложение тестового режима — в нём я ничего не меняю. Включить автопилот: /ads auto on."
PROMO_EXPIRED_OLD = "предложению больше суток"
PROMO_EXPIRED_NOT_RUNNING = "кампания сейчас не идёт"
PROMO_EXPIRED_REMOVED = "кампания больше не под управлением бота"
PROMO_EXPIRED_CHANGED = "бюджет с тех пор уже меняли"


def promo_expired_cap(cap_kop: int) -> str:
    return f"с новым бюджетом расходы могут превысить лимит {_rub(cap_kop)} ₽"


def promo_budget_failed(*, platform: str, name: str, net_kop: int, error: str) -> str:
    if platform == "yd":
        how = (f"Директ → кампания «{name}» → «Редактировать» → «Стратегия» → «Недельный бюджет» "
               f"{_rub_exact(net_kop)} ₽")
    else:
        how = f"VK Реклама → кампания «{name}» → «Редактировать» → «Бюджет» → дневной {_rub_exact(net_kop)} ₽"
    return f"Не удалось поменять бюджет «{name}»: {shorten(error, 200)}. Поменяйте вручную: {how} (без НДС)."


# alerts when a platform refuses our keys (at most once per 6 hours per platform)

def promo_auth_alert(*, platform: str, code: str, detail: str) -> str:
    info = f" ({shorten(detail, 150)})" if detail else ""
    if platform == "yd":
        if code in ("53", "1002", "401", "403"):
            return (f"Яндекс Директ не принял токен (ошибка {code}){info}. Получите новый OAuth-токен "
                    "(docs/PROMO_KIT_RU.md, раздел 3, шаг «Токен»), впишите его в YANDEX_DIRECT_TOKEN в .env и "
                    "выполните docker compose up -d. Пока токен не работает, я не вижу расходы Директа и не могу "
                    "остановить его кампании.")
        if code == "58":
            return ("Заявка на доступ к API Директа ещё не одобрена (ошибка 58). Проверьте её: "
                    "https://direct.yandex.ru/registered/main.pl?cmd=apiSettings → «Мои заявки». Проверка "
                    "занимает от часа до трёх рабочих дней.")
        return (f"У аккаунта Директа нет доступа к API (ошибка {code}){info}. Проверьте, что токен выдан тому "
                "же логину Яндекса, на котором кампании, и что условия API приняты: "
                "https://direct.yandex.ru/registered/main.pl?cmd=apiSettings")
    if code == "invalid_client":
        return ("VK Реклама не принимает VK_ADS_CLIENT_ID и VK_ADS_CLIENT_SECRET. Проверьте их в кабинете: "
                "«Настройки» → «Доступ к API». Если секрет потерян, запросите доступ заново, впишите новые "
                "значения в .env и выполните docker compose up -d.")
    if code == "token_limit":
        return ("VK Реклама не выдаёт новый токен доступа: у приложения уже 5 токенов, а удалить старые не "
                "вышло. Проверьте /ads через час; если не помогло — напишите в поддержку API: ads_api@vk.team.")
    return (f"VK Реклама отказала в доступе ({code}){info}. Проверьте в кабинете, что аккаунт активен и доступ "
            "к API включён («Настройки» → «Доступ к API»).")


# /ads and its subcommands

PROMO_ADS_USAGE = (
    "Команды рекламы:\n"
    "/ads — сводка по кампаниям\n"
    "/ads add yd НОМЕР [имя] — подключить кампанию Яндекс Директа\n"
    "/ads add vk НОМЕР [имя] — подключить кампанию VK Рекламы\n"
    "/ads remove ИСТОЧНИК — перестать управлять кампанией (на площадке она не останавливается)\n"
    "/ads auto on|off — включить или выключить автопилот\n"
    "/ads stop — остановить все кампании сейчас\n"
    "/ads resume ИСТОЧНИК — снова запустить кампанию\n"
    "/ads budget ИСТОЧНИК РУБЛИ — поставить бюджет (сумма с НДС)\n"
    "/ads cap РУБЛИ — общий лимит расходов (с НДС)\n"
    "/ads rules ДОРОГО ДЁШЕВО [МИНИМУМ] [ПРОЦЕНТ] [ДНИ] — пороги автопилота\n"
    "ИСТОЧНИК — например yd701234567 или vk123456, он есть в /ads."
)
PROMO_ADD_USAGE = "Формат: /ads add yd НОМЕР [имя] или /ads add vk НОМЕР [имя]. НОМЕР — цифры из кабинета."
PROMO_AUTO_USAGE = "Формат: /ads auto on или /ads auto off"
PROMO_SRC_USAGE = "Укажите источник кампании, например yd701234567 — список в /ads."
PROMO_BUDGET_USAGE = "Формат: /ads budget ИСТОЧНИК РУБЛИ, например /ads budget yd701234567 2000 (сумма с НДС)."
PROMO_CAP_USAGE = "Формат: /ads cap РУБЛИ, например /ads cap 15000 (сумма с НДС)."
PROMO_RULES_USAGE = (
    "Формат: /ads rules ДОРОГО ДЁШЕВО [МИНИМУМ] [ПРОЦЕНТ] [ДНИ], например /ads rules 250 150 500 30 2. "
    "ДЁШЕВО меньше ДОРОГО, ПРОЦЕНТ — рост бюджета за раз, от 1 до 100, ДНИ — через сколько дней учитывать "
    "игру, от 0 до 14."
)
PROMO_NO_CAMPAIGNS = "Кампаний под управлением пока нет. Подключить: /ads add yd НОМЕР или /ads add vk НОМЕР."
PROMO_AUTO_OFF = (
    "Автопилот выключен: дальше я только пишу, что сделал бы. Команды /ads stop, /ads resume и /ads budget "
    "работают как раньше."
)


def promo_status_header(*, dry: bool, spent_kop: int, cap_kop: int) -> str:
    mode = _PROMO_MODE_DRY if dry else _PROMO_MODE_AUTO
    return f"Реклама. Режим: {mode}.\nПотрачено всего {_rub(spent_kop)} ₽ из {_rub(cap_kop)} ₽."


def promo_rules_summary(*, pause_cpa_rub: int, scale_cpa_rub: int, min_spend_rub: int, max_raise_pct: int,
                        lag_days: int, season: str) -> str:
    return (f"Пороги: останавливаю кампанию, если игра на 3+ участника обходится дороже {pause_cpa_rub} ₽ "
            f"(или игр нет) после {min_spend_rub} ₽ расходов; предлагаю поднять бюджет (до +{max_raise_pct}%), "
            f"если игра дешевле {scale_cpa_rub} ₽. Игры считаю с задержкой {lag_days} дн. Сезон рекламы: {season}.")


def promo_status_line(*, platform: str, name: str, src: str, state: str, paused_by: str | None,
                      yesterday_kop: int, total_kop: int, games3: int, cpa_kop: int | None, paid_rub: int,
                      downstream_games: int, budget_kop: int | None, kind: str | None) -> str:
    cpa = "—" if cpa_kop is None else _rub(cpa_kop)
    payback = f"{paid_rub * 10000 // total_kop}%" if total_kop else "—"
    budget = f"{_rub(budget_kop)} ₽ в {_PROMO_PERIODS[kind]}" if budget_kop is not None and kind else "не виден"
    spread = ""
    if downstream_games:
        spread = (f" · участники этих игр устроили ещё {downstream_games} "
                  f"{plural(downstream_games, 'игру', 'игры', 'игр')}")
    return (f"{_PROMO_PLATFORMS[platform]} «{name}» ({src}): {_promo_state(state, paused_by)} · вчера "
            f"{_rub(yesterday_kop)} ₽, всего {_rub(total_kop)} ₽ · игр на 3+: {games3} · {cpa} ₽ за игру · "
            f"оплат {paid_rub} ₽, окупаемость {payback}{spread} · бюджет {budget}")


def promo_waiting_proposal(*, name: str, to_kop: int) -> str:
    return f"Ждёт решения: поднять бюджет «{name}» до {_rub(to_kop)} ₽ — кнопки в сообщении после отчёта."


def promo_registered(*, platform: str, name: str, src: str, validated: bool, landing_url: str,
                     template: str) -> str:
    lines = [f"Подключил: {_PROMO_PLATFORMS_FULL[platform]} «{name}», источник {src}."]
    if not validated:
        lines.append("Проверить кампанию не смог — нет доступа к API площадки (см. .env). Проверьте номер сами.")
    lines += ["Ссылка для объявлений этой кампании:", landing_url]
    if platform == "yd":
        lines += ["Или одна ссылка на все кампании Директа — номер кампании Директ подставит сам:", template]
    else:
        lines += [
            "Или в группе объявлений: «Параметры URL» → «Добавлять UTM-метки вручную» → впишите:", template,
            "Тогда в объявлениях — просто адрес сайта. Автоматические UTM-метки VK не оставляйте: они заменят "
            "эту разметку, и я не увижу, откуда пришли люди.",
        ]
    lines.append("Пока автопилот в тесте (/ads auto on — включить), я только пишу, что сделал бы.")
    return "\n".join(lines)


def promo_not_found(*, platform: str, external_id: str) -> str:
    return (f"В кабинете {_PROMO_PLATFORMS_FULL[platform]} не нашлось кампании {external_id} (или у доступа нет к ней "
            "прав). Проверьте номер.")


def promo_unreachable(*, platform: str, detail: str) -> str:
    return (f"{_PROMO_PLATFORMS_FULL[platform]} сейчас не отвечает ({shorten(detail, 150)}). "
            "Попробуйте через несколько минут.")


def promo_unknown_src(src: str) -> str:
    return f"Не знаю кампанию «{src}». Список — /ads."


def promo_removed(*, name: str, src: str) -> str:
    return (f"Больше не управляю «{name}» ({src}). На площадке она не остановлена — если нужно, остановите её "
            "в кабинете. Её расходы остаются в общем лимите.")


def promo_auto_on(*, cap_kop: int, spent_kop: int, rules: str) -> str:
    return (
        "Автопилот включён. Что я теперь делаю сам:\n"
        "— каждый день в отчёте сверяю расходы с играми, которые принесла реклама;\n"
        "— останавливаю дорогие кампании (без вопросов);\n"
        "— предлагаю поднять бюджет дешёвым — поднимаю, только если вы нажмёте кнопку;\n"
        f"— останавливаю всё, если расходы могут превысить лимит {_rub(cap_kop)} ₽ (сейчас потрачено "
        f"{_rub(spent_kop)} ₽), и вне сезона рекламы;\n"
        "— снова запускаю кампании только по вашей команде /ads resume.\n"
        f"{rules}\n"
        "Выключить: /ads auto off. Остановить всё сразу: /ads stop."
    )


def promo_stop_line(*, name: str, error: str | None) -> str:
    return f"«{name}» — остановлена" if error is None else f"«{name}» — не получилось: {shorten(error, 150)}"


def promo_stopped(lines: Sequence[str]) -> str:
    return fit_lines(list(lines), header="Остановил все кампании (автопилот их больше не запустит):",
                     footer="Запустить снова: /ads resume ИСТОЧНИК.")


def promo_resumed(name: str) -> str:
    return f"Запустил «{name}». Автопилот снова следит за ней."


def promo_resume_out_of_season(*, ended: bool) -> str:
    return f"Не запускаю: {promo_reason_season(ended=ended)} (PROMO_START и PROMO_END в .env)."


def promo_over_cap(*, cap_kop: int, projected_kop: int) -> str:
    return (f"Не делаю: с этим расходы за следующий день могут дойти до {_rub(projected_kop)} ₽ — больше лимита "
            f"{_rub(cap_kop)} ₽. Поднимите лимит (/ads cap) или уменьшите бюджеты.")


def promo_action_failed(*, name: str, error: str) -> str:
    return f"Не получилось с «{name}»: {shorten(error, 200)}. Попробуйте ещё раз или сделайте это в кабинете."


def promo_budget_too_small(min_kop: int) -> str:
    return f"Бюджет не может быть меньше {_rub(min_kop)} ₽ с НДС (это 300 ₽ без НДС — минимум площадок)."


def promo_budget_set(*, name: str, gross_kop: int, kind: str, net_kop: int) -> str:
    return (f"Бюджет «{name}»: {_rub(gross_kop)} ₽ в {_PROMO_PERIODS[kind]} с НДС, в кабинете будет "
            f"{_rub_exact(net_kop)} ₽ без НДС.")


def promo_cap_set(*, cap_kop: int, spent_kop: int) -> str:
    return f"Общий лимит расходов: {_rub(cap_kop)} ₽ с НДС. Уже потрачено {_rub(spent_kop)} ₽."


# manual tracking links (/link, /links)

LINK_USAGE = (
    "Формат: /link СЛОВО [название], например /link habr Пост на Хабре. СЛОВО — до 15 латинских букв и цифр."
)
LINKS_EMPTY = "Ссылок пока нет. Создать: /link СЛОВО [название]."


def link_created(*, title: str, src: str, landing_url: str, bot_url: str, existed: bool) -> str:
    first = f"Такая ссылка уже есть: «{title}» ({src})." if existed else f"Ссылка «{title}» ({src}) готова."
    return (f"{first}\nНа сайт (лучше для постов и сообщений — там есть описание и цены):\n{landing_url}\n"
            f"Сразу в бота:\n{bot_url}\nЧто она принесла — /links.")


def links_report(lines: Sequence[str]) -> str:
    return fit_lines(list(lines), header="Ссылки (люди, игры, игры на 3+ участника, оплаты):")


def link_line(*, title: str, entry: str) -> str:
    return f"«{title}» — {entry}"


# the owner's MAX channel (/channel)

CHANNEL_USAGE = "Формат: /channel, /channel test, /channel on, /channel off или /channel send ch01"
CHANNEL_NO_ID = (
    "Не задан PROMO_MAX_CHANNEL_ID. Добавьте бота администратором в ваш канал — я пришлю его id, — впишите "
    "его в .env и выполните docker compose up -d."
)
CHANNEL_OFF = "Посты в канал выключены. Включить: /channel on."
CHANNEL_NOTHING_LEFT = "Все посты календаря уже отправлены или пропущены."


def channel_status(*, on: bool, channel_id: int | None, sent: int, skipped: int, total: int,
                   upcoming: Sequence[str], known: Sequence[int]) -> str:
    lines = [
        f"Посты в канал MAX: {'включены' if on else 'выключены'}. Канал: "
        f"{channel_id if channel_id is not None else 'не задан (PROMO_MAX_CHANNEL_ID)'}.",
        f"Календарь: {total} постов, отправлено {sent}, пропущено {skipped}.",
    ]
    lines += ["Следующие:", *upcoming] if upcoming else [CHANNEL_NOTHING_LEFT]
    if known:
        lines.append("Каналы, куда добавили бота: " + ", ".join(str(chat_id) for chat_id in known))
    lines.append("Команды: /channel test — пример следующего поста вам; /channel on|off; /channel send ch01.")
    return "\n".join(lines)


def channel_upcoming_line(*, post_id: str, when: str, first_line: str) -> str:
    return f"{post_id} — {when}: {shorten(first_line, 80)}"


def channel_on(*, channel_id: int) -> str:
    return f"Посты в канал {channel_id} включены: отправляю по календарю, ближайшие — в /channel."


def channel_sent(post_id: str) -> str:
    return f"Пост {post_id} отправлен в канал (появится через несколько секунд)."


def channel_already_sent(post_id: str) -> str:
    return f"Пост {post_id} уже был в канале — второй раз не отправляю."


def channel_unknown_post(post_id: str) -> str:
    return f"Нет поста «{post_id}». Номера — в /channel (ch01, ch02…)."


def channel_preview(*, post_id: str, when: str) -> str:
    return f"Так будет выглядеть пост {post_id} (по плану — {when}):"


def channel_added(chat_id: int) -> str:
    return (f"Бота добавили в канал, его id: {chat_id}. Если это ваш канал для постов, впишите в .env строку "
            f"PROMO_MAX_CHANNEL_ID={chat_id}, выполните docker compose up -d и проверьте: /channel test.")


def promo_post_failed(error: str) -> str:
    return (f"Пост не ушёл в канал MAX: {shorten(error, 200)}. Проверьте, что бот — администратор канала, "
            "а PROMO_MAX_CHANNEL_ID верный.")
