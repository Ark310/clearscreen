# -*- coding: utf-8 -*-
"""What the Admin app does, without the window: HTTPS config, SQL connection and Groq key
checks, backup / restore, loading Excel, the health check. Everything here runs on the server as
an administrator."""
import os
import re
from datetime import datetime
from pathlib import Path

import httpx

from clearscreen import db
from clearscreen.config import Settings, config_dir, data_dir, logs_dir, registry_value
from clearscreen.excel import read_records

INSTALL_DIR = Path(os.environ.get("CLS_INSTALL_DIR") or registry_value("InstallDir")
                   or Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClearScreen")


# ------------------------------------------------------------------- SSMS ---
def ssms_path() -> Path | None:
    """SQL Server Management Studio: where setup installed it, else the usual places."""
    candidates = [registry_value("SsmsExe"),
                  r"C:\Program Files\Microsoft SQL Server Management Studio 22\Release\Common7\IDE\Ssms.exe",
                  r"C:\Program Files\Microsoft SQL Server Management Studio 21\Release\Common7\IDE\Ssms.exe"]
    for c in candidates:
        if c and Path(c).is_file():
            return Path(c)
    return None


def _cs_part(cs: str, *names: str) -> str | None:
    for name in names:
        m = re.search(rf"(?:^|;)\s*{name}\s*=\s*([^;]+)", cs or "", re.I)
        if m:
            return m.group(1).strip()
    return None


def open_ssms(cs: str | None) -> str:
    """Open SSMS connected to the ClearScreen database with the signed-in administrator's Windows
    login (-C: trust the local server's certificate). Returns what was opened."""
    import subprocess
    exe = ssms_path()
    if exe is None:
        raise FileNotFoundError("SQL Server Management Studio is not installed. Run Setup.cmd again "
                                "(it installs it), or install it from https://aka.ms/ssms.")
    server = _cs_part(cs, "Server", "Data Source", "Address") or "localhost"
    database = _cs_part(cs, "Database", "Initial Catalog") or "ClearScreen"
    subprocess.Popen([str(exe), "-S", server, "-d", database, "-C", "-nosplash"], close_fds=True)
    return f"{server} / {database}"


# ------------------------------------------------------------------ HTTPS ---
# tls_mode "certificate": a certificate + private key supplied by you (e.g. a Cloudflare Origin
# Certificate, with Cloudflare proxying the domain). Port 443 only, nothing to renew.
# tls_mode "automatic": Caddy gets and renews a Let's Encrypt certificate itself (needs port 80 too).
def tls_dir() -> Path:
    return config_dir() / "tls"          # Administrators, SYSTEM and the HTTPS service only (installer sets it)


def cert_file() -> Path:
    return tls_dir() / "origin.pem"


def key_file() -> Path:
    return tls_dir() / "origin.key"


def _covers(pattern: str, host: str) -> bool:
    pattern, host = pattern.lower().rstrip("."), host.lower().rstrip(".")
    if pattern.startswith("*."):                      # a wildcard covers exactly one extra label
        head, _, rest = host.partition(".")
        return bool(head) and rest == pattern[2:]
    return pattern == host


def inspect_certificate(cert_path, key_path=None, domain: str | None = None) -> dict:
    """Check a PEM certificate (and its private key): readable, not expired, covers the domain,
    key belongs to it. Returns {names, issuer, expires, days_left}; raises ValueError with a plain
    explanation otherwise. Uses only Python's ssl module."""
    import ssl
    from datetime import timezone
    data = Path(cert_path).read_bytes()
    if b"-----BEGIN CERTIFICATE-----" not in data:
        raise ValueError("The certificate must be in PEM format: a text file that starts with "
                         "-----BEGIN CERTIFICATE-----. In Cloudflare choose the PEM key format.")
    if b"PRIVATE KEY-----" in data:
        raise ValueError("This file also contains a private key. Choose the certificate and the key as two separate files.")
    try:
        info = ssl._ssl._test_decode_cert(str(cert_path))            # stdlib X.509 reader (first certificate)
    except Exception as e:                                         # noqa: BLE001
        raise ValueError(f"The certificate file could not be read: {e}") from e
    names = [v for k, v in info.get("subjectAltName", ()) if k == "DNS"]
    issuer = dict(x[0] for x in info.get("issuer", ()))
    expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(info["notAfter"]), tz=timezone.utc)
    days_left = (expires - datetime.now(timezone.utc)).days
    result = {"names": names, "issuer": issuer.get("organizationName") or issuer.get("commonName", "?"),
              "issuer_detail": issuer.get("organizationalUnitName") or issuer.get("commonName", ""),
              "expires": expires.strftime("%Y-%m-%d"), "days_left": days_left}
    if days_left < 0:
        raise ValueError(f"The certificate expired on {result['expires']}.")
    if domain and not any(_covers(n, domain) for n in names):
        raise ValueError(f"The certificate is for {', '.join(names) or '(no names)'}, which does not cover {domain}.")
    if key_path is not None:
        key = Path(key_path).read_bytes()
        if b"PRIVATE KEY-----" not in key:
            raise ValueError("The key file must be the private key in PEM format: a text file that starts with "
                             "-----BEGIN PRIVATE" " KEY-----.")
        if b"ENCRYPTED" in key:
            raise ValueError("The private key is password-protected. Use the unprotected key Cloudflare gave you.")
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        try:
            ctx.load_cert_chain(str(cert_path), str(key_path), password=lambda: b"")
        except ssl.SSLError as e:
            raise ValueError("The private key does not belong to this certificate.") from e
    return result


