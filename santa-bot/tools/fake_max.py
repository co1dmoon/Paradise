"""FakeMaxApi: an in-memory MAX Bot API for offline tests and the simulator (§14).

It records every send, edit and answer per target (``timeline`` keeps every
visible change in order) and offers helpers to read what a user saw and to
build the updates MAX would deliver:

    api = FakeMaxApi()
    olga = FakeUser(api, 101, "Ольга")
    await process_update(ctx, olga.start("j_ABC234"))
    await process_update(ctx, olga.press("Согласен"))
    assert "Вы в игре" in api.last_text(101)

Failure injection: ``block_user`` (Forbidden on sends), ``fail_next`` (any error
for the next calls) and ``reject_notifications`` (MAX refusing the undocumented
``notification`` field of /answers).
"""

from __future__ import annotations

import asyncio
import itertools
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from app.core.clock import Clock
from app.core.kb import Button, CallbackButton, LinkButton
from app.max_api import (
    BadRequest,
    BotInfo,
    ChatInfo,
    Forbidden,
    MaxApiError,
    OutMessage,
    Subscription,
    Target,
    UpdatesPage,
)

BOT_USER_ID = 1
BOT_USERNAME = "santa_test_bot"
DIALOG_CHAT_OFFSET = 900_000_000

_ids = itertools.count(1)


def _next_id() -> int:
    return next(_ids)


def dialog_chat_id(user_id: int) -> int:
    """The fake chat id of the private dialog between the bot and ``user_id``."""
    return DIALOG_CHAT_OFFSET + user_id


# --- recorded calls ----------------------------------------------------------------------------


@dataclass(slots=True)
class SentMessage:
    mid: str
    target: Target
    message: OutMessage
    disable_link_preview: bool
    at: float
    edits: list[OutMessage] = field(default_factory=list)
    touched: int = 0  # order of the last send or edit, across all messages

    @property
    def current(self) -> OutMessage:
        """The message as the user sees it now (after edits)."""
        return self.edits[-1] if self.edits else self.message

    @property
    def text(self) -> str:
        return self.current.text

    @property
    def buttons(self) -> list[Button]:
        keyboard = self.current.keyboard
        return keyboard.buttons() if keyboard else []


@dataclass(frozen=True, slots=True)
class Delivery:
    """One visible change in a chat, in order: a new message, or a message replaced by an edit."""

    record: SentMessage
    body: OutMessage
    edited: bool


@dataclass(frozen=True, slots=True)
class RecordedAnswer:
    callback_id: str
    notification: str | None
    message: OutMessage | None
    at: float


@dataclass(frozen=True, slots=True)
class RecordedCall:
    """One API call in order, for rate-limit assertions: kind is send, edit or answer."""

    kind: str
    target_key: str
    at: float


