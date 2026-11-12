# -*- coding: utf-8 -*-
"""The embedding queue: whitelist records waiting to be embedded for the similar-case search.

The API service's background worker (EmbeddingWorker) works through the queue oldest first, one
client at a time, in small steps:
  * Pause / resume: Settings.embedding_paused (set from the Admin app's Embedding tab). A pause
    takes effect after the current small batch and survives restarts until resumed.
  * Alerts go first: while an alert is being searched, the worker waits (similarity.ALERTS_FIRST).
  * Progress: per client, the records embedded and the time spent in the current run, written to
    logs\\embedding_status.json (the API service may write only there). The Admin app reads it and
    asks SQL Server what is still waiting.
"""
import json
import logging
import os
import time
from datetime import datetime, timezone

from clearscreen import similarity
from clearscreen.config import Settings, _atomic_write, logs_dir

log = logging.getLogger("clearscreen")

STEP_RECORDS = 100          # records per step: the waiting count moves often, a pause is quick
BATCH_CHUNKS = 16           # text pieces per call to the embedding service: an alert waits at most this long

_NOT_EMBEDDED = ("NOT EXISTS (SELECT 1 FROM dbo.WL_Payload_Chunks c WHERE c.RecordKey = r.RecordKey "
                 "AND c.Model = ?)")


