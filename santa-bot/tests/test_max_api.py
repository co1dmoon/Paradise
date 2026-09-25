from __future__ import annotations

import json
import ssl
from collections.abc import AsyncIterator
from typing import Any

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from app.core.kb import callback, keyboard, link
from app.max_api import (
    BadRequest,
    BotStarted,
    CallbackQuery,
    Forbidden,
    HttpMaxApi,
    MessageCreated,
    OutMessage,
    RateLimited,
    Target,
    Transient,
    Unauthorized,
    classify_error,
    dedupe_key,
    ensure_subscription,
    parse_update,
    update_user_id,
)
from tools import fake_max


@pytest.mark.parametrize(
    ("status", "detail", "expected"),
    [
        (429, "", RateLimited),
        (500, "oops", Transient),
        (503, "", Transient),
        (401, "Invalid access_token", Unauthorized),
        (403, "whatever", Forbidden),
        (404, "chat.not.found", Forbidden),
        (400, "error.dialog.suspended: user blocked the bot", Forbidden),
        (400, "Access denied", Forbidden),
        (400, "text: size must be between 0 and 4000", BadRequest),
        (405, "Method not allowed", BadRequest),
    ],
)
def test_classify_error(status: int, detail: str, expected: type) -> None:
    assert type(classify_error(status, detail)) is expected


def test_out_message_round_trip() -> None:
    board = keyboard([callback("Да", "c:yes"), link("Сайт", "https://santa.example.ru")])
    message = OutMessage("Привет", board)
    body = message.to_max()
    assert body["attachments"][0]["type"] == "inline_keyboard"
    assert body["attachments"][0]["payload"]["buttons"][0][1] == {"type": "link", "text": "Сайт",
                                                                   "url": "https://santa.example.ru"}
    assert "format" not in body
    assert OutMessage.from_max(json.loads(json.dumps(body))) == message


def test_long_text_is_cut_to_4000() -> None:
    assert len(OutMessage("я" * 5000).to_max()["text"]) == 4000


def test_parse_updates_from_the_fake() -> None:
    started = parse_update(fake_max.bot_started(5, "j_ABC234", name="Ольга Петрова"))
    assert isinstance(started, BotStarted) and started.payload == "j_ABC234"
    assert started.user.name == "Ольга Петрова" and update_user_id(started) == 5
    message = parse_update(fake_max.message_created(5, "код abc234"))
    assert isinstance(message, MessageCreated) and message.is_private and message.text == "код abc234"
    press = parse_update(fake_max.message_callback(5, "c:yes", message_mid="mid.1"))
    assert isinstance(press, CallbackQuery) and press.payload == "c:yes" and press.message_mid == "mid.1"
    assert dedupe_key(press) == press.callback_id
    assert dedupe_key(started) == f"bs:5:{started.timestamp}"
    assert dedupe_key(message) == message.mid


@pytest.mark.parametrize(
    "raw",
    [None, [], {}, {"update_type": "message_edited"}, {"update_type": "bot_started"},
     {"update_type": "message_created", "message": "x"}, {"update_type": "message_callback", "callback": {}},
     {"update_type": "bot_added"}],
)
def test_parse_is_lenient(raw: Any) -> None:
    assert parse_update(raw) is None


def test_parse_ignores_unknown_fields_and_odd_shapes() -> None:
    update = {"update_type": "message_created", "timestamp": "17", "extra": {"x": 1},
              "message": {"sender": {"user_id": "12", "first_name": "Иван", "future": True},
                          "recipient": {"chat_type": "chat", "chat_id": -3},
                          "body": {"mid": "m1", "text": None, "attachments": [{"type": "image"}]}}}
    parsed = parse_update(update)
    assert isinstance(parsed, MessageCreated)
    assert (parsed.sender.user_id, parsed.sender.name, parsed.text, parsed.has_attachments) == (12, "Иван", "", True)
    assert not parsed.is_private


