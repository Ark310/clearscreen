# -*- coding: utf-8 -*-
"""SQL Server storage (pyodbc, ODBC Driver 18).

dbo.WL_Records - one row per whitelisted alert, in the client's own columns:
    ClientID, Unique_ID (together the record's identity), Screened_Fields, Screened_Values,
    Database_Screened, Screened_Date, Matched_Details, Analyst_Allow_Reasoning
  plus internal columns: RecordKey (row number used by the embedding cache), RepeatKey
  (fingerprint of the customer + listing, indexed, for the exact-repeat check), CreatedUtc,
  UpdatedUtc.
dbo.WL_Chunk_Vectors, dbo.WL_Payload_Chunks - the embedding cache (clearscreen/similarity.py).

The service's login needs only SELECT/INSERT/UPDATE/DELETE (db_datareader + db_datawriter);
the schema is created by setup or the Admin app with an administrator's login.
"""
import hashlib
import re
from datetime import datetime, timezone

import pyodbc

from clearscreen.screened import parse_screened

COLUMNS = ["ClientID", "Unique_ID", "Screened_Fields", "Screened_Values", "Database_Screened",
           "Screened_Date", "Matched_Details", "Analyst_Allow_Reasoning"]
SCHEMA_VERSION = 1

SCHEMA = [
    """IF OBJECT_ID('dbo.WL_Records', 'U') IS NULL
    CREATE TABLE dbo.WL_Records (
        RecordKey                BIGINT IDENTITY(1,1) NOT NULL,
        ClientID                 NVARCHAR(100)  NOT NULL,
        Unique_ID                NVARCHAR(200)  NOT NULL,
        Screened_Fields          NVARCHAR(MAX)  NOT NULL,
        Screened_Values          NVARCHAR(MAX)  NOT NULL,
        Database_Screened        NVARCHAR(400)  NULL,
        Screened_Date            NVARCHAR(100)  NULL,
        Matched_Details          NVARCHAR(MAX)  NOT NULL,
        Analyst_Allow_Reasoning  NVARCHAR(MAX)  NULL,
        RepeatKey                BINARY(32)     NOT NULL,
        CreatedUtc               DATETIME2(0)   NOT NULL CONSTRAINT DF_WL_Records_Created DEFAULT SYSUTCDATETIME(),
        UpdatedUtc               DATETIME2(0)   NOT NULL CONSTRAINT DF_WL_Records_Updated DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_WL_Records PRIMARY KEY NONCLUSTERED (ClientID, Unique_ID),
        CONSTRAINT UQ_WL_Records_RecordKey UNIQUE CLUSTERED (RecordKey)
    )""",
    """IF NOT EXISTS (SELECT 1 FROM sys.indexes WHERE name = 'IX_WL_Records_Repeat')
    CREATE INDEX IX_WL_Records_Repeat ON dbo.WL_Records (ClientID, RepeatKey)""",
    """IF OBJECT_ID('dbo.WL_Chunk_Vectors', 'U') IS NULL
    CREATE TABLE dbo.WL_Chunk_Vectors (
        ChunkHash  CHAR(40)        NOT NULL,
        Model      NVARCHAR(100)   NOT NULL,
        Vector     VARBINARY(MAX)  NOT NULL,
        CONSTRAINT PK_WL_Chunk_Vectors PRIMARY KEY (ChunkHash, Model)
    )""",
    """IF OBJECT_ID('dbo.WL_Payload_Chunks', 'U') IS NULL
    CREATE TABLE dbo.WL_Payload_Chunks (
        RecordKey  BIGINT         NOT NULL,
        Model      NVARCHAR(100)  NOT NULL,
        ChunkNo    INT            NOT NULL,
        ChunkHash  CHAR(40)       NOT NULL,
        CONSTRAINT PK_WL_Payload_Chunks PRIMARY KEY (RecordKey, Model, ChunkNo),
        CONSTRAINT FK_WL_Payload_Chunks_Record FOREIGN KEY (RecordKey)
            REFERENCES dbo.WL_Records (RecordKey) ON DELETE CASCADE
    )""",
    """IF OBJECT_ID('dbo.CLS_SchemaVersion', 'U') IS NULL
    CREATE TABLE dbo.CLS_SchemaVersion (Version INT NOT NULL, AppliedUtc DATETIME2(0) NOT NULL DEFAULT SYSUTCDATETIME())""",
]