def status_path():
    return logs_dir() / "embedding_status.json"


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def read_status() -> dict:
    """What the worker last wrote; {} before it ever ran."""
    try:
        return json.loads(status_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


# ------------------------------------------------------------------ queries --
def queue_by_client(con, model: str) -> list[dict]:
    """Clients with records waiting, oldest waiting first."""
    rows = con.cursor().execute(
        "SELECT r.ClientID, COUNT(*), MIN(r.RecordKey), MIN(r.UpdatedUtc), SUM(CAST(LEN(r.Matched_Details) AS BIGINT)) "
        f"FROM dbo.WL_Records r WHERE {_NOT_EMBEDDED} GROUP BY r.ClientID ORDER BY MIN(r.RecordKey)", model).fetchall()
    return [dict(ClientID=c, waiting=n, first_key=k, oldest_utc=t, chars=int(ch or 0)) for c, n, k, t, ch in rows]


def waiting_records(con, model: str, client_id: str, limit: int = 1000) -> list[dict]:
    """A client's waiting records in the order they will be embedded."""
    rows = con.cursor().execute(
        f"SELECT TOP ({int(limit)}) r.Unique_ID, r.UpdatedUtc, LEN(r.Matched_Details) FROM dbo.WL_Records r "
        f"WHERE r.ClientID = ? AND {_NOT_EMBEDDED} ORDER BY r.RecordKey", client_id, model).fetchall()
    return [dict(Unique_ID=u, added_utc=t, chars=n) for u, t, n in rows]


def estimate(client: dict, stats: dict | None, overall: dict | None) -> float | None:
    """Seconds left for a client's waiting records: its own pace in this run (seconds per 1,000
    characters of listing, as listing length drives the time), else the worker's overall pace."""
    for pace in ((stats or {}).get("s_per_kchar"), (overall or {}).get("s_per_kchar")):
        if pace:
            return client["chars"] / 1000 * pace
    return None


# ------------------------------------------------------------------- worker --
class EmbeddingWorker:
    def __init__(self, connect, sim_cfg, stop_event, settings=Settings.load, embedder=None):
        self.connect, self.sim_cfg, self.stop, self.settings = connect, sim_cfg, stop_event, settings
        self.embedder = embedder or similarity.ollama_embedder
        old = read_status()
        self.clients = old.get("clients", {})        # keeps "time taken" across a restart
        self.overall = old.get("overall", {})
        self.state, self.message = "starting", ""

    # -- status file --
    def _write(self, state=None, message=None, current=None):
        if state:
            self.state = state
        if message is not None:
            self.message = message
        doc = dict(updated_utc=_now(), state=self.state, message=self.message, current_client=current,
                   clients=self.clients, overall=self.overall, pid=os.getpid())
        try:
            _atomic_write(status_path(), json.dumps(doc, indent=1, default=str).encode("utf-8"))
        except OSError as e:
            log.warning("embedding queue: cannot write %s: %s", status_path(), e)

    def paused(self) -> bool:
        try:
            return bool(self.settings().embedding_paused)
        except Exception:                                          # noqa: BLE001 - unreadable settings: keep going
            return False

    def _may_continue(self) -> bool:
        """Before each batch: stop for a pause or shutdown; wait while alerts are being searched."""
        while not similarity.ALERTS_FIRST.wait_idle(0.5):
            if self.stop.is_set():
                return False
        return not (self.stop.is_set() or self.paused())

    # -- one pass --
    def step(self) -> bool:
        """Embed the next few records. True when it did something (call again soon)."""
        if self.paused():
            self._write("paused", "Paused from the Admin app. Records stay in the queue until resumed.")
            return False
        cfg = self.sim_cfg()
        con = self.connect()
        try:
            queue = queue_by_client(con, cfg.embed_model)
            waiting = {q["ClientID"]: q for q in queue}
            for cid, st in self.clients.items():             # runs that finished since the last pass
                if cid not in waiting and st.get("run_started_utc") and not st.get("finished_utc"):
                    st["finished_utc"] = _now()
                    st["last_run"] = {k: st.get(k) for k in ("done", "seconds", "run_started_utc", "finished_utc")}
            if not queue:
                self._write("idle", "Nothing waiting.")
                return False
            q = queue[0]
            cid = q["ClientID"]
            st = self.clients.setdefault(cid, {})
            if not st.get("run_started_utc") or st.get("finished_utc"):
                self.clients[cid] = st = dict(run_started_utc=_now(), done=0, seconds=0.0, kchars=0.0,
                                              last_run=st.get("last_run"))
            self._write("running", f"Embedding client {cid}: {q['waiting']:,} record(s) waiting.", current=cid)
            before = {r["Unique_ID"]: r["chars"] for r in waiting_records(con, cfg.embed_model, cid, STEP_RECORDS)}
            started = time.monotonic()
            done = similarity.embed_missing(con, self.embedder(cfg), cfg, client_id=cid, batch=BATCH_CHUNKS,
                                            limit=STEP_RECORDS, before_batch=self._may_continue)
            spent = time.monotonic() - started
            left = {r["Unique_ID"] for r in waiting_records(con, cfg.embed_model, cid, STEP_RECORDS)}
            kchars = sum(n for u, n in before.items() if u not in left) / 1000
            st["done"] = st.get("done", 0) + done
            st["seconds"] = round(st.get("seconds", 0.0) + spent, 1)
            st["kchars"] = round(st.get("kchars", 0.0) + kchars, 1)
            if st["kchars"] > 0:
                st["s_per_kchar"] = round(st["seconds"] / st["kchars"], 4)
            if kchars > 0:                                    # the worker's overall pace, recent-weighted
                pace = spent / kchars
                old = self.overall.get("s_per_kchar")
                self.overall["s_per_kchar"] = round(pace if not old else 0.7 * old + 0.3 * pace, 4)
            if done:
                log.info("embedding queue: client %s +%d record(s) in %.1f s", cid, done, spent)
            if self.paused():
                self._write("paused", "Paused from the Admin app. Records stay in the queue until resumed.")
                return False
            self._write("running", f"Embedding client {cid}.", current=cid)
            return True
        finally:
            con.close()

    def run(self, idle_seconds) -> None:
        while not self.stop.is_set():
            try:
                busy = self.step()
            except Exception as e:                                # noqa: BLE001
                name, text = type(e).__name__, str(getattr(e, "message", None) or e)[:200]
                if name != "ServiceError":                         # not configured yet: stay quiet
                    log.warning("embedding queue: %s: %s", name, text)
                self._write("error", f"{text} - retrying shortly." if name == "ServiceError"
                            else f"{name}: {text} - retrying shortly.")
                busy = False
            if not busy:
                self.stop.wait(5 if self.state == "paused" else idle_seconds())
