# -*- coding: utf-8 -*-
"""Similar-case search: which of a client's whitelisted records are similar enough for the AI to
examine. Python does not decide the whitelist here; it only picks the records to send.

For every whitelisted record OF THE SAME CLIENT (clients never mix):
  base      payload similarity, 0-100: semantic (embedding) similarity between the alert's
            Matched_Details and the record's, compared chunk by chunk so one relevant section
            is not diluted by unrelated text
  penalties on the screened (customer) fields, only when BOTH sides have a value:
            name materially different -15 | DOB different -25 | country different -10
  hard stop entity type different -> record rejected (ENTITY_TYPE_MISMATCH)
  adjusted  max(0, base - penalties); send_to_ai when adjusted >= the threshold (80)
Matching values never add points.

Embeddings: bge-m3 from the local embedding service (Ollama, CPU). Each distinct chunk text is
embedded once (dbo.WL_Chunk_Vectors); dbo.WL_Payload_Chunks lists each record's chunks. New
records are embedded by the background worker (embed_missing); until then only the exact-repeat
check can use them. A client's vectors are kept in memory between alerts and an alert is
compared with all of them in one matrix product.

The decision logic (chunking, similarity, field comparisons, scoring) is the proof of concept's
app/gateway2.py, copied verbatim; only storage is new.
"""
import hashlib
import re
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from functools import lru_cache

import httpx
import numpy as np
import pycountry
from dateutil import parser as dateparser
from rapidfuzz.distance import JaroWinkler

from clearscreen import db


@dataclass
class Gateway2Config:
    ai_threshold: float = 80.0          # adjusted similarity at or above this -> send_to_ai
    name_threshold: float = 85.0        # name similarity below this -> "materially different"
    name_penalty: float = 15.0
    dob_penalty: float = 25.0
    country_penalty: float = 10.0
    chunk_max_chars: int = 600          # longer sections are split at sentence boundaries
    near_misses: int = 5                # best cases below the threshold also returned
    embed_model: str = "bge-m3"
    ollama_url: str = "http://127.0.0.1:11434"
    embed_timeout_s: float = 120.0


# ------------------------------------------------------------------ chunking --
_SECTIONS = re.compile(r"(?=\b(?:Address Information|AKA Listing|Additional Information)\b)")
_SEPARATOR = re.compile(r"-{5,}|_{5,}|={5,}")
_EMPTY_CHUNK = re.compile(r"^(?:Address Information|AKA Listing|Additional Information|AKA|NOT AVAILABLE|\W)*$",
                          re.I)


def chunk_payload(text: str, max_chars: int = 600) -> list[str]:
    """Logical chunks of a LexisNexis listing: the header (name, reference, reason, notes),
    then each address, alias and additional-information block. Any chunk longer than
    max_chars is split at sentence boundaries."""
    chunks = []
    for sect in _SECTIONS.split(text or ""):
        for block in _SEPARATOR.split(sect):
            block = re.sub(r"\s+", " ", block).strip()
            if not block or _EMPTY_CHUNK.match(block):
                continue
            chunks.extend(_split_long(block, max_chars))
    return chunks or [re.sub(r"\s+", " ", text or "").strip()]


def _split_long(block: str, max_chars: int) -> list[str]:
    if len(block) <= max_chars:
        return [block]
    out, cur = [], ""
    for sent in re.split(r"(?<=[.;])\s+", block):
        while len(sent) > max_chars:                      # one sentence longer than a chunk
            out.extend([cur] if cur else [])
            cur, out = "", out + [sent[:max_chars]]
            sent = sent[max_chars:]
        if cur and len(cur) + 1 + len(sent) > max_chars:
            out.append(cur)
            cur = sent
        else:
            cur = f"{cur} {sent}".strip()
    return out + ([cur] if cur else [])


# ---------------------------------------------------------------- embeddings --
def ollama_embedder(cfg: Gateway2Config):
    """The local embedding service. It runs CPU-only (the server has no GPU)."""
    def embed(texts: list[str]) -> list[list[float]]:
        r = httpx.post(f"{cfg.ollama_url.rstrip('/')}/api/embed",
                       json={"model": cfg.embed_model, "input": texts, "options": {"num_gpu": 0}},
                       timeout=cfg.embed_timeout_s)
        r.raise_for_status()
        return r.json()["embeddings"]
    return embed