def connect(conn_str: str, autocommit: bool = False) -> pyodbc.Connection:
    con = pyodbc.connect(conn_str, autocommit=autocommit, timeout=15)
    con.timeout = 120                                   # per-query limit, seconds
    return con


def create_schema(con) -> None:
    cur = con.cursor()
    for stmt in SCHEMA:
        cur.execute(stmt)
    if not cur.execute("SELECT 1 FROM dbo.CLS_SchemaVersion WHERE Version = ?", SCHEMA_VERSION).fetchone():
        cur.execute("INSERT INTO dbo.CLS_SchemaVersion (Version) VALUES (?)", SCHEMA_VERSION)
    con.commit()


def rows(cursor) -> list[dict]:
    names = [d[0] for d in cursor.description]
    return [dict(zip(names, r)) for r in cursor.fetchall()]


# ------------------------------------------------------------- fingerprints ---
def _norm(value) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip().casefold()


def _norm_list(value) -> str:
    """A comma-separated list with each item normalized, so ' a , B' equals 'a,b'."""
    return ",".join(_norm(v) for v in str(value or "").split(","))


def repeat_key(screened_fields, screened_values, matched_details) -> bytes:
    """Same value <=> an exact repeat: same fields, values and listing, ignoring case and spacing."""
    text = "\x1f".join((_norm_list(screened_fields), _norm_list(screened_values), _norm(matched_details)))
    return hashlib.sha256(text.encode("utf-8")).digest()


def _clean(v):
    s = "" if v is None else str(v).strip()
    return s or None


# ------------------------------------------------------------------ records ---
def upsert_many(con, records: list[dict]) -> list[str]:
    """Store records (dicts keyed by COLUMNS) in one transaction. A ClientID + Unique_ID pair that
    exists is replaced. Returns "added" / "replaced" per input record, in order. The caller commits."""
    recs = [{c: _clean(r.get(c)) for c in COLUMNS} for r in records]
    for r in recs:
        r["RepeatKey"] = repeat_key(r["Screened_Fields"], r["Screened_Values"], r["Matched_Details"])
    status, last = [], {}
    for i, r in enumerate(recs):                       # the same pair twice in one request: the later wins
        k = (r["ClientID"].lower(), r["Unique_ID"].lower())
        status.append("replaced" if k in last else None)
        last[k] = i
    cur = con.cursor()
    existing = {}
    by_client: dict[str, list[str]] = {}
    for i in last.values():
        by_client.setdefault(recs[i]["ClientID"], []).append(recs[i]["Unique_ID"])
    for client, ids in by_client.items():
        for start in range(0, len(ids), 1000):
            part = ids[start:start + 1000]
            cur.execute(f"SELECT RecordKey, ClientID, Unique_ID, RepeatKey FROM dbo.WL_Records "
                        f"WHERE ClientID = ? AND Unique_ID IN ({','.join('?' * len(part))})", client, *part)
            for rk, c, u, key in cur.fetchall():
                existing[(c.lower(), u.lower())] = (rk, bytes(key))
    inserts, updates, stale = [], [], []
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    for k, i in last.items():
        r = recs[i]
        if k in existing:
            rk, old_key = existing[k]
            updates.append([r[c] for c in COLUMNS[2:]] + [r["RepeatKey"], now, rk])
            if old_key != r["RepeatKey"]:
                stale.append(rk)                       # listing or customer changed: rebuild its embeddings
            status[i] = status[i] or "replaced"
        else:
            inserts.append([r[c] for c in COLUMNS] + [r["RepeatKey"]])
            status[i] = status[i] or "added"
    cur.fast_executemany = True
    if updates:
        cur.executemany(f"UPDATE dbo.WL_Records SET {', '.join(f'{c} = ?' for c in COLUMNS[2:])}, RepeatKey = ?, "
                        "UpdatedUtc = ? WHERE RecordKey = ?", updates)
    if inserts:
        cur.executemany(f"INSERT INTO dbo.WL_Records ({', '.join(COLUMNS)}, RepeatKey) "
                        f"VALUES ({', '.join('?' * (len(COLUMNS) + 1))})", inserts)
    for start in range(0, len(stale), 1000):
        part = stale[start:start + 1000]
        cur.execute(f"DELETE FROM dbo.WL_Payload_Chunks WHERE RecordKey IN ({','.join('?' * len(part))})", *part)
    return [s if s else "added" for s in status]


