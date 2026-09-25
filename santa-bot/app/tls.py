"""Outbound TLS trust (§2): the certifi bundle plus the Russian CA certificates in certs/.

MAX asks bots to trust the Минцифры root. One SSLContext built here is used for
all outbound HTTPS. ``bundled_certificates`` feeds the daily expiry check.
"""

from __future__ import annotations

import hashlib
import ssl
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import certifi

CERTS_DIR = Path(__file__).resolve().parent.parent / "certs"


@dataclass(frozen=True, slots=True)
class BundledCertificate:
    path: Path
    common_name: str
    sha256: str  # colon-separated upper-case hex, as openssl prints it
    not_after: datetime


def build_ssl_context(certs_dir: Path = CERTS_DIR) -> ssl.SSLContext:
    context = ssl.create_default_context(cafile=certifi.where())
    for path in sorted(certs_dir.glob("*.pem")):
        context.load_verify_locations(cafile=str(path))
    return context


def bundled_certificates(certs_dir: Path = CERTS_DIR) -> list[BundledCertificate]:
    return [_describe(path) for path in sorted(certs_dir.glob("*.pem"))]


def _describe(path: Path) -> BundledCertificate:
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=str(path))
    info: dict[str, Any] = context.get_ca_certs()[0]
    der: bytes = context.get_ca_certs(binary_form=True)[0]
    subject = {key: value for rdn in info.get("subject", ()) for key, value in rdn}
    digest = hashlib.sha256(der).hexdigest().upper()
    return BundledCertificate(
        path=path,
        common_name=str(subject.get("commonName", path.name)),
        sha256=":".join(digest[i : i + 2] for i in range(0, len(digest), 2)),
        not_after=datetime.fromtimestamp(ssl.cert_time_to_seconds(str(info["notAfter"])), tz=timezone.utc),
    )