def _unit(vectors) -> np.ndarray:
    m = np.asarray(vectors, dtype=np.float32)
    n = np.linalg.norm(m, axis=1, keepdims=True)
    return m / np.where(n == 0, 1, n)


def payload_similarity(current: np.ndarray, previous: np.ndarray) -> float:
    """0-100. Every chunk is matched to its most similar chunk on the other side, in both
    directions, and the two averages are averaged: a section that matches well counts in
    full however much unrelated text surrounds it, and text only one side has still costs."""
    sims = current @ previous.T
    score = (sims.max(axis=1).mean() + sims.max(axis=0).mean()) / 2
    return round(float(max(0.0, min(1.0, score))) * 100, 2)


def _hash(text: str) -> str:
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def pending_embeddings(con, cfg: Gateway2Config, client_id=None) -> int:
    """Records (of one client, or all) not embedded yet - not yet usable by the similar-case search."""
    cur = con.cursor()
    sql = ("SELECT COUNT(*) FROM dbo.WL_Records r WHERE NOT EXISTS (SELECT 1 FROM dbo.WL_Payload_Chunks c "
           "WHERE c.RecordKey = r.RecordKey AND c.Model = ?)")
    args = [cfg.embed_model]
    if client_id:
        sql += " AND r.ClientID = ?"
        args.append(client_id)
    return cur.execute(sql, *args).fetchone()[0]


class AlertsFirst:
    """Alerts go first: while an alert is being searched (its own embedding and the client's
    index), the background worker holds back its next batch. The embedding service works on one
    request at a time, so an alert then waits for at most one small batch."""

    def __init__(self):
        self._active = 0
        self._lock = threading.Lock()
        self._idle = threading.Event()
        self._idle.set()

    @contextmanager
    def alert(self):
        with self._lock:
            self._active += 1
            self._idle.clear()
        try:
            yield
        finally:
            with self._lock:
                self._active -= 1
                if self._active == 0:
                    self._idle.set()

    def wait_idle(self, timeout: float | None = None) -> bool:
        """True once no alert is being searched (False if `timeout` passed first)."""
        return self._idle.wait(timeout)


ALERTS_FIRST = AlertsFirst()


def embed_missing(con, embed, cfg: Gateway2Config, client_id=None, batch=64, progress=None, limit=2000,
                  before_batch=None) -> int:
    """Chunk and embed up to `limit` records (of one client, or all) not embedded yet. Each
    distinct chunk text is embedded once. Commits as it goes. Returns the number of records done.
    before_batch(): called before each call to the embedding service; returning False stops early
    (pause) - vectors already made are kept, and the records whose chunks are all done count."""
    cur = con.cursor()
    sql = (f"SELECT TOP ({int(limit)}) r.RecordKey, r.Matched_Details FROM dbo.WL_Records r WHERE NOT EXISTS "
           "(SELECT 1 FROM dbo.WL_Payload_Chunks c WHERE c.RecordKey = r.RecordKey AND c.Model = ?)")
    args = [cfg.embed_model]
    if client_id:
        sql += " AND r.ClientID = ?"
        args.append(client_id)
    missing = cur.execute(sql + " ORDER BY r.RecordKey", *args).fetchall()
    if not missing:
        return 0
    refs, texts = [], {}
    for record_key, details in missing:
        for i, c in enumerate(chunk_payload(details, cfg.chunk_max_chars)):
            h = _hash(c)
            refs.append((record_key, cfg.embed_model, i, h))
            texts.setdefault(h, c)
    have = set()
    hashes = list(texts)
    for start in range(0, len(hashes), 1000):
        part = hashes[start:start + 1000]
        cur.execute(f"SELECT ChunkHash FROM dbo.WL_Chunk_Vectors WHERE Model = ? AND ChunkHash IN "
                    f"({','.join('?' * len(part))})", cfg.embed_model, *part)
        have.update(r[0] for r in cur.fetchall())
    todo = [(h, t) for h, t in texts.items() if h not in have]
    cur.fast_executemany = True
    stopped = False
    for start in range(0, len(todo), batch):
        if before_batch is not None and before_batch() is False:
            stopped = True
            break
        part = todo[start:start + batch]
        vectors = _unit(embed([t for _, t in part]))
        cur.executemany("INSERT INTO dbo.WL_Chunk_Vectors (ChunkHash, Model, Vector) VALUES (?, ?, ?)",
                        [(h, cfg.embed_model, v.tobytes()) for (h, _), v in zip(part, vectors)])
        con.commit()
        have.update(h for h, _ in part)
        if progress:
            progress(min(start + batch, len(todo)), len(todo))
    if stopped:                                       # only records whose every chunk has a vector
        unfinished = {r[0] for r in refs if r[3] not in have}
        refs = [r for r in refs if r[0] not in unfinished]
    # A record replaced meanwhile loses its chunks again (db.upsert_many); refs of deleted records are skipped.
    keys = sorted({r[0] for r in refs})
    alive = set()
    for start in range(0, len(keys), 1000):
        part = keys[start:start + 1000]
        cur.execute(f"SELECT RecordKey FROM dbo.WL_Records WHERE RecordKey IN ({','.join('?' * len(part))})", *part)
        alive.update(r[0] for r in cur.fetchall())
    refs = [r for r in refs if r[0] in alive]
    if refs:
        cur.executemany("INSERT INTO dbo.WL_Payload_Chunks (RecordKey, Model, ChunkNo, ChunkHash) VALUES (?, ?, ?, ?)",
                        refs)
    con.commit()
    return len(alive)


