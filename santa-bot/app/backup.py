"""Nightly SQLite backups (§10): an online copy to /data/backups/santa-YYYYMMDD.db, 14 kept.

The copy is written under a temporary name and renamed when complete, so a crash
never leaves a half-written file that looks like a backup. Backups hold personal
data: the files are readable by the app user only.

P1: when S3_* is set, the copy is also uploaded to an S3-compatible bucket (in
Russia: Yandex Object Storage, Selectel, Timeweb Cloud) with a plain signed PUT
(AWS Signature Version 4, path-style URL). Old copies in the bucket are removed
by the bucket's lifecycle rule, which the owner sets up (README_RU.md).
"""

from __future__ import annotations

import hashlib
import hmac
import os
import re
import ssl
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlsplit

import aiohttp

from app.db import Database

KEEP = 14
_NAME = re.compile(r"^santa-\d{8}\.db$")


def backup_name(day: date) -> str:
    return f"santa-{day:%Y%m%d}.db"


async def make_backup(db: Database, directory: Path, day: date) -> Path:
    """Copy the live database into ``directory`` (one file per day, replaced if it exists)."""
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / backup_name(day)
    partial = target.with_name(target.name + ".partial")
    partial.unlink(missing_ok=True)
    await db.backup_to(partial)
    partial.chmod(0o600)
    os.replace(partial, target)
    return target


def rotate(directory: Path, keep: int = KEEP) -> list[Path]:
    """Delete all but the ``keep`` newest backups; returns the deleted files."""
    backups = sorted(path for path in directory.iterdir() if _NAME.match(path.name))
    stale = backups[:-keep] if len(backups) > keep else []
    for path in stale:
        path.unlink()
    return stale


# --- S3 upload (P1) ------------------------------------------------------------------------------------

UPLOAD_TIMEOUT = 300.0


class UploadFailed(Exception):
    """The bucket refused the backup or could not be reached."""


@dataclass(frozen=True, slots=True)
class Bucket:
    endpoint: str  # https://storage.yandexcloud.net
    name: str
    key_id: str
    secret: str
    region: str


def signed_put(bucket: Bucket, object_name: str, body: bytes, now: datetime) -> tuple[str, dict[str, str]]:
    """URL and headers of a SigV4-signed PUT of ``body`` as ``object_name``."""
    host = urlsplit(bucket.endpoint).netloc
    path = f"/{quote(bucket.name)}/{quote(object_name)}"
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    scope = f"{stamp[:8]}/{bucket.region}/s3/aws4_request"
    payload = hashlib.sha256(body).hexdigest()
    headers = {"host": host, "x-amz-content-sha256": payload, "x-amz-date": stamp}
    signed = ";".join(sorted(headers))
    canonical = "\n".join(
        ["PUT", path, "", *(f"{name}:{headers[name]}" for name in sorted(headers)), "", signed, payload]
    )
    to_sign = "\n".join(["AWS4-HMAC-SHA256", stamp, scope, hashlib.sha256(canonical.encode()).hexdigest()])
    key = f"AWS4{bucket.secret}".encode()
    for part in (stamp[:8], bucket.region, "s3", "aws4_request"):
        key = hmac.new(key, part.encode(), hashlib.sha256).digest()
    signature = hmac.new(key, to_sign.encode(), hashlib.sha256).hexdigest()
    headers["authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={bucket.key_id}/{scope}, SignedHeaders={signed}, Signature={signature}"
    )
    del headers["host"]  # aiohttp sends it from the URL
    return bucket.endpoint.rstrip("/") + path, headers


async def upload(bucket: Bucket, path: Path, now: datetime, ssl_context: ssl.SSLContext | bool = True) -> None:
    """PUT the backup file into the bucket; raises ``UploadFailed``."""
    body = path.read_bytes()
    url, headers = signed_put(bucket, path.name, body, now)
    try:
        async with aiohttp.ClientSession(connector=aiohttp.TCPConnector(ssl=ssl_context)) as session:
            async with session.put(url, data=body, headers=headers,
                                   timeout=aiohttp.ClientTimeout(total=UPLOAD_TIMEOUT)) as response:
                if response.status >= 300:
                    raise UploadFailed(f"HTTP {response.status}")
    except (aiohttp.ClientError, TimeoutError) as error:
        raise UploadFailed(type(error).__name__) from error
