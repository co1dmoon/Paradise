"""The Russian CA files in certs/ (§2), checked straight from disk with the stdlib only.

These files are copied into the system trust store by the Dockerfile and loaded by
app/tls.py, so a wrong or damaged file breaks every call to MAX.
"""

from __future__ import annotations

import hashlib
import re
import ssl
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pytest

CERTS_DIR = Path(__file__).resolve().parent.parent / "certs"
PEM_BLOCK = re.compile(r"-----BEGIN CERTIFICATE-----\s+[A-Za-z0-9+/=\s]+?-----END CERTIFICATE-----")

EXPECTED = {
    "russian_trusted_root_ca.pem": {
        "common_name": "Russian Trusted Root CA",
        "issuer": "Russian Trusted Root CA",
        "sha256": "D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31",
        "not_after": datetime(2032, 2, 27, 21, 4, 15, tzinfo=timezone.utc),
    },
    "russian_trusted_sub_ca.pem": {
        "common_name": "Russian Trusted Sub CA",
        "issuer": "Russian Trusted Root CA",
        "sha256": "BB:BD:E2:10:3E:79:0B:99:9E:C6:2B:D0:3C:F6:25:A5:A2:E7:C3:16:E1:0A:FE:6A:49:0E:ED:EA:D8:B3:FD:9B",
        "not_after": datetime(2027, 3, 6, 11, 25, 19, tzinfo=timezone.utc),
    },
}


def _pem(name: str) -> str:
    return (CERTS_DIR / name).read_text(encoding="ascii")


def _decode(pem: str) -> dict[str, Any]:
    """The certificate as the ssl module describes it (subject, issuer, notAfter, ...)."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cadata=pem)
    (info,) = context.get_ca_certs()
    return info


def _common_name(rdns: tuple[tuple[tuple[str, str], ...], ...]) -> str:
    return next(value for rdn in rdns for key, value in rdn if key == "commonName")


def _fingerprint(pem: str) -> str:
    digest = hashlib.sha256(ssl.PEM_cert_to_DER_cert(pem)).hexdigest().upper()
    return ":".join(digest[i : i + 2] for i in range(0, len(digest), 2))


def test_certs_dir_holds_exactly_the_two_russian_cas() -> None:
    assert sorted(path.name for path in CERTS_DIR.glob("*.pem")) == sorted(EXPECTED)


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_each_file_is_a_single_pem_certificate(name: str) -> None:
    # update-ca-certificates wants exactly one PEM certificate per file.
    assert len(PEM_BLOCK.findall(_pem(name))) == 1


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_sha256_fingerprint_matches_the_spec(name: str) -> None:
    assert _fingerprint(_pem(name)) == EXPECTED[name]["sha256"]


@pytest.mark.parametrize("name", sorted(EXPECTED))
def test_subject_issuer_and_expiry(name: str) -> None:
    info = _decode(_pem(name))
    not_after = datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), tz=timezone.utc)
    assert _common_name(info["subject"]) == EXPECTED[name]["common_name"]
    assert _common_name(info["issuer"]) == EXPECTED[name]["issuer"]
    assert not_after == EXPECTED[name]["not_after"]


def test_sub_ca_is_issued_by_the_bundled_root() -> None:
    root = _decode(_pem("russian_trusted_root_ca.pem"))
    sub = _decode(_pem("russian_trusted_sub_ca.pem"))
    assert sub["issuer"] == root["subject"]