def get_record(con, client_id, unique_id) -> dict | None:
    cur = con.cursor()
    cur.execute(f"SELECT {', '.join(COLUMNS)}, CreatedUtc, UpdatedUtc FROM dbo.WL_Records "
                "WHERE ClientID = ? AND Unique_ID = ?", str(client_id).strip(), str(unique_id).strip())
    found = rows(cur)
    if not found:
        return None
    rec = found[0]
    for k in ("CreatedUtc", "UpdatedUtc"):
        rec[k] = rec[k].strftime("%Y-%m-%dT%H:%M:%SZ") if rec[k] else None
    return rec


def count_by_client(con) -> dict[str, int]:
    cur = con.cursor()
    cur.execute("SELECT ClientID, COUNT(*) FROM dbo.WL_Records GROUP BY ClientID")
    return {c: n for c, n in cur.fetchall()}


def delete_client_records(con, client_id) -> int:
    cur = con.cursor()
    cur.execute("DELETE FROM dbo.WL_Records WHERE ClientID = ?", client_id)
    return cur.rowcount


def to_alert(row: dict) -> dict:
    """A record or a match request as the matching code reads it: our customer parsed from
    Screened_Fields / Screened_Values, the listing, and the analyst's reasoning."""
    c = parse_screened(row.get("Screened_Fields"), row.get("Screened_Values"))
    return dict(WL_ID=row.get("RecordKey"), ClientID=_clean(row.get("ClientID")), Source_ID=_clean(row.get("Unique_ID")),
                Entity_Name=c["name"], Entity_Type=c["entity_type"], Entity_DOB=c["dob"],
                Entity_Country=c["country"], Entity_Identification=c["identification"], State=c["state"],
                City=c["city"], Matched_Payload=row.get("Matched_Details") or "",
                Analyst_Remarks=_clean(row.get("Analyst_Allow_Reasoning")),
                Screened_Fields=row.get("Screened_Fields") or "", Screened_Values=row.get("Screened_Values") or "")


def alerts_by_keys(con, client_id, record_keys) -> list[dict]:
    """to_alert of the client's records with these RecordKeys (other clients' rows are never returned)."""
    out, keys = [], list(record_keys)
    cur = con.cursor()
    for start in range(0, len(keys), 1000):
        part = keys[start:start + 1000]
        cur.execute(f"SELECT RecordKey, {', '.join(COLUMNS)} FROM dbo.WL_Records "
                    f"WHERE ClientID = ? AND RecordKey IN ({','.join('?' * len(part))})", client_id, *part)
        out += [to_alert(r) for r in rows(cur)]
    return out


def exact_repeats(con, client_id, screened_fields, screened_values, matched_details) -> list[str]:
    """Unique_IDs of the client's records that this alert exactly repeats (indexed lookup)."""
    cur = con.cursor()
    cur.execute("SELECT Unique_ID FROM dbo.WL_Records WHERE ClientID = ? AND RepeatKey = ? ORDER BY RecordKey",
                client_id, repeat_key(screened_fields, screened_values, matched_details))
    return [r[0] for r in cur.fetchall()]


_NO_DETAILS = re.compile(r"^\W*entity\s+details\s+not\s+available\W*$", re.I)


def no_listing_details(matched_details) -> bool:
    """LexisNexis gave no details of what was matched (e.g. 'Entity Details NOT AVAILABLE')."""
    text = re.sub(r"\s+", " ", str(matched_details or "")).strip()
    return not text or bool(_NO_DETAILS.match(text))