_INDEX: dict = {}       # (model, client) -> (stamp, ClientIndex)
_INDEX_LOCK = threading.Lock()
_BUILD_LOCKS: dict = {}  # (model, client) -> Lock: one load of a client's index at a time (warm-up vs an alert)


@dataclass
class ClientIndex:
    wl_ids: np.ndarray          # RecordKey of each embedded record, ascending
    starts: np.ndarray          # where each record's chunks begin in `refs`
    counts: np.ndarray          # how many chunks each record has
    refs: np.ndarray            # record chunks -> row of `vectors`
    vectors: np.ndarray         # distinct unit chunk vectors
    ref_ids: np.ndarray | None = None   # RecordKey of each entry of `refs` (to extend the index)
    hashes: list | None = None          # ChunkHash of each row of `vectors`


_CHUNKS_OF_CLIENT = ("FROM dbo.WL_Payload_Chunks c JOIN dbo.WL_Records r ON r.RecordKey = c.RecordKey "
                     "WHERE c.Model = ? AND r.ClientID = ?")
_STAMP = ("SELECT COUNT(*), MAX(c.RecordKey), CHECKSUM_AGG(CHECKSUM(c.RecordKey, c.ChunkNo, c.ChunkHash)) "
          + _CHUNKS_OF_CLIENT)


def _vectors_for(cur, model: str, hashes: list[str]) -> dict:
    got = {}
    for start in range(0, len(hashes), 1000):
        part = hashes[start:start + 1000]
        cur.execute(f"SELECT ChunkHash, Vector FROM dbo.WL_Chunk_Vectors WHERE Model = ? AND ChunkHash IN "
                    f"({','.join('?' * len(part))})", model, *part)
        got.update((h, np.frombuffer(bytes(v), dtype=np.float32)) for h, v in cur.fetchall())
    return got


def _assemble(ref_ids: np.ndarray, refs: np.ndarray, vectors: np.ndarray, hashes: list) -> ClientIndex:
    wl_ids, starts, counts = np.unique(ref_ids, return_index=True, return_counts=True)
    return ClientIndex(wl_ids=wl_ids, starts=starts, counts=counts, refs=refs, vectors=vectors,
                       ref_ids=ref_ids, hashes=hashes)


def _extend(old: ClientIndex, cur, cfg: Gateway2Config, rows) -> ClientIndex:
    """`old` plus the chunks of records added after it (all with a higher RecordKey)."""
    hashes = list(old.hashes)
    pos = {h: i for i, h in enumerate(hashes)}
    new = sorted({h for _, h in rows if h not in pos})
    got = _vectors_for(cur, cfg.embed_model, new)
    new = [h for h in new if h in got]
    for h in new:
        pos[h] = len(hashes)
        hashes.append(h)
    rows = [(k, h) for k, h in rows if h in pos]
    vectors = old.vectors
    if new:
        add = np.stack([got[h] for h in new])
        vectors = add if not len(old.vectors) else np.vstack([old.vectors, add])
    ref_ids = np.concatenate([old.ref_ids, np.array([k for k, _ in rows], dtype=np.int64)])
    refs = np.concatenate([old.refs, np.array([pos[h] for _, h in rows], dtype=np.int64)])
    return _assemble(ref_ids, refs, vectors, hashes)


