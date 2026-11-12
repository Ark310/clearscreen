"""1.5.0 speed: an alert's AI reviews asked at the same time (same decisions as one by one), AI
timing counted as clock time, stored chunk vectors reused for an alert's own text, and every
client's search index loaded once at start-up (one load per client at a time)."""
import re
import threading
import time

import numpy as np
import pytest

from clearscreen import ai_review, api, similarity
from clearscreen.ai_review import APPLICABLE, MANUAL, NOT_APPLICABLE, AIProviderLimit
from clearscreen.matching import ai_timing
from tests.conftest import fake_embed
from tests.test_ai_review import CUR, PREV, answer, cand
from tests.test_api import RECORD, env  # noqa: F401
from tests.test_v14 import _rec


# ------------------------------------------------- AI reviews at the same time ---
def _cases(*ids):
    return [cand(PREV | {"WL_ID": int(i), "Source_ID": i}) for i in ids]


def _slow_chat(decisions: dict, delay=0.2):
    """Answers each previous case by its ID (from the prompt) after `delay`; records concurrency."""
    state = dict(now=0, most=0)
    lock = threading.Lock()

    def chat(system, user):
        case_id = re.search(r"PREVIOUS WHITELISTED CASE \(ID (\w+)\)", user).group(1)
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
        time.sleep(delay)
        with lock:
            state["now"] -= 1
        if decisions[case_id] == "LIMIT":
            raise AIProviderLimit("limit")
        return answer(decision=decisions[case_id], case_id=case_id), {"total_s": delay}
    chat.state = state
    return chat


def _decision_part(out):
    return (out["decision"], out["explanation"], out["strongest_precedent"],
            [(r["previous_case_id"], r["decision"], r.get("confidence"), r["python_checks"]) for r in out["reviews"]])


@pytest.mark.parametrize("decisions", [
    {"1": APPLICABLE, "2": APPLICABLE, "3": APPLICABLE, "4": APPLICABLE, "5": APPLICABLE},
    {"1": APPLICABLE, "2": NOT_APPLICABLE, "3": APPLICABLE, "4": MANUAL, "5": APPLICABLE},
    {"1": NOT_APPLICABLE, "2": NOT_APPLICABLE, "3": MANUAL, "4": NOT_APPLICABLE, "5": MANUAL},
])
def test_reviews_at_the_same_time_decide_exactly_like_one_by_one(decisions):
    cases = _cases(*decisions)
    one, many = _slow_chat(decisions), _slow_chat(decisions)
    t = time.time()
    seq = ai_review.review(CUR, cases, cfg=ai_review.AIReviewConfig(parallel=1), chat=one)
    seq_s, t = time.time() - t, time.time()
    par = ai_review.review(CUR, cases, cfg=ai_review.AIReviewConfig(parallel=5), chat=many)
    par_s = time.time() - t
    assert _decision_part(par) == _decision_part(seq)
    assert [r["previous_case_id"] for r in par["reviews"]] == list(decisions)      # candidates' order kept
    assert one.state["most"] == 1 and many.state["most"] == 5
    assert par_s < seq_s / 2


def test_parallel_never_exceeds_max_cases_or_the_setting():
    decisions = {str(i): APPLICABLE for i in range(1, 8)}
    chat = _slow_chat(decisions, delay=0.1)
    out = ai_review.review(CUR, _cases(*decisions), cfg=ai_review.AIReviewConfig(max_cases=5, parallel=3), chat=chat)
    assert len(out["reviews"]) == 5 and chat.state["most"] == 3


def test_a_usage_limit_still_stops_the_alert_when_asked_together():
    decisions = {"1": APPLICABLE, "2": "LIMIT", "3": APPLICABLE}
    with pytest.raises(AIProviderLimit):
        ai_review.review(CUR, _cases(*decisions), cfg=ai_review.AIReviewConfig(parallel=3), chat=_slow_chat(decisions))


def test_setting_reaches_the_review_and_is_bounded():
    s = api.Settings.load()
    assert api.Settings().ai_parallel_calls == 1                                  # one by one unless changed
    for value, expected in ((5, 5), (0, 1), (50, 10)):
        s.ai_parallel_calls = value
        s.save()
        api.STATE._settings = None
        assert api.STATE.ai_cfg().parallel == expected
    s.ai_parallel_calls = 1
    s.save()
    api.STATE._settings = None


# ------------------------------------------------------------- AI timing ---
def test_ai_timing_counts_overlapping_calls_once():
    spans = [(0.0, 6.0), (0.5, 6.5), (1.0, 7.0)]
    assert ai_timing([{}, {}, {}], spans) == (0.0, 7.0)
    calls = [{"waits": [(0.0, 3.0)]}, {"waits": [(1.0, 4.0)]}, {"waits": []}]
    assert ai_timing(calls, [(0.0, 9.0), (1.0, 10.0), (4.0, 8.0)]) == (4.0, 6.0)


def test_ai_timing_one_by_one_is_the_sum_as_before():
    calls = [{"waits": [(0.0, 21.0)]}, {"waits": []}, {"waits": []}]
    waited, ai = ai_timing(calls, [(0.0, 27.0), (27.0, 33.0), (33.0, 39.0)])
    assert (waited, ai) == (21.0, 18.0)
    assert ai_timing([{"waited_s": 5.0}], [(0.0, 11.0)]) == (5.0, 11.0)         # a chat that gives no wait spans


