# -*- coding: utf-8 -*-
"""Setup steps the installer runs with the ClearScreen Python (as an administrator).

  python -m clearscreen.setup_cli sql-init --server S --database D [--service-login "NT SERVICE\\ClearScreenApi"]
  python -m clearscreen.setup_cli sql-use < connection string on stdin
  python -m clearscreen.setup_cli groq-key            < key on stdin   (tested, then stored encrypted)
  python -m clearscreen.setup_cli settings --domain D [--email E] [--api-port N] [--embed-url U]
  python -m clearscreen.setup_cli pull-model          (downloads bge-m3 into the embedding service)
  python -m clearscreen.setup_cli status              (prints what is configured; no secrets)

Secrets are read from standard input, never from the command line (other processes can read a
command line). Exit code 0 = done, 1 = failed (message on stderr).
"""
import argparse
import json
import re
import sys

import httpx

from clearscreen import db
from clearscreen.admin import ops
from clearscreen.config import SecureStore, Settings

DRIVER = "ODBC Driver 18 for SQL Server"
_NAME = re.compile(r"^[A-Za-z0-9_\-]{1,100}$")


def _conn(server: str, database: str) -> str:
    return (f"Driver={{{DRIVER}}};Server={server};Database={database};Trusted_Connection=yes;"
            "Encrypt=yes;TrustServerCertificate=yes")


def sql_init(a) -> str:
    """Create the database, the tables, and a login for the API service's Windows identity with
    read/write rights only. Runs with the administrator's Windows login."""
    if not _NAME.match(a.database):
        raise SystemExit("Database name: letters, digits, '_' or '-' only.")
    master = db.connect(_conn(a.server, "master"), autocommit=True)
    try:
        cur = master.cursor()
        if cur.execute("SELECT DB_ID(?)", a.database).fetchone()[0] is None:
            cur.execute(f"CREATE DATABASE [{a.database}]")
            print(f"created database {a.database}")
        if a.service_login:
            if cur.execute("SELECT SUSER_ID(?)", a.service_login).fetchone()[0] is None:
                cur.execute(f"CREATE LOGIN [{a.service_login}] FROM WINDOWS")
                print(f"created login {a.service_login}")
    finally:
        master.close()
    cs = _conn(a.server, a.database)
    con = db.connect(cs)
    try:
        db.create_schema(con)
        if a.service_login:
            cur = con.cursor()
            if cur.execute("SELECT USER_ID(?)", a.service_login).fetchone()[0] is None:
                cur.execute(f"CREATE USER [{a.service_login}] FOR LOGIN [{a.service_login}]")
            for role in ("db_datareader", "db_datawriter"):
                cur.execute(f"ALTER ROLE [{role}] ADD MEMBER [{a.service_login}]")
            con.commit()
            print(f"{a.service_login}: read/write on {a.database}")
    finally:
        con.close()
    SecureStore().update(lambda d: d.update(sql_connection_string=cs))
    print("connection saved (encrypted)")
    return cs


def _read_secret() -> str:
    """One line from stdin as UTF-8, without a byte-order mark (Windows PowerShell may add one) or spaces."""
    return sys.stdin.buffer.readline().decode("utf-8-sig", errors="replace").strip().lstrip("﻿")


def sql_use(_a) -> None:
    cs = _read_secret()
    ok, msg = ops.test_connection(cs)
    print(msg)
    if not ok:
        raise SystemExit(1)
    ops.ensure_schema(cs)
    SecureStore().update(lambda d: d.update(sql_connection_string=cs))
    print("connection saved (encrypted)")


def groq_key(_a) -> None:
    key = _read_secret()
    if not key:
        raise SystemExit("No key given.")
    if not key.isascii() or any(c.isspace() for c in key):
        raise SystemExit("That does not look like a Groq key (it contains spaces or unusual characters). "
                         "Copy it again from console.groq.com.")
    ok, msg = ops.test_groq_key(key, Settings.load().groq_model)
    print(msg)
    if not ok:
        raise SystemExit(1)
    SecureStore().update(lambda d: d.update(groq_api_key=key))
    print("Groq key saved (encrypted)")


def settings(a) -> None:
    s = Settings.load()
    if a.domain:
        s.domain = a.domain
    if a.email is not None:
        s.acme_email = a.email
    if a.tls_mode:
        s.tls_mode = a.tls_mode
    if a.api_port:
        s.api_port = a.api_port
    if a.embed_url:
        s.embed_url = a.embed_url
    s.save()
    ops.write_caddyfile(s)
    print("settings saved; HTTPS configuration written")


def tls_cert(a) -> None:
    """Check a certificate + private key (PEM) and install them for the HTTPS service."""
    s = Settings.load()
    try:
        i = ops.install_certificate(a.cert, a.key, s.domain)
    except (ValueError, OSError) as e:
        raise SystemExit(str(e))
    s.tls_mode = "certificate"
    s.save()
    ops.write_caddyfile(s)
    print(f"certificate for {', '.join(i['names'])} ({i['issuer']}), expires {i['expires']} - installed")


def pull_model(_a) -> None:
    s = Settings.load()
    with httpx.stream("POST", s.embed_url.rstrip("/") + "/api/pull", json={"model": s.embed_model},
                      timeout=httpx.Timeout(30, read=1800)) as r:
        r.raise_for_status()
        last = None
        for line in r.iter_lines():
            msg = json.loads(line) if line else {}
            if msg.get("error"):
                raise SystemExit(msg["error"])
            status = msg.get("status")
            if status != last:
                print(status, flush=True)
                last = status
    print(f"{s.embed_model} ready")


def status(_a) -> None:
    st = SecureStore()
    s = Settings.load()
    print(json.dumps({"domain": s.domain, "tls": ops.certificate_status(s), "api_port": s.api_port, "embed_url": s.embed_url,
                      "sql_connection": ops.mask_connection_string(st.get("sql_connection_string")),
                      "groq_key": "set" if st.get("groq_api_key") else "missing",
                      "clients": sorted(st.clients())}, indent=2))


def main(argv=None):
    p = argparse.ArgumentParser(prog="clearscreen.setup_cli")
    sub = p.add_subparsers(dest="cmd", required=True)
    x = sub.add_parser("sql-init")
    x.add_argument("--server", required=True)
    x.add_argument("--database", default="ClearScreen")
    x.add_argument("--service-login", default="NT SERVICE\\ClearScreenApi")
    sub.add_parser("sql-use")
    sub.add_parser("groq-key")
    x = sub.add_parser("settings")
    x.add_argument("--domain")
    x.add_argument("--email")
    x.add_argument("--api-port", type=int)
    x.add_argument("--embed-url")
    x.add_argument("--tls-mode", choices=("certificate", "automatic"))
    x = sub.add_parser("tls-cert")
    x.add_argument("--cert", required=True)
    x.add_argument("--key", required=True)
    sub.add_parser("pull-model")
    sub.add_parser("status")
    a = p.parse_args(argv)
    try:
        {"sql-init": sql_init, "sql-use": sql_use, "groq-key": groq_key, "settings": settings,
         "tls-cert": tls_cert, "pull-model": pull_model, "status": status}[a.cmd](a)
    except SystemExit:
        raise
    except Exception as e:                                        # noqa: BLE001
        print(f"FAILED: {ops._short_error(e) if 'pyodbc' in type(e).__module__ else e}", file=sys.stderr)
        raise SystemExit(1) from e


if __name__ == "__main__":
    main()