class FakeMaxApi:
    def __init__(self, *, clock: Clock | None = None, bot_username: str = BOT_USERNAME) -> None:
        self._clock = clock
        self.bot = BotInfo(BOT_USER_ID, bot_username, "Санта в чате")
        self.sent: list[SentMessage] = []
        self.timeline: list[Delivery] = []
        self.answers: list[RecordedAnswer] = []
        self.calls: list[RecordedCall] = []
        self.subscriptions: list[Subscription] = []
        self.chats: dict[int, ChatInfo] = {}
        self.pending_updates: list[dict[str, Any]] = []
        self.poll_wait = 0.01
        self.reject_notifications = False
        self._blocked_users: set[int] = set()
        self._failures: list[MaxApiError] = []
        self._by_mid: dict[str, SentMessage] = {}
        self._callback_targets: dict[str, Target] = {}
        self._callback_mids: dict[str, str] = {}
        self._touches = itertools.count(1)

    # --- failure injection -------------------------------------------------------------------

    def block_user(self, user_id: int) -> None:
        """Sends to this user fail with Forbidden, as when they blocked the bot."""
        self._blocked_users.add(user_id)

    def unblock_user(self, user_id: int) -> None:
        self._blocked_users.discard(user_id)

    def fail_next(self, error: MaxApiError, times: int = 1) -> None:
        """The next ``times`` send/edit calls raise ``error``."""
        self._failures.extend([error] * times)

    # --- MaxApi ------------------------------------------------------------------------------------

    async def get_me(self) -> BotInfo:
        return self.bot

    async def send(self, target: Target, message: OutMessage, disable_link_preview: bool = False) -> str | None:
        self._raise_injected()
        if target.kind == "user" and target.id in self._blocked_users:
            raise Forbidden(403, "chat.denied: user blocked the bot")
        mid = f"mid.{_next_id():06d}"
        record = SentMessage(mid, target, message, disable_link_preview, self._now(), touched=next(self._touches))
        self.sent.append(record)
        self.timeline.append(Delivery(record, message, edited=False))
        self._by_mid[mid] = record
        self.calls.append(RecordedCall("send", target.key, record.at))
        return mid

    async def edit(self, message_id: str, message: OutMessage) -> None:
        self._raise_injected()
        record = self._by_mid.get(message_id)
        if record is None:
            raise Forbidden(404, "message.not.found")
        record.edits.append(message)
        record.touched = next(self._touches)
        self.timeline.append(Delivery(record, message, edited=True))
        self.calls.append(RecordedCall("edit", record.target.key, self._now()))

    async def answer(
        self, callback_id: str, notification: str | None = None, message: OutMessage | None = None
    ) -> bool:
        target = self._callback_targets.get(callback_id)
        self.calls.append(RecordedCall("answer", target.key if target else "?", self._now()))
        shown = notification is not None and not self.reject_notifications
        self.answers.append(RecordedAnswer(callback_id, notification if shown else None, message, self._now()))
        mid = self._callback_mids.get(callback_id)
        if message is not None and mid in self._by_mid:
            record = self._by_mid[mid]
            record.edits.append(message)
            record.touched = next(self._touches)
            self.timeline.append(Delivery(record, message, edited=True))
        return notification is None or shown

    async def get_chat(self, chat_id: int) -> ChatInfo:
        if chat_id not in self.chats:
            raise Forbidden(404, "chat.not.found")
        return self.chats[chat_id]

    async def list_subscriptions(self) -> list[Subscription]:
        return list(self.subscriptions)

    async def subscribe(self, url: str, types: Sequence[str], secret: str) -> None:
        if not url.startswith("https://"):
            raise BadRequest(400, "url must be https")
        self.subscriptions = [s for s in self.subscriptions if s.url != url] + [Subscription(url, tuple(types))]

    async def get_updates(self, marker: int | None, timeout: int) -> UpdatesPage:
        """Long polling: an empty page comes back after ``poll_wait`` real seconds, not at once."""
        if not self.pending_updates:
            await asyncio.sleep(self.poll_wait)
        updates, self.pending_updates = self.pending_updates, []
        return UpdatesPage(updates, (marker or 0) + len(updates))

    async def close(self) -> None:
        """Nothing to release."""

    # --- reading what users saw -----------------------------------------------------------------

    def messages_to(self, user_id: int) -> list[SentMessage]:
        return [m for m in self.sent if m.target == Target.user(user_id)]

    def messages_in_chat(self, chat_id: int) -> list[SentMessage]:
        return [m for m in self.sent if m.target == Target.chat(chat_id)]

    def texts_to(self, user_id: int) -> list[str]:
        return [m.text for m in self.messages_to(user_id)]

    def last_to(self, user_id: int) -> SentMessage:
        messages = self.messages_to(user_id)
        if not messages:
            raise AssertionError(f"nothing was sent to user {user_id}")
        return messages[-1]

    def last_text(self, user_id: int) -> str:
        return self.last_to(user_id).text

    def screen(self, user_id: int) -> SentMessage:
        """The message the user saw change last: newly sent, or replaced by an edit or answer."""
        messages = self.messages_to(user_id)
        if not messages:
            raise AssertionError(f"nothing was sent to user {user_id}")
        return max(messages, key=lambda message: message.touched)

    def buttons(self, user_id: int) -> list[Button]:
        """Buttons of the newest message to the user that has a keyboard."""
        for message in reversed(self.messages_to(user_id)):
            if message.buttons:
                return message.buttons
        return []

    def button_texts(self, user_id: int) -> list[str]:
        return [button.text for button in self.buttons(user_id)]

    def find_button(self, user_id: int, text: str) -> tuple[SentMessage, Button]:
        """The newest button whose text contains ``text`` (case-insensitive), and its message."""
        needle = text.lower()
        for message in reversed(self.messages_to(user_id)):
            for button in message.buttons:
                if needle in button.text.lower():
                    return message, button
        raise AssertionError(f"no button containing {text!r} was sent to user {user_id}")

    def link_url(self, user_id: int, text: str) -> str:
        _, button = self.find_button(user_id, text)
        if not isinstance(button, LinkButton):
            raise AssertionError(f"button {button.text!r} is not a link")
        return button.url

    def press(self, user_id: int, text: str, *, name: str = "Участник") -> dict[str, Any]:
        """Build the message_callback update for pressing the button labelled ``text``."""
        message, button = self.find_button(user_id, text)
        if not isinstance(button, CallbackButton):
            raise AssertionError(f"button {button.text!r} is a link, not a callback")
        return self._callback(user_id, button.payload, name, message)

    def forge(self, user_id: int, payload: str, *, name: str = "Участник") -> dict[str, Any]:
        """A callback with any payload, as if pressed on the user's latest screen (stale or tampered buttons)."""
        return self._callback(user_id, payload, name, self.screen(user_id))

    def _callback(self, user_id: int, payload: str, name: str, message: SentMessage) -> dict[str, Any]:
        update = message_callback(user_id, payload, name=name, message_mid=message.mid)
        callback_id = update["callback"]["callback_id"]
        self._callback_targets[callback_id] = Target.user(user_id)
        self._callback_mids[callback_id] = message.mid
        return update

    def answered(self, callback_id: str) -> bool:
        return any(answer.callback_id == callback_id for answer in self.answers)

    def clear(self) -> None:
        """Forget recorded messages and answers (not failures or subscriptions)."""
        self.sent.clear()
        self.timeline.clear()
        self.answers.clear()
        self.calls.clear()
        self._by_mid.clear()

    # --- internals ------------------------------------------------------------------------------

    def _raise_injected(self) -> None:
        if self._failures:
            raise self._failures.pop(0)

    def _now(self) -> float:
        return self._clock.monotonic() if self._clock else 0.0


