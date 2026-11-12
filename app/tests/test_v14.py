"""1.4.0: /match/main (decision only) and the switchable /match/test, no AI provider named in
anything client-facing, the embedding queue (pause, alerts first, progress) and the search index
that grows with new records instead of being rebuilt."""
import json
import threading
import time

import numpy as np
import pytest

from clearscreen import api, embedding_queue, similarity
from clearscreen.models import MAIN_FIELDS
from tests.conftest import fake_embed
from tests.test_api import ALERT, NEW_LIMA, RECORD, WL_LIMA, _Resp, _ok, answer, env, rationale  # noqa: F401

MAIN = {"ClientID", "Unique_ID", "disposition", "confidence", "rule", "explanation", "score", "repeat_of",
        "precedent", "precedent_reasoning"}


def _switch(on: bool):
    s = api.Settings.load()
    s.test_endpoint_enabled = on
    s.save()
    api.STATE._settings = None
    api._OPENAPI.clear()


def _applicable(case_id="1"):
    return _ok(answer(case_id=case_id, rationales=[rationale("LOCATION_MISMATCH")], confidence=90))


# -------------------------------------------------------------- /match/main ---
def test_main_fields_are_exactly_the_agreed_ones():
    assert set(MAIN_FIELDS) == MAIN


def test_main_returns_only_the_decision_fields(env):
    env.client.post("/whitelist/add", headers=env.h(), json=[RECORD, WL_LIMA | {"Unique_ID": "2"}])
    env.embed_now()
    env.replies.extend([_applicable("2")] * 5)
    r = env.client.post("/match/main", headers=env.h(), json=[ALERT, NEW_LIMA])
    assert r.status_code == 200
    for item in r.json():
        assert set(item) == MAIN
    repeat, ai = r.json()
    assert (repeat["disposition"], repeat["rule"], repeat["repeat_of"]) == ("CLEAR", "exact_repeat_whitelisted", ["1"])
    assert (ai["disposition"], ai["rule"], ai["precedent"]) == ("CLEAR", "prior_adjudication_applicable", "2")


def test_main_and_test_decide_the_same(env):
    env.client.post("/whitelist/add", headers=env.h(), json=WL_LIMA)
    env.embed_now()
    env.replies.extend([_applicable(), _applicable()])
    [main] = env.client.post("/match/main", headers=env.h(), json=NEW_LIMA).json()
    [full] = env.client.post("/match/test", headers=env.h(), json=NEW_LIMA).json()
    assert {k: full[k] for k in MAIN} == main
    assert full["ai_calls"] == 1 and full["model"].startswith("groq")


# -------------------------------------------------------- the test switch ---
def test_test_endpoint_off_is_invisible(env):
    _switch(False)
    for headers in (env.h(), {}, {"X-API-Key": "cls_wrong"}):
        r = env.client.post("/match/test", headers=headers, json=ALERT)
        assert r.status_code == 404 and r.json()["error_code"] == "NOT_FOUND"
    spec = env.client.get("/openapi.json").json()
    assert "/match/test" not in spec["paths"] and "/match/main" in spec["paths"]
    assert "MatchResult" not in spec["components"]["schemas"]


def test_test_endpoint_on_is_listed_and_works(env):
    _switch(True)
    assert "/match/test" in env.client.get("/openapi.json").json()["paths"]
    assert env.client.post("/match/test", headers=env.h(), json=ALERT).status_code == 200
    _switch(False)
    assert "/match/test" not in env.client.get("/openapi.json").json()["paths"]


# ------------------------------------------------ no provider in client view ---
def test_nothing_client_facing_names_the_provider(env):
    _switch(False)
    env.client.post("/whitelist/add", headers=env.h(), json=[RECORD, WL_LIMA | {"Unique_ID": "2"}])
    env.embed_now()
    env.replies.extend([_applicable("2")] * 5)
    texts = [env.client.get("/openapi.json").text, env.client.get("/docs").text,
             env.client.get("/health").text, env.client.get("/health", headers=env.h()).text,
             env.client.post("/match/main", headers=env.h(), json=[ALERT, NEW_LIMA]).text,
             env.client.get("/search?q=ewyler", headers=env.h()).text,
             env.client.get("/whitelist/C1/1", headers=env.h()).text]
    env.replies.append(_Resp(429, headers={"retry-after": "3600"}, text="Rate limit reached: tokens per day"))
    limited = env.client.post("/match/main", headers=env.h(), json=[ALERT, NEW_LIMA | {"Unique_ID": "9009"}])
    texts.append(limited.text)
    env.store.update(lambda d: d.pop("groq_api_key"))
    texts.append(env.client.post("/match/main", headers=env.h(), json=ALERT).text)
    texts.append(env.client.get("/health").text)
    for t in texts:
        assert "groq" not in t.lower() and "qwen" not in t.lower(), t[:300]


def test_main_limit_error_is_neutral_and_trimmed(env):
    env.client.post("/whitelist/add", headers=env.h(), json=WL_LIMA)
    env.embed_now()
    env.replies.append(_Resp(429, headers={"retry-after": "3600"}, text="Rate limit reached: tokens per day"))
    r = env.client.post("/match/main", headers=env.h(), json=[ALERT, NEW_LIMA])
    body = r.json()
    assert r.status_code == 429 and body["error_code"] == "AI_LIMIT_REACHED" and "detail" not in body
    assert [set(c) for c in body["completed"]] == [MAIN] and body["not_run"] == ["9002"]


