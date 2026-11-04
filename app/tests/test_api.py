"""The HTTP API end to end (SQL Server test database; embedding service and Groq faked): API keys
and client isolation, the error codes, the matching flow, Groq limits."""
import pytest
from fastapi.testclient import TestClient

from clearscreen import api, groq_client, similarity
from clearscreen.security import add_client, reset_key, set_enabled
from tests.conftest import TEST_SQL, fake_embed
from tests.test_ai_review import answer, rationale

FIELDS = "Beneficiary Name,Beneficiary Country,Beneficiary City,Beneficiary Province"
LISTING = ("Entity Name: JON EWYLER Reference No.:12345 Reason: SDGT Notes:Program: SDGT "
           "Address Information ---------- Address Type: Unknown City: Pico Country: Panama")
RECORD = dict(ClientID="C1", Unique_ID="1", Screened_Fields=FIELDS, Screened_Values="Jon Ewyler,Canada,Toronto,Ontario",
              Database_Screened="OFAC SDN.BDF", Screened_Date="6/12/2014 2:00:00 PM", Matched_Details=LISTING,
              Analyst_Allow_Reasoning="Different country and city than the listing.")
ALERT = {k: v for k, v in RECORD.items() if k != "Analyst_Allow_Reasoning"} | {"Unique_ID": "9001"}
# Whitelisted: customer in Lima, listing in Pico (both Panama). New alert: same, new province.
WL_LIMA = RECORD | {"Screened_Values": "Jon Ewyler,Panama,Lima,Lima", "Analyst_Allow_Reasoning": "Different city."}
NEW_LIMA = ALERT | {"Unique_ID": "9002", "Screened_Values": "Jon Ewyler,Panama,Lima,Panama Oeste"}


class _Resp:
    def __init__(self, status, body=None, headers=None, text=""):
        self.status_code, self._body, self.headers, self.text = status, body, headers or {}, text

    def json(self):
        return self._body


def _ok(reply):
    return _Resp(200, {"choices": [{"message": {"content": reply}, "finish_reason": "stop"}],
                       "usage": {"prompt_tokens": 2000, "completion_tokens": 800}})


@pytest.fixture
def env(sql, monkeypatch):
    """API with an empty test database, clients C1 and C2, faked embeddings and Groq."""
    store = api.STATE.store
    store.update(lambda d: (d.clear(), d.update(sql_connection_string=TEST_SQL, groq_api_key="gsk_test")))
    keys = {"C1": add_client(store, "C1", "Client One"), "C2": add_client(store, "C2", "Client Two")}
    api.STATE.limiter = api.RateLimiter()
    monkeypatch.setattr(similarity, "ollama_embedder", lambda cfg: fake_embed)
    replies = []
    real = groq_client.groq_chat

    def post(url, **kw):
        post.calls.append(kw)
        return replies.pop(0)
    post.calls = []
    monkeypatch.setattr(groq_client, "groq_chat", lambda store, cfg=None, post_=None: real(store, cfg, post=post))
    client = TestClient(api.app)

    class Env:
        pass
    e = Env()
    e.client, e.keys, e.replies, e.groq_calls, e.store, e.sql = client, keys, replies, post.calls, store, sql
    e.h = lambda c="C1": {"X-API-Key": keys[c]}

    def embed_now():
        similarity.embed_missing(sql, fake_embed, similarity.Gateway2Config())
    e.embed_now = embed_now
    return e


# ---------------------------------------------------------------- API keys ---
@pytest.mark.parametrize("method,path", [("POST", "/whitelist/add"), ("GET", "/whitelist/C1/1"),
                                         ("POST", "/match/groq"), ("GET", "/search?q=acme")])
def test_every_route_needs_a_valid_key(env, method, path):
    for headers in ({}, {"X-API-Key": "cls_wrong"}, {"X-API-Key": "not-a-key"}):
        r = env.client.request(method, path, headers=headers, json=RECORD)
        assert r.status_code == 401 and r.json()["error_code"] == "INVALID_API_KEY"


def test_reset_key_stops_the_old_one(env):
    old = env.keys["C1"]
    new = reset_key(env.store, "C1")
    assert env.client.get("/whitelist/C1/1", headers={"X-API-Key": old}).status_code == 401
    assert env.client.get("/whitelist/C1/1", headers={"X-API-Key": new}).status_code == 404


def test_disabled_client_refused(env):
    set_enabled(env.store, "C1", False)
    r = env.client.post("/match/groq", headers=env.h(), json=ALERT)
    assert r.status_code == 403 and r.json()["error_code"] == "CLIENT_DISABLED"


def test_rate_limit(env, monkeypatch):
    s = api.STATE.settings
    monkeypatch.setattr(s, "rate_limit_per_minute", 2)
    codes = [env.client.get("/whitelist/C1/x", headers=env.h()).status_code for _ in range(3)]
    assert codes[:2] == [404, 404] and codes[2] == 429
    assert env.client.get("/whitelist/C1/x", headers=env.h()).headers.get("Retry-After")


