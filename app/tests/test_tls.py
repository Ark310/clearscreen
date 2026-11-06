"""HTTPS configuration: wildcard matching and the two certificate modes of the Caddyfile."""
import pytest

from clearscreen.admin import ops
from clearscreen.config import Settings


@pytest.mark.parametrize("pattern,host,ok", [
    ("*.clearscreen.example", "api.clearscreen.example", True),
    ("*.clearscreen.example", "API.CLEARSCREEN.EXAMPLE", True),
    ("*.clearscreen.example", "a.b.clearscreen.example", False),          # a wildcard covers one label only
    ("*.clearscreen.example", "clearscreen.example", False),
    ("*.clearscreen.example", "evilclearscreen.example", False),
    ("clearscreen.example", "clearscreen.example", True),
    ("api.clearscreen.example", "api.clearscreen.example.", True),
])
def test_wildcard_matching(pattern, host, ok):
    assert ops._covers(pattern, host) is ok


def test_caddyfile_certificate_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CLS_DATA_DIR", str(tmp_path))
    text = ops.caddyfile(Settings(domain="api.clearscreen.example", tls_mode="certificate"))
    assert "auto_https off" in text                         # no Let's Encrypt, no port 80
    assert "https://api.clearscreen.example:443 {" in text
    assert 'tls "' in text and "config/tls/origin.pem" in text and "config/tls/origin.key" in text
    assert "email" not in text


def test_caddyfile_automatic_mode(tmp_path, monkeypatch):
    monkeypatch.setenv("CLS_DATA_DIR", str(tmp_path))
    text = ops.caddyfile(Settings(domain="api.example.com", tls_mode="automatic", acme_email="it@example.com"))
    assert "auto_https off" not in text and "\ttls " not in text
    assert "email it@example.com" in text and "\napi.example.com {" in text


def test_status_without_certificate(tmp_path, monkeypatch):
    monkeypatch.setenv("CLS_DATA_DIR", str(tmp_path))
    assert "NO CERTIFICATE" in ops.certificate_status(Settings(tls_mode="certificate"))