# --- update builders ------------------------------------------------------------------------------


def user_json(user_id: int, name: str, username: str | None = None, *, is_bot: bool = False) -> dict[str, Any]:
    first, _, last = name.partition(" ")
    data: dict[str, Any] = {"user_id": user_id, "first_name": first, "name": name, "is_bot": is_bot}
    if last:
        data["last_name"] = last
    if username:
        data["username"] = username
    return data


def bot_started(user_id: int, payload: str | None = None, *, name: str = "Участник",
                username: str | None = None) -> dict[str, Any]:
    update: dict[str, Any] = {
        "update_type": "bot_started",
        "timestamp": _next_id(),
        "chat_id": dialog_chat_id(user_id),
        "user": user_json(user_id, name, username),
        "user_locale": "ru",
    }
    if payload is not None:
        update["payload"] = payload
    return update


def message_created(
    user_id: int,
    text: str,
    *,
    name: str = "Участник",
    chat_id: int | None = None,
    chat_type: str = "dialog",
    attachments: list[dict[str, Any]] | None = None,
    is_bot: bool = False,
) -> dict[str, Any]:
    return {
        "update_type": "message_created",
        "timestamp": _next_id(),
        "message": {
            "sender": user_json(user_id, name, is_bot=is_bot),
            "recipient": {"chat_id": chat_id or dialog_chat_id(user_id), "chat_type": chat_type},
            "timestamp": _next_id(),
            "body": {"mid": f"umid.{_next_id():06d}", "seq": _next_id(), "text": text,
                     "attachments": attachments or []},
        },
        "user_locale": "ru",
    }


def message_callback(
    user_id: int, payload: str, *, name: str = "Участник", message_mid: str | None = None
) -> dict[str, Any]:
    return {
        "update_type": "message_callback",
        "timestamp": _next_id(),
        "callback": {
            "timestamp": _next_id(),
            "callback_id": f"cb.{_next_id():06d}",
            "payload": payload,
            "user": user_json(user_id, name),
        },
        "message": {
            "sender": user_json(BOT_USER_ID, "Санта в чате", BOT_USERNAME, is_bot=True),
            "recipient": {"chat_id": dialog_chat_id(user_id), "chat_type": "dialog", "user_id": user_id},
            "body": {"mid": message_mid or f"mid.{_next_id():06d}", "text": ""},
        },
        "user_locale": "ru",
    }


def bot_added(chat_id: int, user_id: int, *, name: str = "Участник", is_channel: bool = False) -> dict[str, Any]:
    return {"update_type": "bot_added", "timestamp": _next_id(), "chat_id": chat_id,
            "user": user_json(user_id, name), "is_channel": is_channel}


def bot_removed(chat_id: int, user_id: int, *, name: str = "Участник") -> dict[str, Any]:
    return {"update_type": "bot_removed", "timestamp": _next_id(), "chat_id": chat_id,
            "user": user_json(user_id, name), "is_channel": False}


def bot_stopped(user_id: int, *, name: str = "Участник") -> dict[str, Any]:
    return {"update_type": "bot_stopped", "timestamp": _next_id(), "chat_id": dialog_chat_id(user_id),
            "user": user_json(user_id, name)}


@dataclass(slots=True)
class FakeUser:
    """A person talking to the bot: builds their updates and reads what they received."""

    api: FakeMaxApi
    user_id: int
    name: str
    username: str | None = None

    def start(self, payload: str | None = None) -> dict[str, Any]:
        return bot_started(self.user_id, payload, name=self.name, username=self.username)

    def say(self, text: str) -> dict[str, Any]:
        return message_created(self.user_id, text, name=self.name)

    def press(self, button_text: str) -> dict[str, Any]:
        return self.api.press(self.user_id, button_text, name=self.name)

    @property
    def last_text(self) -> str:
        return self.api.last_text(self.user_id)

    @property
    def screen_text(self) -> str:
        """Text of the message that changed last (a new message or a replaced screen)."""
        return self.api.screen(self.user_id).text

    @property
    def texts(self) -> list[str]:
        return self.api.texts_to(self.user_id)

    @property
    def button_texts(self) -> list[str]:
        return self.api.button_texts(self.user_id)
