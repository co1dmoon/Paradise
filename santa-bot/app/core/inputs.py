"""Validation of user-written text (§11): length limits and control-character stripping."""

from __future__ import annotations

import re
import unicodedata

MAX_TITLE = 60
MAX_NAME = 40
MAX_WISHES = 1000
MAX_RELAY = 500
MAX_BUDGET = 30

_SPACES = re.compile(r"[ \t]+")
_BLANK_LINES = re.compile(r"\n{3,}")
_ZWJ = "‍"  # a format character, but needed to keep composite emoji intact


def clean_text(text: str, *, multiline: bool = False) -> str:
    """Strip control and format characters, collapse spaces, trim.

    Newlines survive only when ``multiline`` is set (wishes, relays). Nothing is
    interpreted as markup: the bot sends plain text only.
    """
    text = unicodedata.normalize("NFC", text.replace("\r\n", "\n").replace("\r", "\n"))
    kept = []
    for char in text:
        if char == "\n":
            kept.append("\n" if multiline else " ")
        elif char == "\t":
            kept.append(" ")
        elif char == _ZWJ or unicodedata.category(char) not in ("Cc", "Cf", "Cs", "Co", "Cn"):
            kept.append(char)
    lines = [_SPACES.sub(" ", line).strip() for line in "".join(kept).split("\n")]
    return _BLANK_LINES.sub("\n\n", "\n".join(lines)).strip()


def shorten(text: str, limit: int) -> str:
    """Cut ``text`` to ``limit`` characters, ending with '…' when something was cut."""
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def clean_limited(text: str, limit: int, *, multiline: bool = False) -> str | None:
    """Cleaned text, or None when it is empty or longer than ``limit`` characters."""
    cleaned = clean_text(text, multiline=multiline)
    if not cleaned or len(cleaned) > limit:
        return None
    return cleaned