def client_index(con, cfg: Gateway2Config, client_id) -> ClientIndex:
    """One client's chunk vectors, kept in memory until that client's embeddings change. When
    records were only ADDED since (the background worker embedding new records), the kept index
    is extended with just the new ones instead of reading every vector again; a checksum over the
    kept part proves it is unchanged. Anything else (records replaced or removed) rebuilds it.
    Only one thread loads a client's index at a time: an alert that arrives while the start-up
    warm-up (warm_indexes) is loading it waits and then uses that result."""
    key = (cfg.embed_model, client_id.lower())
    with _INDEX_LOCK:
        build_lock = _BUILD_LOCKS.setdefault(key, threading.Lock())
    with build_lock:
        return _client_index(con, cfg, client_id, key)


def _client_index(con, cfg: Gateway2Config, client_id, key) -> ClientIndex:
    cur = con.cursor()
    stamp = tuple(cur.execute(_STAMP, cfg.embed_model, client_id).fetchone())
    with _INDEX_LOCK:
        kept = _INDEX.get(key)
    if kept and kept[0] == stamp:
        return kept[1]
    index = None
    if kept and kept[1].ref_ids is not None and kept[0][1] is not None and stamp[0] > kept[0][0]:
        old_count, old_max, old_sum = kept[0]
        same = cur.execute(_STAMP + " AND c.RecordKey <= ?", cfg.embed_model, client_id, old_max).fetchone()
        if (same[0], same[2]) == (old_count, old_sum):
            rows = cur.execute("SELECT c.RecordKey, c.ChunkHash " + _CHUNKS_OF_CLIENT + " AND c.RecordKey > ? "
                               "ORDER BY c.RecordKey, c.ChunkNo", cfg.embed_model, client_id, old_max).fetchall()
            index = _extend(kept[1], cur, cfg, rows)
    if index is None:
        rows = cur.execute("SELECT c.RecordKey, c.ChunkHash " + _CHUNKS_OF_CLIENT + " ORDER BY c.RecordKey, c.ChunkNo",
                           cfg.embed_model, client_id).fetchall()
        got = {h: np.frombuffer(bytes(v), dtype=np.float32) for h, v in cur.execute(
            "SELECT v.ChunkHash, v.Vector FROM dbo.WL_Chunk_Vectors v WHERE v.Model = ? AND v.ChunkHash IN "
            "(SELECT c.ChunkHash " + _CHUNKS_OF_CLIENT + ")", cfg.embed_model, cfg.embed_model, client_id).fetchall()}
        rows = [(k, h) for k, h in rows if h in got]
        hashes = sorted({h for _, h in rows})
        pos = {h: i for i, h in enumerate(hashes)}
        vectors = (np.stack([got[h] for h in hashes]) if hashes else np.zeros((0, 0), dtype=np.float32))
        index = _assemble(np.array([k for k, _ in rows], dtype=np.int64),
                          np.array([pos[h] for _, h in rows], dtype=np.int64), vectors, hashes)
    with _INDEX_LOCK:
        _INDEX[key] = (stamp, index)
    return index


def embed_chunks(con, cfg: Gateway2Config, chunks: list[str], embed) -> np.ndarray:
    """Unit vectors of `chunks`, in order. A piece of text that is already in WL_Chunk_Vectors
    (the same text in a whitelisted record) reuses its stored vector - the same text always gives
    the same vector - and only the rest is sent to the embedding service. Read-only: an alert's
    own vectors are not stored. If the lookup fails, everything is embedded as before."""
    if not chunks:
        return _unit(embed(chunks))
    hashes = [_hash(c) for c in chunks]
    try:
        stored = _vectors_for(con.cursor(), cfg.embed_model, sorted(set(hashes)))
    except Exception:                                         # noqa: BLE001 -- reuse is only a shortcut
        stored = {}
    todo = list(dict.fromkeys(h for h in hashes if h not in stored))
    if todo:
        text = dict(zip(hashes, chunks))
        stored.update(zip(todo, _unit(embed([text[h] for h in todo]))))
    return np.stack([np.asarray(stored[h], dtype=np.float32) for h in hashes])


