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
from clearscreen.config import Settings, config_dir, data_dir, logs_dir
from clearscreen.excel import read_records

INSTALL_DIR = Path(os.environ.get("CLS_INSTALL_DIR", Path(os.environ.get("ProgramFiles", r"C:\Program Files")) / "ClearScreen"))


# ------------------------------------------------------------------ HTTPS ---
def caddyfile(s: Settings) -> str:
    """The HTTPS front door: automatic Let's Encrypt certificate for the domain, security headers,
    request size limit, everything passed to the API on 127.0.0.1. No access log (it would record
    the X-API-Key header); the API keeps its own access log without keys."""
    caddy_data = (data_dir() / "caddy").as_posix()
    email = f"\n\temail {s.acme_email}" if s.acme_email else ""
    return f"""# Written by ClearScreen Admin - changes here are overwritten.
{{{email}
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

{s.domain} {{
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