def test_main_not_configured_is_neutral(env):
    env.store.update(lambda d: d.pop("groq_api_key"))
    r = env.client.post("/match/main", headers=env.h(), json=ALERT)
    assert r.status_code == 503 and r.json()["error_code"] == "AI_NOT_CONFIGURED"
    assert env.client.get("/health").json()["components"]["ai_service"] == "not_configured"


# ------------------------------------------------------ the growing index ---
def _rec(uid, city):
    return RECORD | {"Unique_ID": uid, "Matched_Details": RECORD["Matched_Details"] + f" City: {city} {uid}"}


def _same(a, b):
    order_a, order_b = np.argsort(a.wl_ids), np.argsort(b.wl_ids)
    assert list(a.wl_ids) == list(b.wl_ids) and list(a.counts) == list(b.counts)
    va = [a.vectors[a.refs[s:s + n]] for s, n in zip(a.starts, a.counts)]
    vb = [b.vectors[b.refs[s:s + n]] for s, n in zip(b.starts, b.counts)]
    assert all(np.allclose(x, y) for x, y in zip(va, vb)) and len(order_a) == len(order_b)


def test_index_grows_with_new_records_and_matches_a_rebuild(env):
    cfg = similarity.Gateway2Config()
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec(str(i), f"Town{i}") for i in range(5)])
    env.embed_now()
    first = similarity.client_index(env.sql, cfg, "C1")
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec(str(i), f"Town{i}") for i in range(5, 9)])
    env.embed_now()
    reads = []
    real = similarity._vectors_for
    similarity._vectors_for = lambda cur, model, hashes: reads.append(len(hashes)) or real(cur, model, hashes)
    try:
        grown = similarity.client_index(env.sql, cfg, "C1")
    finally:
        similarity._vectors_for = real
    assert len(grown.wl_ids) == 9 and reads and max(reads) < len(grown.hashes)   # only the new vectors were read
    assert first is not grown
    similarity._INDEX.clear()
    _same(grown, similarity.client_index(env.sql, cfg, "C1"))


def test_index_rebuilds_when_a_record_is_replaced(env):
    cfg = similarity.Gateway2Config()
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec(str(i), f"Town{i}") for i in range(4)])
    env.embed_now()
    similarity.client_index(env.sql, cfg, "C1")
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec("1", "Elsewhere"), _rec("7", "Town7")])
    env.embed_now()
    kept = similarity.client_index(env.sql, cfg, "C1")
    similarity._INDEX.clear()
    _same(kept, similarity.client_index(env.sql, cfg, "C1"))


# ---------------------------------------------------------- embedding queue ---
def test_embed_missing_stops_between_batches_and_keeps_finished_records(env):
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec(str(i), f"Town{i}") for i in range(6)])
    cfg = similarity.Gateway2Config()
    calls = []

    def before():
        calls.append(1)
        return len(calls) <= 2                        # two batches, then "pause"
    done = similarity.embed_missing(env.sql, fake_embed, cfg, client_id="C1", batch=2, before_batch=before)
    left = similarity.pending_embeddings(env.sql, cfg, "C1")
    assert 0 <= done < 6 and left == 6 - done
    assert similarity.embed_missing(env.sql, fake_embed, cfg, client_id="C1") == left   # the rest, nothing redone
    assert similarity.pending_embeddings(env.sql, cfg, "C1") == 0


class _Settings:
    def __init__(self, paused=False):
        self.embedding_paused = paused


def _worker(env, paused):
    flag = _Settings(paused)
    w = embedding_queue.EmbeddingWorker(lambda: __import__("clearscreen.db", fromlist=["x"]).connect(
        __import__("tests.conftest", fromlist=["x"]).TEST_SQL), similarity.Gateway2Config, threading.Event(),
        settings=lambda: flag, embedder=lambda cfg: fake_embed)
    return w, flag


def test_worker_pause_resume_and_progress(env):
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec(str(i), f"Town{i}") for i in range(3)])
    env.client.post("/whitelist/add", headers=env.h("C2"), json=[_rec(str(i), f"Town{i}") | {"ClientID": "C2"}
                                                                for i in range(2)])
    cfg = similarity.Gateway2Config()
    queue = embedding_queue.queue_by_client(env.sql, cfg.embed_model)
    assert [(q["ClientID"], q["waiting"]) for q in queue] == [("C1", 3), ("C2", 2)]
    assert [r["Unique_ID"] for r in embedding_queue.waiting_records(env.sql, cfg.embed_model, "C2")] == ["0", "1"]

    w, flag = _worker(env, paused=True)
    assert w.step() is False and embedding_queue.read_status()["state"] == "paused"
    assert similarity.pending_embeddings(env.sql, cfg) == 5             # nothing embedded while paused
    flag.embedding_paused = False
    while w.step():
        pass
    status = embedding_queue.read_status()
    assert similarity.pending_embeddings(env.sql, cfg) == 0 and status["state"] == "idle"
    assert status["clients"]["C1"]["done"] == 3 and status["clients"]["C2"]["done"] == 2
    assert status["clients"]["C1"]["finished_utc"] and status["clients"]["C1"]["seconds"] >= 0
    assert embedding_queue.estimate({"chars": 5000}, None, {"s_per_kchar": 2.0}) == 10.0


def test_worker_waits_while_an_alert_is_searched(env):
    w, _ = _worker(env, paused=False)
    finished = []
    with similarity.ALERTS_FIRST.alert():
        t = threading.Thread(target=lambda: finished.append(w._may_continue()))
        t.start()
        time.sleep(1.2)
        assert not finished                           # the worker is held back
    t.join(5)
    assert finished == [True]