# ----------------------------------------------------------- client isolation ---
def test_a_key_works_only_for_its_own_client(env):
    r = env.client.post("/whitelist/add", headers=env.h("C1"), json=RECORD | {"ClientID": "C2"})
    assert r.status_code == 403 and r.json()["error_code"] == "CLIENT_MISMATCH"
    r = env.client.post("/match/groq", headers=env.h("C1"), json=[ALERT, ALERT | {"ClientID": "C2"}])
    assert r.status_code == 403 and "record(s) 1" in r.json()["message"]
    env.client.post("/whitelist/add", headers=env.h("C2"), json=RECORD | {"ClientID": "C2"})
    assert env.client.get("/whitelist/C2/1", headers=env.h("C1")).json()["error_code"] == "CLIENT_MISMATCH"


def test_client_id_is_case_insensitive_and_stored_canonically(env):
    r = env.client.post("/whitelist/add", headers=env.h(), json=RECORD | {"ClientID": " c1 "})
    assert r.status_code == 200 and r.json()["records"][0]["ClientID"] == "C1"


def test_other_clients_whitelist_never_used(env):
    env.client.post("/whitelist/add", headers=env.h("C2"), json=[WL_LIMA | {"ClientID": "C2"}, RECORD | {"ClientID": "C2"}])
    env.embed_now()
    [r] = env.client.post("/match/groq", headers=env.h("C1"), json=NEW_LIMA).json()
    assert (r["disposition"], r["rule"]) == ("REVIEW", "no_similar_whitelisted")
    [r] = env.client.post("/match/groq", headers=env.h("C1"), json=ALERT).json()
    assert r["rule"] != "exact_repeat_whitelisted"


# ----------------------------------------------------------------- errors ---
@pytest.mark.parametrize("path", ["/whitelist/add", "/match/groq"])
@pytest.mark.parametrize("bad", [{k: v for k, v in RECORD.items() if k != "ClientID"}, RECORD | {"ClientID": "  "},
                                 RECORD | {"ClientID": None}])
def test_missing_client_id(env, path, bad):
    r = env.client.post(path, headers=env.h(), json=bad)
    assert r.status_code == 400 and r.json()["error_code"] == "MISSING_CLIENT_ID"


def test_invalid_request_and_unknown_route(env):
    r = env.client.post("/match/groq", headers=env.h(), json={k: v for k, v in ALERT.items() if k != "Matched_Details"})
    assert r.status_code == 422 and r.json()["error_code"] == "INVALID_REQUEST"
    assert env.client.get("/nope").json()["error_code"] == "NOT_FOUND"


def test_request_too_large(env, monkeypatch):
    monkeypatch.setattr(api.STATE.settings, "max_request_mb", 0)
    r = env.client.post("/whitelist/add", headers=env.h(), json=RECORD)
    assert r.status_code == 413 and r.json()["error_code"] == "REQUEST_TOO_LARGE"


