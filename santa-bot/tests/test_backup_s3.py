"""P1: nightly backups also go to an S3-compatible bucket, signed with AWS Signature Version 4."""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone

from aiohttp import web
from aiohttp.test_utils import TestServer

from app import backup, jobs
from app.context import AppContext
from app.core import texts
from tests.bot import ADMIN_ID
from tools.fake_max import FakeMaxApi


def test_signature_matches_botocore() -> None:
    """The vector was computed once with botocore's S3SigV4Auth for the same request."""
    bucket = backup.Bucket("https://storage.yandexcloud.net", "santa-backups", "YCAJEexampleKEYID",
                           "YCsecretEXAMPLEkey/abc+123", "ru-central1")
    url, headers = backup.signed_put(bucket, "santa-20261120.db", b"SQLite format 3\x00 backup",
                                     datetime(2026, 9, 25, 1, 8, 24, tzinfo=timezone.utc))
    assert url == "https://storage.yandexcloud.net/santa-backups/santa-20261120.db"
    assert headers == {
        "x-amz-content-sha256": "7a5d293e44f76c7e066fd8d88edb18754ebca80f0ba6fcfafb852679af14d3d7",
        "x-amz-date": "20260925T010824Z",
        "authorization": "AWS4-HMAC-SHA256 Credential=YCAJEexampleKEYID/20260925/ru-central1/s3/aws4_request, "
                         "SignedHeaders=host;x-amz-content-sha256;x-amz-date, "
                         "Signature=934309cdaa8f12cf66e6590d095a2afc6c2fb4817bf0bb3a36b951b667b160c0",
    }


async def fake_bucket(status: int) -> tuple[TestServer, list[tuple[str, bytes, str]]]:
    received: list[tuple[str, bytes, str]] = []

    async def put(request: web.Request) -> web.Response:
        received.append((request.path, await request.read(), request.headers.get("Authorization", "")))
        return web.Response(status=status)

    app = web.Application(client_max_size=64 * 1024 * 1024)
    app.router.add_put("/{bucket}/{name}", put)
    server = TestServer(app)
    await server.start_server()
    return server, received


def with_bucket(ctx: AppContext, server: TestServer) -> None:
    ctx.config = dataclasses.replace(
        ctx.config, s3_endpoint=str(server.make_url("")).rstrip("/"), s3_bucket="santa-backups",
        s3_key="key-id", s3_secret="secret", s3_region="ru-1",
    )


async def test_backup_is_uploaded(ctx: AppContext, api: FakeMaxApi) -> None:
    server, received = await fake_bucket(200)
    with_bucket(ctx, server)
    await jobs.backup_database(ctx)
    await server.close()
    ((path, body, authorization),) = received
    assert path == "/santa-backups/santa-20261120.db"
    assert body == (ctx.config.backups_dir / "santa-20261120.db").read_bytes()
    assert authorization.startswith("AWS4-HMAC-SHA256 Credential=key-id/20261120/ru-1/s3/aws4_request")
    await ctx.outbox.drain()
    assert api.texts_to(ADMIN_ID) == []


async def test_a_refused_upload_alerts_the_admins(ctx: AppContext, api: FakeMaxApi) -> None:
    server, _ = await fake_bucket(403)
    with_bucket(ctx, server)
    await jobs.backup_database(ctx)
    await server.close()
    await ctx.outbox.drain()
    assert api.texts_to(ADMIN_ID) == [texts.backup_upload_failed("HTTP 403")]
    assert (ctx.config.backups_dir / "santa-20261120.db").exists()
