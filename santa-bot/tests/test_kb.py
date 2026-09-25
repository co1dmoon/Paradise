from __future__ import annotations

import pytest

from app.core import texts
from app.core.kb import (
    MAX_BUTTON_TEXT,
    CallbackButton,
    callback,
    cb,
    keyboard,
    link,
    paged_keyboard,
    paginate,
    split_cb,
)


def test_long_button_text_is_shortened() -> None:
    button = callback("Очень длинное имя участника, которое не влезает в кнопку", "x")
    assert len(button.text) == MAX_BUTTON_TEXT and button.text.endswith("…")


@pytest.mark.parametrize("payload", ["", "x" * 65, "имя"])
def test_bad_callback_payload_raises(payload: str) -> None:
    with pytest.raises(ValueError):
        callback("ok", payload)


def test_link_requires_http_url() -> None:
    assert link("Оплатить", "https://auth.robokassa.ru/x").url.startswith("https://")
    with pytest.raises(ValueError):
        link("x", "javascript:alert(1)")


def test_keyboard_limits() -> None:
    button = callback("a", "a")
    keyboard(*([[button, button, button]] * 8))
    with pytest.raises(ValueError):
        keyboard(*([[button]] * 9))
    with pytest.raises(ValueError):
        keyboard([button] * 4)


def test_keyboard_accepts_single_buttons_and_drops_empty_rows() -> None:
    board = keyboard(callback("a", "a"), [], [callback("b", "b"), callback("c", "c")])
    assert [[b.text for b in row] for row in board.rows] == [["a"], ["b", "c"]]


def test_callback_data_helpers() -> None:
    assert cb("pay", "ABC234", "S") == "pay:ABC234:S"
    assert split_cb("pay:ABC234:S") == ["pay", "ABC234", "S"]


def test_paginate() -> None:
    items = list(range(20))
    first = paginate(items, 0)
    assert first.items == tuple(range(8)) and first.total_pages == 3 and first.has_next and not first.has_previous
    last = paginate(items, 99)
    assert last.number == 2 and last.items == (16, 17, 18, 19) and last.has_previous and not last.has_next
    assert paginate([], 0).items == () and paginate([], 0).total_pages == 1


def test_paged_keyboard_stays_within_limits() -> None:
    buttons = [callback(f"Участник {i}", f"pick:{i}") for i in range(20)]
    board = paged_keyboard(buttons, 1, lambda page: f"page:{page}", footer=[callback(texts.BTN_CANCEL, "cancel")])
    texts_by_row = [[b.text for b in row] for row in board.rows]
    assert texts_by_row[0] == ["Участник 8", "Участник 9"]
    assert texts_by_row[4] == [texts.BTN_PREVIOUS_PAGE, texts.BTN_NEXT_PAGE]
    assert texts_by_row[-1] == [texts.BTN_CANCEL]
    nav = board.rows[4]
    assert isinstance(nav[0], CallbackButton) and nav[0].payload == "page:0"
    assert len(board.rows) <= 8