def test_internal_error_hides_details(env, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("secret internal detail")
    monkeypatch.setattr(api.db, "get_record", boom)
    r = TestClient(api.app, raise_server_exceptions=False).get("/whitelist/C1/1", headers=env.h())
    assert r.status_code == 500 and r.json()["error_code"] == "INTERNAL_ERROR"
    assert "secret" not in r.text and "Reference" in r.json()["message"]


def test_database_down_is_503(env, monkeypatch):
    env.store.update(lambda d: d.update(sql_connection_string="Driver={ODBC Driver 18 for SQL Server};Server=127.0.0.1,1;"
                                                              "Database=x;Trusted_Connection=yes;TrustServerCertificate=yes"))
    r = env.client.get("/whitelist/C1/1", headers=env.h())
    assert r.status_code == 503 and r.json()["error_code"] == "DATABASE_UNAVAILABLE"


# --------------------------------------------------------------- whitelist ---
def test_add_get_replace(env):
    assert env.client.post("/whitelist/add", headers=env.h(), json=[RECORD, RECORD | {"Unique_ID": "2"}]).json()["added"] == 2
    r = env.client.post("/whitelist/add", headers=env.h(), json=RECORD | {"Analyst_Allow_Reasoning": "corrected"}).json()
    assert (r["added"], r["replaced"]) == (0, 1)
    got = env.client.get("/whitelist/C1/1", headers=env.h()).json()
    assert got["Analyst_Allow_Reasoning"] == "corrected" and got["Matched_Details"] == LISTING


def test_health_public_and_with_key(env):
    r = env.client.get("/health").json()
    assert r["product"] == "ClearScreen" and r["components"]["database"] == "ok" and "client" not in r
    env.client.post("/whitelist/add", headers=env.h(), json=RECORD)
    r = env.client.get("/health", headers=env.h()).json()
    assert r["client"]["whitelist_records"] == 1 and r["client"]["pending_embeddings"] == 1


def test_search_returns_only_own_client(env):
    env.client.post("/whitelist/add", headers=env.h(), json=RECORD)
    env.client.post("/whitelist/add", headers=env.h("C2"), json=RECORD | {"ClientID": "C2", "Unique_ID": "c2-only"})
    env.embed_now()
    hits = env.client.get("/search", headers=env.h(), params={"q": "JON EWYLER Pico Panama"}).json()
    assert [h["Unique_ID"] for h in hits] == ["1"]


# ------------------------------------------------------------------- match ---
def test_exact_repeat_cleared_with_its_reasoning(env):
    env.client.post("/whitelist/add", headers=env.h(), json=RECORD)
    [r] = env.client.post("/match/groq", headers=env.h(), json=ALERT | {"Screened_Values": " jon  ewyler ,CANADA,Toronto,Ontario"}).json()
    assert (r["disposition"], r["rule"], r["repeat_of"], r["Unique_ID"]) == ("CLEAR", "exact_repeat_whitelisted", ["1"], "9001")
    assert r["precedent_reasoning"] == RECORD["Analyst_Allow_Reasoning"] and env.groq_calls == []


def test_score_below_threshold_cleared(env):
    [r] = env.client.post("/match/groq", headers=env.h(), json=ALERT).json()
    assert (r["disposition"], r["rule"], r["score"]) == ("CLEAR", "score_below_threshold", 0.74)


def test_no_listing_details_reviewed_unless_exact_repeat(env):
    nd = " Entity Details NOT AVAILABLE"
    [r] = env.client.post("/match/groq", headers=env.h(), json=ALERT | {"Matched_Details": nd}).json()
    assert (r["disposition"], r["rule"]) == ("REVIEW", "no_listing_details")
    env.client.post("/whitelist/add", headers=env.h(), json=RECORD | {"Matched_Details": nd})
    [r] = env.client.post("/match/groq", headers=env.h(), json=ALERT | {"Matched_Details": nd}).json()
    assert r["rule"] == "exact_repeat_whitelisted"


def test_not_embedded_yet_means_no_similar_case(env):
    env.client.post("/whitelist/add", headers=env.h(), json=WL_LIMA)          # not embedded yet
    [r] = env.client.post("/match/groq", headers=env.h(), json=NEW_LIMA).json()
    assert r["rule"] == "no_similar_whitelisted" and r["similar"]["pending_embeddings"] == 1


def test_ai_applicable_clears_citing_the_record(env):
    env.replies.append(_ok(answer(case_id="1", rationales=[rationale("LOCATION_MISMATCH")], confidence=93)))
    env.client.post("/whitelist/add", headers=env.h(), json=WL_LIMA)
    env.embed_now()
    [r] = env.client.post("/match/groq", headers=env.h(), json=NEW_LIMA).json()
    assert (r["disposition"], r["rule"], r["precedent"], r["confidence"]) == ("CLEAR", "prior_adjudication_applicable", "1", 0.93)
    assert r["precedent_reasoning"] == "Different city." and r["ai_calls"] == 1 and r["model"].startswith("groq")
    assert env.groq_calls[0]["json"]["model"] == "qwen/qwen3.8-27b"
    assert r["cost_usd"] == pytest.approx((2000 * 0.80 + 800 * 4.00) / 1e6)


def test_ai_not_applicable_flagged(env):
    env.replies.append(_ok(answer(decision="PRIOR_ADJUDICATION_NOT_APPLICABLE", case_id="1",
                                  rationales=[rationale("LOCATION_MISMATCH")])))
    env.client.post("/whitelist/add", headers=env.h(), json=WL_LIMA)
    env.embed_now()
    [r] = env.client.post("/match/groq", headers=env.h(), json=NEW_LIMA).json()
    assert (r["disposition"], r["rule"]) == ("FLAG", "prior_adjudication_not_applicable")


def test_groq_limit_is_its_own_error_with_what_was_done(env):
    env.replies.append(_Resp(429, headers={"retry-after": "3600"}, text="Rate limit reached: tokens per day"))
    env.client.post("/whitelist/add", headers=env.h(), json=WL_LIMA)
    env.embed_now()
    r = env.client.post("/match/groq", headers=env.h(), json=[ALERT, NEW_LIMA, NEW_LIMA | {"Unique_ID": "9003"}])
    body = r.json()
    assert r.status_code == 429 and body["error_code"] == "GROQ_LIMIT_REACHED" and body["message"] == "Groq limit reached"
    assert [c["Unique_ID"] for c in body["completed"]] == ["9001"] and body["not_run"] == ["9002", "9003"]


def test_groq_not_configured(env):
    env.store.update(lambda d: d.pop("groq_api_key"))
    r = env.client.post("/match/groq", headers=env.h(), json=ALERT)
    assert r.status_code == 503 and r.json()["error_code"] == "GROQ_NOT_CONFIGURED"


def test_secrets_never_in_responses(env):
    texts = [env.client.get("/health", headers=env.h()).text, env.client.get("/openapi.json").text]
    assert not any("gsk_test" in t or env.keys["C1"] in t or "Trusted_Connection" in t for t in texts)