def install_certificate(cert_src, key_src, domain: str) -> dict:
    """Check the certificate + key, then copy them into the protected tls folder."""
    info = inspect_certificate(cert_src, key_src, domain)
    tls_dir().mkdir(parents=True, exist_ok=True)
    cert_file().write_bytes(Path(cert_src).read_bytes())
    key_file().write_bytes(Path(key_src).read_bytes())
    return info


PFX_SUFFIXES = (".pfx", ".p12")


def pfx_to_pem(pfx_path, password: str) -> tuple[bytes, bytes]:
    """Unpack a .pfx / .p12 file (certificate + private key, password-protected) into a PEM certificate
    (followed by any chain certificates in the file) and an unencrypted PKCS#8 PEM private key.
    The HTTPS service reads only PEM; the key is protected by the tls folder's permissions instead."""
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12
    data = Path(pfx_path).read_bytes()
    attempts = [password.encode("utf-8")] + ([None] if not password else [])   # "no password" has two encodings
    for pw in attempts:
        try:
            key, cert, extra = pkcs12.load_key_and_certificates(data, pw)
            break
        except ValueError:
            continue
    else:
        raise ValueError("The .pfx file could not be opened: the password is wrong, or it is not a .pfx file.")
    if cert is None or key is None:
        raise ValueError("The .pfx file does not contain both a certificate and its private key.")
    cert_pem = cert.public_bytes(Encoding.PEM) + b"".join(c.public_bytes(Encoding.PEM) for c in (extra or []))
    return cert_pem, key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption())


def install_pfx(pfx_path, password: str, domain: str) -> dict:
    """Unpack the .pfx, check the result like PEM files (key belongs to the certificate, covers the domain,
    not expired), and only then replace the installed certificate."""
    cert_pem, key_pem = pfx_to_pem(pfx_path, password)
    tls_dir().mkdir(parents=True, exist_ok=True)
    new_cert, new_key = tls_dir() / "incoming.pem", tls_dir() / "incoming.key"
    try:
        new_cert.write_bytes(cert_pem)
        new_key.write_bytes(key_pem)
        info = inspect_certificate(new_cert, new_key, domain)
        os.replace(new_cert, cert_file())
        os.replace(new_key, key_file())
        return info
    finally:
        for f in (new_cert, new_key):
            f.unlink(missing_ok=True)


def certificate_status(s: Settings) -> str:
    if s.tls_mode != "certificate":
        return f"Automatic (Let's Encrypt) for {s.domain}"
    if not cert_file().exists() or not key_file().exists():
        return "NO CERTIFICATE INSTALLED - HTTPS will not work"
    try:
        i = inspect_certificate(cert_file(), key_file(), s.domain)
    except ValueError as e:
        return f"PROBLEM: {e}"
    warn = "  - RENEW SOON" if i["days_left"] < 30 else ""
    return f"{', '.join(i['names'])} - {i['issuer']} - expires {i['expires']} ({i['days_left']:,} days){warn}"


