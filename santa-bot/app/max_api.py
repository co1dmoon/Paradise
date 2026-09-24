"""MAX Bot API: the ``MaxApi`` interface, its HTTP implementation and update parsing (§2).

Checked against dev.max.ru on 2026-09-24:

- Base URL https://platform-api2.max.ru, header ``Authorization: <token>``.
- POST /messages?user_id=|chat_id=[&disable_link_preview=true] with body
  {text, attachments, notify}; the reply is {message: {body: {mid, ...}, ...}}.
- PUT /messages?message_id=… and POST /answers?callback_id=… reply HTTP 200 with
  {success: bool, message?: str}: ``success: false`` is an error even with 200.
- POST /answers documents body fields ``message`` (replaces the message with the
  button) and ``disable_link_preview``; ``notification`` is NOT documented, so
  ``answer`` falls back to answering without it (see ``HttpMaxApi.answer``).
- POST /subscriptions {url, update_types, secret}; the secret must match
  ^[a-zA-Z0-9_-]{5,256}$. GET /subscriptions → {subscriptions: [{url, time, update_types}]}.
- GET /updates?limit=1..1000&timeout=0..90&marker= → {updates: [...], marker}.
- Button limits are not documented; ``app.core.kb`` enforces conservative ones.

Responses are parsed leniently: unknown fields are ignored and unexpected shapes
never raise outside the documented error classes.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import ssl
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal, Protocol

import aiohttp

from app.core.inputs import shorten
from app.core.kb import CallbackButton, Keyboard, LinkButton
from app.core.texts import MAX_MESSAGE

log = logging.getLogger(__name__)

UPDATE_TYPES: tuple[str, ...] = (
    "bot_started",
    "bot_added",
    "bot_removed",
    "bot_stopped",
    "message_created",
    "message_callback",
)
DEFAULT_TIMEOUT = 30.0

# --- errors ----------------------------------------------------------------------------------


class MaxApiError(Exception):
    def __init__(self, status: int, detail: str) -> None:
        super().__init__(f"HTTP {status}: {detail}")
        self.status = status
        self.detail = detail


class RateLimited(MaxApiError):
    """HTTP 429."""


class Transient(MaxApiError):
    """5xx, timeouts and connection failures: worth retrying."""


class Unauthorized(MaxApiError):
    """HTTP 401: the token is wrong or revoked."""


class Forbidden(MaxApiError):
    """The user blocked the bot, the chat is gone, access denied."""


class BadRequest(MaxApiError):
    """Any other 4xx: our request is wrong; retrying will not help."""


_FORBIDDEN_WORDS = re.compile(r"denied|blocked|forbidden|not[\s._-]?found", re.IGNORECASE)


def classify_error(status: int, detail: str) -> MaxApiError:
    """Map an HTTP status and error text to the error classes of §2."""
    detail = shorten(detail.strip(), 300)
    if status == 429:
        return RateLimited(status, detail)
    if status >= 500:
        return Transient(status, detail)
    if status == 401:
        return Unauthorized(status, detail)
    if status == 403 or _FORBIDDEN_WORDS.search(detail):
        return Forbidden(status, detail)
    return BadRequest(status, detail)


# --- outgoing ------------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class Target:
    kind: Literal["user", "chat"]
    id: int

    @classmethod
    def user(cls, user_id: int) -> Target:
        return cls("user", user_id)

    @classmethod
    def chat(cls, chat_id: int) -> Target:
        return cls("chat", chat_id)

    @property
    def key(self) -> str:
        """Rate-limiter key: one per dialog or chat."""
        return f"{self.kind}:{self.id}"

    def query(self) -> dict[str, int]:
        return {"user_id" if self.kind == "user" else "chat_id": self.id}


@dataclass(frozen=True, slots=True)
class OutMessage:
    """Plain text (no ``format``, §2) plus an optional inline keyboard."""

    text: str
    keyboard: Keyboard | None = None
    notify: bool = True

    def to_max(self) -> dict[str, Any]:
        body: dict[str, Any] = {"text": shorten(self.text, MAX_MESSAGE), "notify": self.notify}
        body["attachments"] = [_render_keyboard(self.keyboard)] if self.keyboard else []
        return body

    @classmethod
    def from_max(cls, body: dict[str, Any]) -> OutMessage:
        """Inverse of ``to_max`` (the outbox stores bodies as MAX JSON)."""
        keyboard = None
        for attachment in body.get("attachments") or []:
            if attachment.get("type") == "inline_keyboard":
                keyboard = _parse_keyboard(attachment.get("payload", {}).get("buttons", []))
        return cls(str(body.get("text", "")), keyboard, bool(body.get("notify", True)))


def _render_keyboard(keyboard: Keyboard) -> dict[str, Any]:
    rows = [
        [
            {"type": "callback", "text": b.text, "payload": b.payload}
            if isinstance(b, CallbackButton)
            else {"type": "link", "text": b.text, "url": b.url}
            for b in row
        ]
        for row in keyboard.rows
    ]
    return {"type": "inline_keyboard", "payload": {"buttons": rows}}


def _parse_keyboard(rows: list[list[dict[str, Any]]]) -> Keyboard:
    parsed = tuple(
        tuple(
            CallbackButton(b["text"], b["payload"]) if b.get("type") == "callback" else LinkButton(b["text"], b["url"])
            for b in row
        )
        for row in rows
    )
    return Keyboard(parsed)


@dataclass(frozen=True, slots=True)
class BotInfo:
    user_id: int
    username: str | None
    name: str | None


@dataclass(frozen=True, slots=True)
class ChatInfo:
    chat_id: int
    type: str | None
    title: str | None
    status: str | None


@dataclass(frozen=True, slots=True)
class Subscription:
    url: str
    update_types: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpdatesPage:
    updates: list[dict[str, Any]]
    marker: int | None


class MaxApi(Protocol):
    async def get_me(self) -> BotInfo: ...

    async def send(self, target: Target, message: OutMessage, disable_link_preview: bool = False) -> str | None:
        """Send a message; returns its mid when MAX reports one."""

    async def edit(self, message_id: str, message: OutMessage) -> None: ...

    async def answer(
        self, callback_id: str, notification: str | None = None, message: OutMessage | None = None
    ) -> bool:
        """Answer a callback. Returns False if ``notification`` could not be shown."""

    async def get_chat(self, chat_id: int) -> ChatInfo: ...

    async def list_subscriptions(self) -> list[Subscription]: ...

    async def subscribe(self, url: str, types: Sequence[str], secret: str) -> None: ...

    async def get_updates(self, marker: int | None, timeout: int) -> UpdatesPage: ...

    async def close(self) -> None: ...


class HttpMaxApi:
    def __init__(
        self, token: str, base_url: str, ssl_context: ssl.SSLContext, *, timeout: float = DEFAULT_TIMEOUT
    ) -> None:
        self._token = token
        self._base_url = base_url.rstrip("/")
        self._ssl = ssl_context
        self._timeout = timeout
        self._session: aiohttp.ClientSession | None = None
        self._notification_supported = True

    async def close(self) -> None:
        if self._session is not None:
            await self._session.close()
            self._session = None

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=self._ssl, limit=30),
                headers={"Authorization": self._token},
            )
        return self._session

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        query = {key: _query_value(value) for key, value in (params or {}).items() if value is not None}
        try:
            async with self._get_session().request(
                method,
                self._base_url + path,
                params=query,
                json=body,
                timeout=aiohttp.ClientTimeout(total=timeout or self._timeout),
            ) as response:
                status, text = response.status, await response.text()
        except asyncio.TimeoutError as error:
            raise Transient(0, f"timeout on {method} {path}") from error
        except aiohttp.ClientError as error:
            raise Transient(0, f"{type(error).__name__} on {method} {path}") from error
        data = _lenient_json(text)
        if status >= 400:
            raise classify_error(status, str(data.get("message") or data.get("code") or text))
        if data.get("success") is False:
            raise classify_error(400, str(data.get("message") or "success=false"))
        return data

    async def get_me(self) -> BotInfo:
        data = await self._request("GET", "/me")
        return BotInfo(_int(data.get("user_id")) or 0, _str(data.get("username")), _str(data.get("name")))

    async def send(self, target: Target, message: OutMessage, disable_link_preview: bool = False) -> str | None:
        params: dict[str, Any] = dict(target.query())
        if disable_link_preview:
            params["disable_link_preview"] = True
        data = await self._request("POST", "/messages", params=params, body=message.to_max())
        sent = data.get("message")
        body = sent.get("body") if isinstance(sent, dict) else None
        return _str(body.get("mid")) if isinstance(body, dict) else None

    async def edit(self, message_id: str, message: OutMessage) -> None:
        await self._request("PUT", "/messages", params={"message_id": message_id}, body=message.to_max())

    async def answer(
        self, callback_id: str, notification: str | None = None, message: OutMessage | None = None
    ) -> bool:
        """POST /answers. ``notification`` is undocumented: if MAX rejects it (BadRequest),
        answer again without it and remember not to try again; the caller then sends a
        normal message instead."""
        body: dict[str, Any] = {} if message is None else {"message": message.to_max()}
        params = {"callback_id": callback_id}
        if notification and self._notification_supported:
            try:
                await self._request("POST", "/answers", params=params, body={**body, "notification": notification})
                return True
            except BadRequest:
                log.warning("callback notification rejected; falling back to plain answers")
                self._notification_supported = False
        await self._request("POST", "/answers", params=params, body=body)
        return notification is None

    async def get_chat(self, chat_id: int) -> ChatInfo:
        data = await self._request("GET", f"/chats/{chat_id}")
        return ChatInfo(
            _int(data.get("chat_id")) or chat_id, _str(data.get("type")), _str(data.get("title")),
            _str(data.get("status")),
        )

    async def list_subscriptions(self) -> list[Subscription]:
        data = await self._request("GET", "/subscriptions")
        items = data.get("subscriptions")
        return [
            Subscription(str(item.get("url", "")), tuple(str(t) for t in item.get("update_types") or ()))
            for item in (items if isinstance(items, list) else [])
            if isinstance(item, dict)
        ]

    async def subscribe(self, url: str, types: Sequence[str], secret: str) -> None:
        await self._request("POST", "/subscriptions", body={"url": url, "update_types": list(types), "secret": secret})

    async def get_updates(self, marker: int | None, timeout: int) -> UpdatesPage:
        timeout = max(0, min(90, timeout))
        data = await self._request(
            "GET",
            "/updates",
            params={"marker": marker, "timeout": timeout, "limit": 100},
            timeout=timeout + 15,
        )
        updates = data.get("updates")
        return UpdatesPage(
            [u for u in updates if isinstance(u, dict)] if isinstance(updates, list) else [],
            _int(data.get("marker")),
        )


async def ensure_subscription(api: MaxApi, url: str, secret: str) -> bool:
    """Make sure our webhook subscription exists. Returns True when it had to be (re)created."""
    if any(subscription.url == url for subscription in await api.list_subscriptions()):
        return False
    await api.subscribe(url, UPDATE_TYPES, secret)
    return True


def _query_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _lenient_json(text: str) -> dict[str, Any]:
    try:
        data = json.loads(text) if text else {}
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.lstrip("-").isdigit():
        return int(value)
    return None


def _str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


# --- incoming updates -------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class UserRef:
    user_id: int
    name: str | None
    username: str | None
    is_bot: bool

    @classmethod
    def parse(cls, raw: Any) -> UserRef | None:
        data = _dict(raw)
        user_id = _int(data.get("user_id"))
        if user_id is None:
            return None
        full = " ".join(part for part in (_str(data.get("first_name")), _str(data.get("last_name"))) if part)
        return cls(user_id, _str(data.get("name")) or full or None, _str(data.get("username")),
                   bool(data.get("is_bot")))


@dataclass(frozen=True, slots=True)
class BotStarted:
    user: UserRef
    chat_id: int | None
    payload: str | None
    timestamp: int


@dataclass(frozen=True, slots=True)
class MessageCreated:
    sender: UserRef
    chat_id: int | None
    chat_type: str
    text: str
    mid: str | None
    has_attachments: bool
    timestamp: int

    @property
    def is_private(self) -> bool:
        return self.chat_type == "dialog"


@dataclass(frozen=True, slots=True)
class CallbackQuery:
    callback_id: str
    payload: str
    user: UserRef
    message_mid: str | None
    chat_id: int | None
    chat_type: str | None
    timestamp: int


@dataclass(frozen=True, slots=True)
class BotAdded:
    chat_id: int
    user: UserRef | None
    is_channel: bool
    timestamp: int


@dataclass(frozen=True, slots=True)
class BotRemoved:
    chat_id: int
    user: UserRef | None
    timestamp: int


@dataclass(frozen=True, slots=True)
class BotStopped:
    chat_id: int | None
    user: UserRef | None
    timestamp: int


Update = BotStarted | MessageCreated | CallbackQuery | BotAdded | BotRemoved | BotStopped


@dataclass(frozen=True, slots=True)
class _Message:
    sender: UserRef | None
    chat_id: int | None
    chat_type: str | None
    text: str
    mid: str | None
    has_attachments: bool = False

    @classmethod
    def parse(cls, raw: Any) -> _Message:
        data = _dict(raw)
        recipient, body = _dict(data.get("recipient")), _dict(data.get("body"))
        attachments, text = body.get("attachments"), body.get("text")
        return cls(
            UserRef.parse(data.get("sender")),
            _int(recipient.get("chat_id")),
            _str(recipient.get("chat_type")),
            text if isinstance(text, str) else "",
            _str(body.get("mid")),
            bool(attachments) and isinstance(attachments, list),
        )


def parse_update(raw: Any) -> Update | None:
    """Typed view of a raw update; None for other types or unusable shapes."""
    data = _dict(raw)
    kind = data.get("update_type")
    timestamp = _int(data.get("timestamp")) or 0
    user = UserRef.parse(data.get("user"))
    chat_id = _int(data.get("chat_id"))
    if kind == "bot_started" and user is not None:
        return BotStarted(user, chat_id, _str(data.get("payload")), timestamp)
    if kind == "message_created":
        message = _Message.parse(data.get("message"))
        if message.sender is None:
            return None
        return MessageCreated(
            message.sender, message.chat_id, message.chat_type or "dialog", message.text, message.mid,
            message.has_attachments, timestamp,
        )
    if kind == "message_callback":
        callback = _dict(data.get("callback"))
        callback_user = UserRef.parse(callback.get("user"))
        callback_id = _str(callback.get("callback_id"))
        if callback_user is None or callback_id is None:
            return None
        message = _Message.parse(data.get("message"))
        payload = callback.get("payload")
        return CallbackQuery(
            callback_id, payload if isinstance(payload, str) else "", callback_user, message.mid,
            message.chat_id, message.chat_type, timestamp,
        )
    if kind == "bot_added" and chat_id is not None:
        return BotAdded(chat_id, user, bool(data.get("is_channel")), timestamp)
    if kind == "bot_removed" and chat_id is not None:
        return BotRemoved(chat_id, user, timestamp)
    if kind == "bot_stopped":
        return BotStopped(chat_id, user, timestamp)
    return None


def dedupe_key(update: Update) -> str:
    """processed_updates key (§3): callback_id, message mid, or 'bs:'+user+':'+timestamp."""
    match update:
        case CallbackQuery(callback_id=callback_id):
            return callback_id
        case MessageCreated(mid=str(mid)):
            return mid
        case MessageCreated(sender=sender, timestamp=ts):
            return f"mc:{sender.user_id}:{ts}"
        case BotStarted(user=user, timestamp=ts):
            return f"bs:{user.user_id}:{ts}"
        case BotAdded(chat_id=chat_id, timestamp=ts):
            return f"ba:{chat_id}:{ts}"
        case BotRemoved(chat_id=chat_id, timestamp=ts):
            return f"br:{chat_id}:{ts}"
        case BotStopped(chat_id=chat_id, user=user, timestamp=ts):
            return f"bst:{chat_id}:{user.user_id if user else ''}:{ts}"
    raise TypeError(f"unknown update {update!r}")


def update_user_id(update: Update) -> int | None:
    """The user whose updates must be serialized (§11 per-user lock)."""
    match update:
        case BotStarted(user=user) | CallbackQuery(user=user):
            return user.user_id
        case MessageCreated(sender=sender):
            return sender.user_id
        case BotAdded(user=user) | BotRemoved(user=user) | BotStopped(user=user):
            return user.user_id if user else None
    return None