def warm_indexes(con, cfg: Gateway2Config, log=None) -> dict:
    """Load every client's index into memory now (the API calls this in the background when it
    starts), so the first alert after a restart does not wait for it. Returns {client: records}."""
    clients = [r[0] for r in con.cursor().execute(
        "SELECT DISTINCT r.ClientID " + _CHUNKS_OF_CLIENT.replace("r.ClientID = ?", "1 = 1"), cfg.embed_model).fetchall()]
    done = {}
    for client in clients:
        started = time.time()
        done[client] = len(client_index(con, cfg, client).wl_ids)
        if log:
            log.info("search index of client %s loaded: %s records in %.1f s", client, done[client],
                     time.time() - started)
    return done


def payload_similarities(current: np.ndarray, index: ClientIndex) -> np.ndarray:
    """payload_similarity of the alert against every row of the index at once, 0-100."""
    if not len(index.wl_ids):
        return np.zeros(0)
    sims = current @ index.vectors.T                                   # alert chunks x distinct chunks
    per_ref = sims[:, index.refs]                                       # alert chunks x row chunks
    best_for_alert = np.maximum.reduceat(per_ref, index.starts, axis=1).mean(axis=0)
    best_for_row = np.add.reduceat(sims.max(axis=0)[index.refs], index.starts) / index.counts
    return np.round(np.clip((best_for_alert + best_for_row) / 2, 0.0, 1.0) * 100, 2)


# --------------------------------------------------------- structured fields --
def _blank(v) -> bool:
    return v is None or not str(v).strip() or str(v).strip().lower() in {"none", "null", "n/a", "na", "unknown", "-"}


def _name_key(name: str) -> str:
    """Upper case, punctuation dropped, words sorted -- so 'EWYLER, JON' equals 'Jon Ewyler'."""
    return " ".join(sorted(re.sub(r"[^\w\s]", " ", name).upper().split()))


def compare_name(current, previous, cfg: Gateway2Config) -> dict:
    if _blank(current) or _blank(previous):
        return dict(compared=False, similarity=None, materially_different=False, penalty=0)
    sim = round(JaroWinkler.normalized_similarity(_name_key(current), _name_key(previous)) * 100, 2)
    different = sim < cfg.name_threshold
    return dict(compared=True, similarity=sim, threshold=cfg.name_threshold, materially_different=different,
                penalty=-cfg.name_penalty if different else 0)


_DEFAULTS = (datetime(1, 1, 1), datetime(2, 2, 2))   # a part that differs between the two was not given


@lru_cache(maxsize=65536)
def normalize_dob(value) -> tuple[int, int | None, int | None] | None:
    """(year, month, day); a part the value does not give is None. '1958-00-00' -> (1958, None, None),
    'circa 1970' -> (1970, None, None), '12 Mar 1985' -> (1985, 3, 12). None when no year is found."""
    if _blank(value):
        return None
    text = str(value).strip()
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        return y, (mo or None), (d or None)
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})", text)
    if m:
        a, b, y = (int(x) for x in m.groups())
        if a > 12:
            return y, b or None, a or None                # day first
        if b > 12 or a == b:
            return y, a or None, b or None                # month first
        return y, None, None                              # 03/04/1985: day/month order unknown
    cleaned = re.sub(r"(?i)\b(circa|approx(imately)?|about|ca\.?)\b", " ", text)
    if re.search(r"\b\d{4}\b", cleaned):
        try:
            a, b = (dateparser.parse(cleaned, default=d, fuzzy=True) for d in _DEFAULTS)
            if a.year == b.year:
                return a.year, (a.month if a.month == b.month else None), (a.day if a.day == b.day else None)
        except (ValueError, OverflowError):
            pass
    years = re.findall(r"\b(1[89]\d{2}|20\d{2})\b", text)
    return (int(years[0]), None, None) if years else None


def compare_dob(current, previous, cfg: Gateway2Config) -> dict:
    a, b = normalize_dob(current), normalize_dob(previous)
    out = dict(current=current, previous=previous,
               current_normalized=_fmt_dob(a), previous_normalized=_fmt_dob(b))
    if a is None or b is None:
        return out | dict(compared=False, material_difference=False, penalty=0)
    # Only parts BOTH sides give are compared: 1970 vs 1970-03-12 is not a difference.
    different = any(x is not None and y is not None and x != y for x, y in zip(a, b))
    return out | dict(compared=True, material_difference=different, penalty=-cfg.dob_penalty if different else 0)


