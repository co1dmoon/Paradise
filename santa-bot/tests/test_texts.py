from __future__ import annotations

import inspect
import re
from datetime import date
from pathlib import Path

from app.core import texts
from app.core.dates import DateError

FORBIDDEN_WORDS = re.compile(r"розыгрыш|конкурс|приз|вишлист|wishlist", re.IGNORECASE)


def test_copy_avoids_forbidden_words() -> None:
    source = Path(inspect.getfile(texts)).read_text(encoding="utf-8")
    assert not FORBIDDEN_WORDS.search(source)


def test_fit_lines_truncates_with_a_count() -> None:
    lines = [f"{i}. Участник с довольно длинным именем номер {i}" for i in range(500)]
    text = texts.fit_lines(lines, header="Участники:")
    assert len(text) <= texts.MAX_MESSAGE
    hidden = int(re.search(r"и ещё (\d+)$", text).group(1))
    shown = text.count("\n") - 1
    assert shown + hidden == 500
    assert texts.fit_lines(["a", "b"], header="h") == "h\na\nb"


def test_invite_matches_the_spec() -> None:
    invite = texts.invite(title="Отдел продаж", budget="до 1000 ₽", exchange_date=None,
                          link="https://max.ru/bot?start=j_ABC234", code="ABC234")
    assert invite.splitlines() == [
        "Тайный Санта «Отдел продаж»",
        "Бюджет: до 1000 ₽. Обмен подарками: дату сообщит организатор.",
        "Участвовать: нажмите ссылку, затем «Начать»",
        "https://max.ru/bot?start=j_ABC234",
        "Если бот у вас уже открыт — просто отправьте ему код: ABC234",
    ]
    names = [f"Имя{i}" for i in range(20)]
    with_progress = texts.invite(title="X", budget="до 500 ₽", exchange_date=date(2026, 12, 25), link="https://x",
                                 code="ABC234", participants=names)
    assert "Уже участвуют: 20 — Имя0, Имя1" in with_progress and "Имя14…" in with_progress


def test_draw_texts() -> None:
    assert texts.draw_confirm(active=8, without_wishes=0, waiting=0) == (
        "Провести жеребьёвку для 8 участников? После этого вступить будет нельзя.")
    assert "У 2 человек нет пожеланий. В очереди 1 человек" in texts.draw_confirm(active=8, without_wishes=2, waiting=1)
    result = texts.draw_result(title="Семья", receiver="Маша", wishes=None, budget="до 500 ₽",
                               exchange_date=date(2026, 12, 25), redraw=True)
    assert result.startswith(texts.REDRAW_PREFIX) and "«не написаны — можно спросить анонимно»" in result
    assert texts.draw_summary(sent=7, total=8, failed_names=["Иван"]).startswith(
        "Готово! Пары отправлены 7 из 8. Не дошло: Иван")


def test_panel_and_waiting() -> None:
    panel = texts.panel(title="Офис", code="ABC234", active=10, limit=10, waiting=2, without_wishes=3, exclusions=1,
                        budget="до 1000 ₽", exchange_date=date(2026, 12, 25))
    assert "Участников: 10 из 10; в очереди: 2" in panel and "Обмен: 25 декабря" in panel
    assert "до 30 человек может любой участник — 490 ₽" in texts.waiting_list(free_limit=10, limit=30, price=490)


def test_every_date_error_has_a_message() -> None:
    assert set(texts.DATE_ERRORS) == set(DateError)


def test_payment_description_fits_robokassa_rules() -> None:
    description = texts.payment_description(300)
    assert len(description) <= 100 and not set(description) & set("\"'«»<>&")


def test_button_labels_fit() -> None:
    labels = [value for name, value in vars(texts).items() if name.startswith("BTN_") and isinstance(value, str)]
    labels += list(texts.BUDGET_PRESETS)
    assert labels and all(len(label) <= 40 for label in labels)
