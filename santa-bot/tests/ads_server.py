"""A scripted HTTP server for the ad platform clients: queued replies per route, every request recorded."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from aiohttp import web
from aiohttp.test_utils import TestServer


@dataclass(frozen=True, slots=True)
class Reply:
    status: int = 200
    body: Any = None  # dict or list → JSON, str → text as is, None → empty
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True, slots=True)
class Seen:
    method: str
    path: str
    query: dict[str, str]
    headers: dict[str, str]
    json: Any
    form: dict[str, str]

    @property
    def route(self) -> str:
        return f"{self.method} {self.path}"


class ScriptedServer:
    def __init__(self) -> None:
        self.requests: list[Seen] = []
        self._replies: dict[str, list[Reply]] = {}
        self._server: TestServer | None = None

    def reply(self, route: str, *replies: Reply) -> None:
        """Queue replies for 'METHOD /path'; an unscripted request gets 404."""
        self._replies.setdefault(route, []).extend(replies)

    def url(self, path: str) -> str:
        assert self._server is not None
        return str(self._server.make_url(path))

    def routes(self) -> list[str]:
        return [seen.route for seen in self.requests]

    async def _handle(self, request: web.Request) -> web.Response:
        text = await request.text()
        is_form = request.content_type == "application/x-www-form-urlencoded"
        self.requests.append(Seen(
            method=request.method, path=request.path, query=dict(request.query), headers=dict(request.headers),
            json=json.loads(text) if text and request.content_type == "application/json" else None,
            form=dict(await request.post()) if is_form else {},  # type: ignore[arg-type]
        ))
        queued = self._replies.get(f"{request.method} {request.path}")
        reply = queued.pop(0) if queued else Reply(404, "not scripted")
        if reply.body is None:
            return web.Response(status=reply.status, headers=reply.headers)
        if isinstance(reply.body, str):
            return web.Response(status=reply.status, text=reply.body, headers=reply.headers)
        return web.json_response(reply.body, status=reply.status, headers=reply.headers)


@asynccontextmanager
async def scripted_server() -> AsyncIterator[ScriptedServer]:
    scripted = ScriptedServer()
    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", scripted._handle)
    scripted._server = TestServer(app)
    await scripted._server.start_server()
    try:
        yield scripted
    finally:
        await scripted._server.close()