def _fmt_dob(d) -> str | None:
    if d is None:
        return None
    y, m, dd = d
    return f"{y:04d}" + (f"-{m:02d}" if m else "") + (f"-{dd:02d}" if m and dd else "")


_COUNTRY_ALIASES = {
    "burma": "MMR", "russia": "RUS", "uk": "GBR", "great britain": "GBR", "britain": "GBR",
    "england": "GBR", "scotland": "GBR", "wales": "GBR", "northern ireland": "GBR",
    "turkey": "TUR", "turkiye": "TUR", "ivory coast": "CIV", "cote d'ivoire": "CIV",
    "north korea": "PRK", "dprk": "PRK", "south korea": "KOR", "republic of korea": "KOR",
    "korea republic of": "KOR", "laos": "LAO", "lao pdr": "LAO", "syria": "SYR", "iran": "IRN",
    "vietnam": "VNM", "viet nam": "VNM", "macau": "MAC", "macao": "MAC", "taiwan": "TWN",
    "palestine": "PSE", "bolivia": "BOL", "venezuela": "VEN", "tanzania": "TZA", "moldova": "MDA",
    "drc": "COD", "democratic republic of the congo": "COD", "congo": "COG", "uae": "ARE",
    "usa": "USA", "us": "USA", "u.s.": "USA", "u.s.a.": "USA", "united states of america": "USA",
    "czech republic": "CZE", "kosovo": "XKX", "vatican": "VAT", "brunei": "BRN", "cape verde": "CPV",
    "swaziland": "SWZ", "holland": "NLD",
    # "Name, Republic of" style spellings (punctuation is removed before the lookup)
    "china peoples republic of": "CHN", "peoples republic of china": "CHN", "prc": "CHN",
    "iran islamic republic of": "IRN", "islamic republic of iran": "IRN",
    "korea democratic peoples republic of": "PRK", "democratic peoples republic of korea": "PRK",
    "korea south": "KOR", "korea north": "PRK", "lao peoples democratic republic": "LAO",
    "tanzania united republic of": "TZA", "venezuela bolivarian republic of": "VEN",
    "bolivia plurinational state of": "BOL", "moldova republic of": "MDA", "macedonia": "MKD",
    "congo democratic republic of": "COD", "congo republic of": "COG", "taiwan province of china": "TWN",
    "hong kong sar": "HKG", "hong kong special administrative region": "HKG", "macao sar": "MAC",
    "syrian arab republic": "SYR", "russian federation": "RUS", "great britain and northern ireland": "GBR",
    "united kingdom of great britain and northern ireland": "GBR", "united states of america": "USA",
}


@lru_cache(maxsize=65536)
def normalize_country(value) -> str | None:
    """ISO alpha-3 code when recognised ('Canada', 'CA', 'CAN' -> 'CAN'), else the cleaned text."""
    if _blank(value):
        return None
    text = re.sub(r"\s+", " ", str(value)).strip()
    key = text.lower().rstrip(".")
    if key in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[key]
    plain = re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", key.replace("'", ""))).strip()
    if plain in _COUNTRY_ALIASES:
        return _COUNTRY_ALIASES[plain]
    try:
        return pycountry.countries.lookup(text).alpha_3
    except LookupError:
        return text.upper()


def compare_country(current, previous, cfg: Gateway2Config) -> dict:
    a, b = normalize_country(current), normalize_country(previous)
    out = dict(current=current, previous=previous, current_normalized=a, previous_normalized=b)
    if a is None or b is None:
        return out | dict(compared=False, different=False, penalty=0)
    return out | dict(compared=True, different=a != b, penalty=-cfg.country_penalty if a != b else 0)


_ENTITY_TYPES = {
    "individual": "Individual", "person": "Individual", "natural person": "Individual", "people": "Individual",
    "organization": "Organization", "organisation": "Organization", "company": "Organization",
    "business": "Organization", "entity": "Organization", "corporate": "Organization",
    "corporation": "Organization", "corp": "Organization", "firm": "Organization",
    "vessel": "Vessel", "ship": "Vessel", "aircraft": "Aircraft",
}


def normalize_entity_type(value) -> str | None:
    if _blank(value):
        return None
    key = re.sub(r"\s+", " ", str(value)).strip().lower()
    return _ENTITY_TYPES.get(key, key.title())