# ------------------------------------------------ stored vectors reused ---
def test_alert_text_already_embedded_is_not_embedded_again(env):
    cfg = similarity.Gateway2Config()
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec("1", "Lima")])
    env.embed_now()
    stored = similarity.chunk_payload(_rec("1", "Lima")["Matched_Details"], cfg.chunk_max_chars)
    chunks = stored + ["a piece of text nobody has embedded yet"]
    sent = []

    def counting(texts):
        sent.append(list(texts))
        return fake_embed(texts)
    got = similarity.embed_chunks(env.sql, cfg, chunks, counting)
    assert sent == [["a piece of text nobody has embedded yet"]]
    assert np.allclose(got, similarity._unit(fake_embed(chunks)), atol=1e-6)      # same vectors as embedding all
    sent.clear()
    similarity.embed_chunks(env.sql, cfg, stored, counting)
    assert sent == []                                                             # all stored: no embedding call


def test_lookup_failure_falls_back_to_embedding_everything():
    class Broken:
        def cursor(self):
            raise RuntimeError("database down")
    chunks = ["alpha beta", "gamma delta"]
    got = similarity.embed_chunks(Broken(), similarity.Gateway2Config(), chunks, fake_embed)
    assert np.allclose(got, similarity._unit(fake_embed(chunks)))


def test_match_decision_unchanged_by_reuse(env):
    """The same alert decided with reuse and with every chunk embedded afresh."""
    from tests.test_api import ALERT
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec("1", "Lima"), _rec("2", "Quito")])
    env.embed_now()
    alert = ALERT | {"Matched_Details": _rec("1", "Lima")["Matched_Details"]}
    a = similarity.find_candidates(api.db.to_alert(alert | {"ClientID": "C1"}), env.sql, embed=fake_embed)
    real = similarity._vectors_for
    similarity._vectors_for = lambda cur, model, hashes: {}                      # nothing reusable
    try:
        b = similarity.find_candidates(api.db.to_alert(alert | {"ClientID": "C1"}), env.sql, embed=fake_embed)
    finally:
        similarity._vectors_for = real
    assert a["candidates"] and a["candidates"][0]["payload_similarity"] > 99                # a real comparison
    assert [(c["WL_ID"], round(c["payload_similarity"], 4)) for c in a["candidates"] + a["near_misses"]] == \
           [(c["WL_ID"], round(c["payload_similarity"], 4)) for c in b["candidates"] + b["near_misses"]]


# ----------------------------------------------------- index at start-up ---
def test_warm_up_loads_every_client_and_alerts_then_reuse_it(env):
    cfg = similarity.Gateway2Config()
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec(str(i), f"Town{i}") for i in range(3)])
    env.client.post("/whitelist/add", headers=env.h("C2"),
                    json=[_rec(str(i), f"Town{i}") | {"ClientID": "C2"} for i in range(2)])
    env.embed_now()
    similarity._INDEX.clear()
    assert similarity.warm_indexes(env.sql, cfg) == {"C1": 3, "C2": 2}
    kept = similarity._INDEX[(cfg.embed_model, "c1")][1]
    assert similarity.client_index(env.sql, cfg, "C1") is kept                     # nothing reloaded


def test_one_load_of_a_client_index_at_a_time(env):
    cfg = similarity.Gateway2Config()
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec(str(i), f"Town{i}") for i in range(3)])
    env.embed_now()
    similarity._INDEX.clear()
    real, state, lock = similarity._client_index, dict(now=0, most=0, loads=0), threading.Lock()

    def slow(con, cfg_, client_id, key):
        with lock:
            state["now"] += 1
            state["most"] = max(state["most"], state["now"])
        time.sleep(0.3)
        try:
            with lock:
                fresh = key not in similarity._INDEX
                state["loads"] += fresh
            return real(con, cfg_, client_id, key)
        finally:
            with lock:
                state["now"] -= 1
    similarity._client_index = slow
    got = []
    try:
        cons = [api.db.connect(env.store.get("sql_connection_string")) for _ in range(3)]
        threads = [threading.Thread(target=lambda c=c: got.append(similarity.client_index(c, cfg, "C1"))) for c in cons]
        [t.start() for t in threads]
        [t.join() for t in threads]
        [c.close() for c in cons]
    finally:
        similarity._client_index = real
    assert state["most"] == 1 and state["loads"] == 1 and all(g is got[0] for g in got)


def test_api_start_up_warm_up_runs_and_logs(env, caplog):
    env.client.post("/whitelist/add", headers=env.h(), json=[_rec(str(i), f"Town{i}") for i in range(2)])
    env.embed_now()
    similarity._INDEX.clear()
    api.log.propagate = True
    try:
        with caplog.at_level("INFO", logger="clearscreen"):
            api._warm_indexes()
    finally:
        api.log.propagate = False
    assert ("bge-m3", "c1") in similarity._INDEX
    assert "search indexes preloaded: 1 clients" in caplog.text
