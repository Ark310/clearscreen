"""Installing a .pfx (PKCS#12) certificate: unpacked with its password, checked like PEM files, and
only then installed. Certificates are made here with the cryptography package (no external tools)."""
from datetime import datetime, timedelta, timezone

import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import BestAvailableEncryption, PrivateFormat, pkcs12
from cryptography.x509.oid import NameOID

from clearscreen.admin import ops

DOMAIN = "api.clearscreen.example"


def _cert(key, names, days=365):
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Example Origin CA")])
    now = datetime.now(timezone.utc)
    return (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number()).not_valid_before(now - timedelta(days=1))
            .not_valid_after(now + timedelta(days=days))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
            .sign(key, hashes.SHA256()))


def _pfx(tmp_path, key, cert, password, legacy=False, name="origin.pfx"):
    if legacy:      # how older Windows exports look: 3DES + SHA1
        enc = (PrivateFormat.PKCS12.encryption_builder().kdf_rounds(2048)
               .key_cert_algorithm(pkcs12.PBES.PBESv1SHA1And3KeyTripleDESCBC).hmac_hash(hashes.SHA1())
               .build(password.encode()))
    else:
        enc = BestAvailableEncryption(password.encode())
    p = tmp_path / name
    p.write_bytes(pkcs12.serialize_key_and_certificates(b"origin", key, cert, None, enc))
    return p


@pytest.fixture
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("CLS_DATA_DIR", str(tmp_path / "data"))
    return tmp_path


@pytest.mark.parametrize("keytype,legacy", [("rsa", False), ("rsa", True), ("ec", False)])
def test_install_pfx(data, keytype, legacy):
    key = rsa.generate_private_key(65537, 2048) if keytype == "rsa" else ec.generate_private_key(ec.SECP256R1())
    pfx = _pfx(data, key, _cert(key, ["*.clearscreen.example", "clearscreen.example"]), "s3cret pass", legacy)
    info = ops.install_pfx(pfx, "s3cret pass", DOMAIN)
    assert info["names"] == ["*.clearscreen.example", "clearscreen.example"]
    assert ops.cert_file().read_bytes().startswith(b"-----BEGIN CERTIFICATE-----")
    assert b"-----BEGIN PRIVATE" b" KEY-----" in ops.key_file().read_bytes()
    assert b"ENCRYPTED" not in ops.key_file().read_bytes()
    assert not list(ops.tls_dir().glob("incoming.*"))
    ops.inspect_certificate(ops.cert_file(), ops.key_file(), DOMAIN)          # the installed pair belongs together


def test_wrong_password_changes_nothing(data):
    key = rsa.generate_private_key(65537, 2048)
    good = _pfx(data, key, _cert(key, [DOMAIN]), "right", name="good.pfx")
    ops.install_pfx(good, "right", DOMAIN)
    before = ops.cert_file().read_bytes()
    other_key = rsa.generate_private_key(65537, 2048)
    newer = _pfx(data, other_key, _cert(other_key, [DOMAIN]), "right", name="newer.pfx")
    with pytest.raises(ValueError, match="password is wrong"):
        ops.install_pfx(newer, "wrong", DOMAIN)
    assert ops.cert_file().read_bytes() == before
    assert not list(ops.tls_dir().glob("incoming.*"))


def test_pfx_for_another_domain_is_refused(data):
    key = rsa.generate_private_key(65537, 2048)
    pfx = _pfx(data, key, _cert(key, ["*.example.com"]), "pw")
    with pytest.raises(ValueError, match="does not cover"):
        ops.install_pfx(pfx, "pw", DOMAIN)
    assert not ops.cert_file().exists()


def test_not_a_pfx(data):
    junk = data / "junk.pfx"
    junk.write_bytes(b"this is not a pfx")
    with pytest.raises(ValueError, match="not a .pfx"):
        ops.install_pfx(junk, "pw", DOMAIN)
