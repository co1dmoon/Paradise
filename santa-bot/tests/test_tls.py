from __future__ import annotations

from datetime import datetime, timezone

from app.tls import build_ssl_context, bundled_certificates

ROOT_SHA256 = "D2:6D:2D:02:31:B7:C3:9F:92:CC:73:85:12:BA:54:10:35:19:E4:40:5D:68:B5:BD:70:3E:97:88:CA:8E:CF:31"
SUB_SHA256 = "BB:BD:E2:10:3E:79:0B:99:9E:C6:2B:D0:3C:F6:25:A5:A2:E7:C3:16:E1:0A:FE:6A:49:0E:ED:EA:D8:B3:FD:9B"


def test_bundled_russian_certificates_have_the_expected_fingerprints() -> None:
    certificates = {cert.common_name: cert for cert in bundled_certificates()}
    assert certificates["Russian Trusted Root CA"].sha256 == ROOT_SHA256
    assert certificates["Russian Trusted Sub CA"].sha256 == SUB_SHA256
    assert certificates["Russian Trusted Root CA"].not_after.date() == datetime(2032, 2, 27).date()
    assert certificates["Russian Trusted Sub CA"].not_after.date() == datetime(2027, 3, 6).date()
    assert all(cert.not_after.tzinfo == timezone.utc for cert in certificates.values())


def test_ssl_context_trusts_certifi_and_the_russian_root() -> None:
    context = build_ssl_context()
    subjects = {
        value for cert in context.get_ca_certs() for rdn in cert["subject"] for key, value in rdn if key == "commonName"
    }
    assert "Russian Trusted Root CA" in subjects
    assert len(subjects) > 50, "the certifi bundle is loaded too"