def caddyfile(s: Settings) -> str:
    """The HTTPS front door: the certificate (your own, or automatic Let's Encrypt), security headers,
    request size limit, everything passed to the API on 127.0.0.1. No access log (it would record
    the X-API-Key header); the API keeps its own access log without keys."""
    caddy_data = (data_dir() / "caddy").as_posix()
    if s.tls_mode == "certificate":
        options = "\n\tauto_https off"                          # no Let's Encrypt, no port 80
        site = f"https://{s.domain}:443"
        tls = f'\n\ttls "{cert_file().as_posix()}" "{key_file().as_posix()}"'
    else:
        options = f"\n\temail {s.acme_email}" if s.acme_email else ""
        site, tls = s.domain, ""
    return f"""# Written by ClearScreen Admin - changes here are overwritten.
{{{options}
\tstorage file_system {caddy_data}
\tadmin off
\tlog {{
\t\toutput file {(logs_dir() / 'https.log').as_posix()} {{
\t\t\troll_size 10mb
\t\t\troll_keep 10
\t\t}}
\t\tlevel WARN
\t}}
}}

{site} {{{tls}
\tencode gzip
\trequest_body {{
\t\tmax_size {s.max_request_mb}MB
\t}}
\theader {{
\t\tStrict-Transport-Security "max-age=31536000; includeSubDomains"
\t\tX-Content-Type-Options "nosniff"
\t\tX-Frame-Options "DENY"
\t\tReferrer-Policy "no-referrer"
\t\t-Server
\t}}
\treverse_proxy 127.0.0.1:{s.api_port}
}}
"""


def write_caddyfile(s: Settings) -> Path:
    p = config_dir() / "Caddyfile"
    p.write_text(caddyfile(s), encoding="utf-8")
    return p


# -------------------------------------------------------------------- SQL ---
_SECRET_PARTS = re.compile(r"((?:password|pwd)\s*=\s*)([^;]*)", re.I)


def mask_connection_string(cs: str | None) -> str:
    return _SECRET_PARTS.sub(r"\1********", cs or "") or "(not set)"


def database_name(cs: str) -> str:
    m = re.search(r"(?:database|initial catalog)\s*=\s*([^;]+)", cs or "", re.I)
    return m.group(1).strip() if m else "ClearScreen"


def test_connection(cs: str) -> tuple[bool, str]:
    try:
        con = db.connect(cs)
        try:
            cur = con.cursor()
            version, edition = cur.execute("SELECT CAST(SERVERPROPERTY('ProductVersion') AS nvarchar(50)), "
                                           "CAST(SERVERPROPERTY('Edition') AS nvarchar(100))").fetchone()
            has_schema = cur.execute("SELECT OBJECT_ID('dbo.WL_Records', 'U')").fetchone()[0] is not None
        finally:
            con.close()
        msg = (f"Connected (SQL Server {version}, {edition}). "
               f"ClearScreen tables: {'present' if has_schema else 'not created yet'}.")
        if edition.startswith("Developer"):
            msg += ("\nNote: Developer edition is licensed for development, testing and demos only. "
                    "Upgrade to Standard before real client data is used.")
        return True, msg
    except Exception as e:                                        # noqa: BLE001
        return False, f"Could not connect: {_short_error(e)}"


def _short_error(e) -> str:
    text = str(e)
    m = re.findall(r"\[SQL Server\]([^\[\]()]+)", text) or re.findall(r"\]([^\[\]]+)\s*\(", text)
    return (m[-1] if m else text).strip()[:300]


def ensure_schema(cs: str) -> None:
    con = db.connect(cs)
    try:
        db.create_schema(con)
    finally:
        con.close()


def default_backup_dir(cs: str) -> str:
    con = db.connect(cs, autocommit=True)
    try:
        return con.cursor().execute("SELECT CAST(SERVERPROPERTY('InstanceDefaultBackupPath') AS nvarchar(400))").fetchone()[0]
    finally:
        con.close()


def backup(cs: str) -> str:
    """Full, checksummed copy-only backup to SQL Server's default backup folder. Returns the file."""
    name = database_name(cs)
    folder = default_backup_dir(cs)
    path = os.path.join(folder, f"{name}_{datetime.now():%Y%m%d_%H%M%S}.bak")
    con = db.connect(cs, autocommit=True)
    try:
        cur = con.cursor()
        cur.execute(f"BACKUP DATABASE [{name}] TO DISK = ? WITH COPY_ONLY, CHECKSUM, INIT, "
                    "NAME = N'ClearScreen backup'", path)
        while cur.nextset():                                      # let the backup finish
            pass
    finally:
        con.close()
    return path