class Recorder:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.responses: dict[str, list[tuple[int, Any]]] = {}

    def reply(self, route: str, status: int, body: Any) -> None:
        self.responses.setdefault(route, []).append((status, body))

    async def handle(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else None
        route = f"{request.method} {request.path}"
        self.requests.append({"route": route, "query": dict(request.query), "body": body,
                              "auth": request.headers.get("Authorization")})
        queued = self.responses.get(route)
        status, payload = queued.pop(0) if queued else (200, {"success": True})
        return web.Response(status=status, text=payload if isinstance(payload, str) else json.dumps(payload))


@pytest.fixture
async def server() -> AsyncIterator[tuple[Recorder, HttpMaxApi]]:
    recorder = Recorder()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", recorder.handle)
    test_server = TestServer(app)
    await test_server.start_server()
    api = HttpMaxApi("secret-token", str(test_server.make_url("")), ssl.create_default_context())
    yield recorder, api
    await api.close()
    await test_server.close()


async def test_send_uses_header_auth_and_query_params(server: tuple[Recorder, HttpMaxApi]) -> None:
    recorder, api = server
    recorder.reply("POST /messages", 200, {"message": {"body": {"mid": "mid.42", "text": "hi"}, "unknown": 1}})
    mid = await api.send(Target.user(77), OutMessage("hi", keyboard(callback("a", "a"))), disable_link_preview=True)
    request = recorder.requests[-1]
    assert mid == "mid.42"
    assert request["auth"] == "secret-token"
    assert request["query"] == {"user_id": "77", "disable_link_preview": "true"}
    assert request["body"]["attachments"][0]["payload"]["buttons"] == [[{"type": "callback", "text": "a",
                                                                         "payload": "a"}]]
    await api.send(Target.chat(-5), OutMessage("group"))
    assert recorder.requests[-1]["query"] == {"chat_id": "-5"}


async def test_success_false_is_an_error(server: tuple[Recorder, HttpMaxApi]) -> None:
    recorder, api = server
    recorder.reply("PUT /messages", 200, {"success": False, "message": "message.not.found"})
    with pytest.raises(Forbidden):
        await api.edit("mid.1", OutMessage("x"))
    recorder.reply("PUT /messages", 200, {"success": False, "message": "invalid body"})
    with pytest.raises(BadRequest):
        await api.edit("mid.1", OutMessage("x"))
    assert recorder.requests[-1]["query"] == {"message_id": "mid.1"}


async def test_http_errors_are_classified(server: tuple[Recorder, HttpMaxApi]) -> None:
    recorder, api = server
    recorder.reply("GET /me", 401, {"code": "verify.token", "message": "Invalid access_token"})
    with pytest.raises(Unauthorized):
        await api.get_me()
    recorder.reply("GET /me", 502, "<html>bad gateway</html>")
    with pytest.raises(Transient):
        await api.get_me()
    recorder.reply("GET /me", 200, {"user_id": 5, "username": "se1_bot", "name": "Санта", "is_bot": True})
    me = await api.get_me()
    assert (me.user_id, me.username) == (5, "se1_bot")


async def test_answer_notification_fallback(server: tuple[Recorder, HttpMaxApi]) -> None:
    recorder, api = server
    recorder.reply("POST /answers", 400, {"code": "proto.payload", "message": "Unknown field notification"})
    assert await api.answer("cb1", notification="Готово") is False
    assert [r["body"] for r in recorder.requests] == [{"notification": "Готово"}, {}]
    assert await api.answer("cb2", notification="Снова") is False
    assert recorder.requests[-1]["body"] == {}, "an unsupported notification is not retried"
    assert await api.answer("cb3", message=OutMessage("новый текст")) is True
    assert recorder.requests[-1]["body"]["message"]["text"] == "новый текст"
    assert recorder.requests[-1]["query"] == {"callback_id": "cb3"}


async def test_subscriptions_and_updates(server: tuple[Recorder, HttpMaxApi]) -> None:
    recorder, api = server
    recorder.reply("GET /subscriptions", 200, {"subscriptions": []})
    assert await ensure_subscription(api, "https://santa.example.ru/max/webhook/x", "sec-ret") is True
    subscribe = recorder.requests[-1]
    assert subscribe["route"] == "POST /subscriptions"
    assert subscribe["body"]["secret"] == "sec-ret" and "message_callback" in subscribe["body"]["update_types"]
    recorder.reply("GET /subscriptions", 200, {"subscriptions": [
        {"url": "https://santa.example.ru/max/webhook/x", "time": 1, "update_types": ["bot_started"]}]})
    assert await ensure_subscription(api, "https://santa.example.ru/max/webhook/x", "sec-ret") is False
    recorder.reply("GET /updates", 200, {"updates": [{"update_type": "bot_started"}, "junk"], "marker": 7})
    page = await api.get_updates(None, 30)
    assert page.marker == 7 and len(page.updates) == 1
    assert recorder.requests[-1]["query"] == {"timeout": "30", "limit": "100"}


async def test_command_menu(server: tuple[Recorder, HttpMaxApi]) -> None:
    recorder, api = server
    await api.set_commands([("start", "Главное меню")])
    assert recorder.requests[-1]["route"] == "PATCH /me/commands"
    assert recorder.requests[-1]["body"] == {"commands": [{"name": "start", "description": "Главное меню"}]}