def compare_entity_type(current, previous) -> dict:
    a, b = normalize_entity_type(current), normalize_entity_type(previous)
    out = dict(current=current, previous=previous, current_normalized=a, previous_normalized=b)
    if a is None or b is None:
        return out | dict(compared=False, different=False)
    return out | dict(compared=True, different=a != b)


# ------------------------------------------------------------------- compare --
def score_candidate(alert: dict, prev: dict, payload_sim: float, cfg: Gateway2Config) -> dict:
    name = compare_name(alert.get("Entity_Name"), prev["Entity_Name"], cfg)
    dob = compare_dob(alert.get("Entity_DOB"), prev["Entity_DOB"], cfg)
    country = compare_country(alert.get("Entity_Country"), prev["Entity_Country"], cfg)
    etype = compare_entity_type(alert.get("Entity_Type"), prev["Entity_Type"])
    rejected = etype["different"]
    adjusted = 0.0 if rejected else round(max(0.0, payload_sim + name["penalty"] + dob["penalty"]
                                              + country["penalty"]), 2)
    return dict(WL_ID=prev["WL_ID"], Source_ID=prev["Source_ID"], payload_similarity=payload_sim,
                name=name, dob=dob, country=country, entity_type=etype,
                candidate_rejected=rejected, rejection_reason="ENTITY_TYPE_MISMATCH" if rejected else None,
                adjusted_similarity=adjusted, send_to_ai=not rejected and adjusted >= cfg.ai_threshold)


def find_candidates(alert: dict, con, cfg: Gateway2Config | None = None, embed=None) -> dict:
    """Compare the alert with every embedded whitelisted record OF THE SAME CLIENT. Returns every
    record with send_to_ai, best first, plus the best `near_misses` records that fell short."""
    cfg = cfg or Gateway2Config()
    client = (alert.get("ClientID") or "").strip()
    if not client:                                    # no client -> nothing it may be compared with
        return dict(send_to_ai=False, threshold=cfg.ai_threshold, compared=0, candidates=[], near_misses=[])
    embed = embed or ollama_embedder(cfg)
    with ALERTS_FIRST.alert():                        # the background worker steps aside meanwhile
        current = embed_chunks(con, cfg, chunk_payload(alert["Matched_Payload"], cfg.chunk_max_chars), embed)
        index = client_index(con, cfg, client)
    sims = payload_similarities(current, index)
    # Penalties only lower the score, so a record below the threshold on payload alone can never be
    # sent: score those at or above it, plus the best of the rest for near_misses.
    order = np.argsort(-sims, kind="stable")
    above = int((sims >= cfg.ai_threshold).sum())
    wanted = {int(index.wl_ids[i]): float(sims[i]) for i in order[:above + cfg.near_misses * 10]}
    rows = {a["WL_ID"]: a for a in db.alerts_by_keys(con, client, list(wanted))}
    scored = [score_candidate(alert, rows[w], s, cfg) for w, s in wanted.items() if w in rows]
    scored.sort(key=lambda c: (c["send_to_ai"], c["adjusted_similarity"], c["payload_similarity"]), reverse=True)
    send = [c for c in scored if c["send_to_ai"]]
    return dict(send_to_ai=bool(send), threshold=cfg.ai_threshold, compared=len(index.wl_ids),
                pending_embeddings=pending_embeddings(con, cfg, client),
                candidates=send, near_misses=[c for c in scored if not c["send_to_ai"]][:cfg.near_misses])


def search(con, cfg: Gateway2Config, client_id: str, text: str, k: int = 10, embed=None) -> list[dict]:
    """The client's whitelisted records whose listing is most similar to free text (GET /search)."""
    embed = embed or ollama_embedder(cfg)
    current = embed_chunks(con, cfg, [text], embed)
    index = client_index(con, cfg, client_id)
    sims = payload_similarities(current, index)
    top = np.argsort(-sims, kind="stable")[:k]
    keys = [int(index.wl_ids[i]) for i in top]
    found = {a["WL_ID"]: a for a in db.alerts_by_keys(con, client_id, keys)}
    return [dict(Unique_ID=found[key]["Source_ID"], similarity=float(sims[i]), customer=found[key]["Entity_Name"],
                 Analyst_Allow_Reasoning=found[key]["Analyst_Remarks"])
            for key, i in zip(keys, top) if key in found]