def list_backups(cs: str) -> list[str]:
    folder = default_backup_dir(cs)
    name = database_name(cs)
    try:
        return sorted((str(p) for p in Path(folder).glob(f"{name}_*.bak")), reverse=True)
    except OSError:
        return []


def restore(cs: str, path: str) -> None:
    """Replace the database with a backup. The caller stops the API service first."""
    name = database_name(cs)
    master = re.sub(r"((?:database|initial catalog)\s*=\s*)[^;]+", r"\1master", cs, flags=re.I)
    con = db.connect(master, autocommit=True)
    try:
        cur = con.cursor()
        cur.execute(f"IF DB_ID(?) IS NOT NULL ALTER DATABASE [{name}] SET SINGLE_USER WITH ROLLBACK IMMEDIATE", name)
        cur.execute(f"RESTORE DATABASE [{name}] FROM DISK = ? WITH REPLACE, CHECKSUM", path)
        while cur.nextset():
            pass
        cur.execute(f"ALTER DATABASE [{name}] SET MULTI_USER")
    finally:
        con.close()


# ------------------------------------------------------------------- Groq ---
def test_groq_key(key: str, model: str) -> tuple[bool, str]:
    try:
        r = httpx.get("https://api.groq.com/openai/v1/models", headers={"Authorization": f"Bearer {key}"}, timeout=20)
    except httpx.HTTPError as e:
        return False, f"Could not reach Groq: {type(e).__name__}"
    if r.status_code == 401:
        return False, "Groq rejected this key (invalid or expired)."
    if r.status_code != 200:
        return False, f"Groq answered HTTP {r.status_code}."
    ids = {m["id"] for m in r.json().get("data", [])}
    if model not in ids:
        return True, f"Key accepted, but the model {model} is not available on this Groq account."
    return True, f"Key accepted; model {model} available."


# ------------------------------------------------------------------ Excel ---
def load_excel(cs: str, path: str, allowed_clients: dict, progress=None, batch: int = 5000) -> dict:
    """Load an Excel workbook's records into WL_Records. Only rows whose ClientID is a registered
    client are loaded (ClientID matched case-insensitively and stored as registered)."""
    records, problems = read_records(path)
    canon = {c.lower(): c for c in allowed_clients}
    keep, unknown = [], {}
    for r in records:
        c = canon.get(r["ClientID"].lower())
        if c is None:
            unknown[r["ClientID"]] = unknown.get(r["ClientID"], 0) + 1
        else:
            keep.append(r | {"ClientID": c})
    added = replaced = 0
    con = db.connect(cs)
    try:
        for start in range(0, len(keep), batch):
            status = db.upsert_many(con, keep[start:start + batch])
            con.commit()
            added += status.count("added")
            replaced += status.count("replaced")
            if progress:
                progress(min(start + batch, len(keep)), len(keep))
    finally:
        con.close()
    return dict(read=len(records), added=added, replaced=replaced, skipped_incomplete=len(problems),
                skipped_unknown_client=unknown, problems=problems[:200])


# ----------------------------------------------------------------- health ---
def health(s: Settings, api_key: str | None = None) -> dict:
    headers = {"X-API-Key": api_key} if api_key else {}
    r = httpx.get(f"http://127.0.0.1:{s.api_port}/health", headers=headers, timeout=15)
    r.raise_for_status()
    return r.json()


def log_files() -> dict[str, Path]:
    d = logs_dir()
    return {"API": d / "api.log", "API access (who called what)": d / "access.log",
            "API service errors": d / "ClearScreenApi.err.log", "API service output": d / "ClearScreenApi.out.log",
            "Embedding service": d / "ClearScreenEmbed.err.log", "HTTPS (Caddy)": d / "https.log",
            "HTTPS service errors": d / "ClearScreenHttps.err.log", "Service wrapper": d / "ClearScreenApi.wrapper.log",
            "Installer": d / "install.log"}


def tail(path: Path, lines: int = 400) -> str:
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - 400_000))
            text = f.read().decode("utf-8", errors="replace")
        return "\n".join(text.splitlines()[-lines:])
    except FileNotFoundError:
        return f"(no log yet: {path})"
