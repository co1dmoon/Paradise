"""Platform-neutral inline keyboards (§2 limits, §5 pagination).

Handlers build ``Keyboard`` objects from these specs; ``app.max_api`` renders them
for MAX. Limits are conservative because MAX does not document them:
3 buttons per row, 8 rows, 40 characters of button text, 64 ASCII characters
of callback payload. Button text that is too long (e.g. a participant's name)
is shortened; a bad payload or layout is a programming error and raises.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from app.core import texts
from app.core.inputs import shorten

MAX_BUTTONS_PER_ROW = 3
MAX_ROWS = 8
MAX_BUTTON_TEXT = 40
MAX_CALLBACK_PAYLOAD = 64
PAGE_SIZE = 8
CALLBACK_SEPARATOR = ":"

T = TypeVar("T")


@dataclass(frozen=True, slots=True)
class CallbackButton:
    text: str
    payload: str


@dataclass(frozen=True, slots=True)
class LinkButton:
    text: str
    url: str


Button = CallbackButton | LinkButton


@dataclass(frozen=True, slots=True)
class Keyboard:
    rows: tuple[tuple[Button, ...], ...]

    def buttons(self) -> list[Button]:
        return [button for row in self.rows for button in row]


def callback(text: str, payload: str) -> CallbackButton:
    if not payload or len(payload) > MAX_CALLBACK_PAYLOAD or not payload.isascii():
        raise ValueError(f"callback payload must be 1-{MAX_CALLBACK_PAYLOAD} ASCII chars: {payload!r}")
    return CallbackButton(shorten(text, MAX_BUTTON_TEXT), payload)


def link(text: str, url: str) -> LinkButton:
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"link button needs an http(s) URL: {url!r}")
    return LinkButton(shorten(text, MAX_BUTTON_TEXT), url)


def keyboard(*rows: Sequence[Button] | Button) -> Keyboard:
    """Build a keyboard; each argument is a row (a single button is a one-button row)."""
    normalized = tuple(
        (row,) if isinstance(row, (CallbackButton, LinkButton)) else tuple(row) for row in rows
    )
    normalized = tuple(row for row in normalized if row)
    if len(normalized) > MAX_ROWS:
        raise ValueError(f"keyboard has {len(normalized)} rows, max {MAX_ROWS}")
    for row in normalized:
        if len(row) > MAX_BUTTONS_PER_ROW:
            raise ValueError(f"keyboard row has {len(row)} buttons, max {MAX_BUTTONS_PER_ROW}")
    return Keyboard(normalized)


def cb(*parts: str | int) -> str:
    """Callback payload from parts, e.g. cb('pay', 'ABC123', 'S') -> 'pay:ABC123:S'."""
    return CALLBACK_SEPARATOR.join(str(part) for part in parts)


def split_cb(payload: str) -> list[str]:
    return payload.split(CALLBACK_SEPARATOR)


@dataclass(frozen=True, slots=True)
class Page(Generic[T]):
    items: tuple[T, ...]
    number: int
    total_pages: int

    @property
    def has_previous(self) -> bool:
        return self.number > 0

    @property
    def has_next(self) -> bool:
        return self.number < self.total_pages - 1


def paginate(items: Sequence[T], page: int, per_page: int = PAGE_SIZE) -> Page[T]:
    """Zero-based page ``page`` of ``items``; an out-of-range page is clamped."""
    total_pages = max(1, -(-len(items) // per_page))
    number = min(max(page, 0), total_pages - 1)
    start = number * per_page
    return Page(tuple(items[start : start + per_page]), number, total_pages)


def paged_keyboard(
    buttons: Sequence[Button],
    page: int,
    nav_payload: Callable[[int], str],
    *,
    per_row: int = 2,
    footer: Sequence[Sequence[Button] | Button] = (),
) -> Keyboard:
    """Eight item buttons per page (``per_row`` per row), a Назад/Дальше row, then ``footer`` rows."""
    current = paginate(buttons, page)
    rows: list[Sequence[Button] | Button] = [
        current.items[i : i + per_row] for i in range(0, len(current.items), per_row)
    ]
    nav = []
    if current.has_previous:
        nav.append(callback(texts.BTN_PREVIOUS_PAGE, nav_payload(current.number - 1)))
    if current.has_next:
        nav.append(callback(texts.BTN_NEXT_PAGE, nav_payload(current.number + 1)))
    rows.append(nav)
    rows.extend(footer)
    return keyboard(*rows)
