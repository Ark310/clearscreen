# -*- coding: utf-8 -*-
"""ClearScreen HTTP API.

  GET  /health                                    service status (more with a valid key)
  POST /whitelist/add                             add or replace whitelist records (one or a list)
  GET  /whitelist/{ClientID}/{Unique_ID}          one stored record
  POST /match/main                                check alerts: the decision only (what clients use)
  POST /match/test                                the same check with every internal detail (timings, the
                                                  AI provider, candidates, reviews). Only when switched on in
                                                  the Admin app (Settings); otherwise 404 and not in /docs.
  GET  /search                                    the client's records most similar to free text

Every call except /health needs an X-API-Key. A key belongs to one client and works only for
that client's records: a ClientID in a request must be the key's client (CLIENT_MISMATCH).
Errors are {"error_code", "message"}; nothing internal (paths, stack traces, SQL) is returned.
Client-facing text never names the AI provider: only /match/test (internal) does.
The API listens on 127.0.0.1 only; the HTTPS proxy (Caddy) is what faces the internet.
"""
import logging
import threading
import time
import uuid
from contextlib import asynccontextmanager
from logging.handlers import RotatingFileHandler

from fastapi import Body, Depends, FastAPI, Query, Request, Security
from fastapi.exceptions import RequestValidationError
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse
from fastapi.security import APIKeyHeader
from starlette.exceptions import HTTPException as StarletteHTTPException

from clearscreen import PRODUCT, __version__, ai_review, db, groq_client, similarity
from clearscreen.config import SecureStore, Settings, logs_dir
from clearscreen.embedding_queue import EmbeddingWorker
from clearscreen.matching import match_alert
from clearscreen.models import (MAIN_FIELDS, AddResult, ErrorResponse, MatchDecision, MatchRequest, MatchResult,
                                 SearchHit, WLRecord)
from clearscreen.security import RateLimiter, client_for_key

log = logging.getLogger("clearscreen")
access = logging.getLogger("clearscreen.access")


class State:
    """What every request shares: settings, secrets, rate limits."""

    def __init__(self):
        self.store = SecureStore()
        self.limiter = RateLimiter()
        self._settings, self._settings_at = None, 0.0

    @property
    def settings(self) -> Settings:
        if self._settings is None or time.monotonic() - self._settings_at > 10:   # re-read every 10 s
            self._settings, self._settings_at = Settings.load(), time.monotonic()
        return self._settings

    def connect(self):
        cs = self.store.get("sql_connection_string")
        if not cs:
            raise ServiceError(503, "DATABASE_NOT_CONFIGURED", "No SQL Server connection is configured.")
        try:
            return db.connect(cs)
        except Exception as e:                                    # noqa: BLE001
            log.error("database connection failed: %s", type(e).__name__)
            raise ServiceError(503, "DATABASE_UNAVAILABLE", "The database is not reachable right now.") from e

    def sim_cfg(self) -> similarity.Gateway2Config:
        s = self.settings
        return similarity.Gateway2Config(embed_model=s.embed_model, ollama_url=s.embed_url)

    def ai_cfg(self) -> ai_review.AIReviewConfig:
        s = self.settings
        return ai_review.AIReviewConfig(max_cases=s.ai_max_cases, parallel=min(10, max(1, int(s.ai_parallel_calls or 1))))

    def groq_cfg(self) -> groq_client.GroqConfig:
        s = self.settings
        return groq_client.GroqConfig(model=s.groq_model, reasoning_effort=s.groq_reasoning_effort)


STATE = State()


class ServiceError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        self.status, self.code, self.message, self.extra = status, code, message, extra


def _error(status: int, code: str, message: str, headers=None, **extra) -> JSONResponse:
    return JSONResponse(status_code=status, content={"error_code": code, "message": message, **extra}, headers=headers)


# ------------------------------------------------------------ logging -------
def _setup_logging():
    if log.handlers:
        return
    logs_dir().mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    for logger, name in ((log, "api.log"), (access, "access.log")):
        h = RotatingFileHandler(logs_dir() / name, maxBytes=10_000_000, backupCount=10, encoding="utf-8")
        h.setFormatter(fmt)
        logger.addHandler(h)
        logger.setLevel(logging.INFO)
        logger.propagate = False


# ------------------------------------------------- background embedding -----
_stop = threading.Event()


def _embed_worker():
    """Embed new or replaced whitelist records in the background, so adding records returns at
    once. Until a record is embedded only the exact-repeat check can use it. Pause / resume,
    alerts first and progress: clearscreen.embedding_queue."""
    EmbeddingWorker(STATE.connect, STATE.sim_cfg, _stop).run(lambda: STATE.settings.embed_worker_seconds)


def _warm_indexes():
    """Load every client's search index into memory in the background at start-up, so the first
    alert after a restart does not have to. The API answers meanwhile; an alert that needs an index
    still being loaded waits for that load (similarity.client_index) instead of starting another."""
    started = time.time()
    for attempt in range(10):                    # SQL Server may still be starting with the machine
        try:
            con = STATE.connect()
            break
        except ServiceError:
            if _stop.wait(30):
                return
    else:
        log.warning("search indexes not preloaded: the database was not reachable")
        return
    try:
        done = similarity.warm_indexes(con, STATE.sim_cfg(), log)
        log.info("search indexes preloaded: %s clients in %.1f s", len(done), time.time() - started)
    except Exception as e:                                        # noqa: BLE001 -- alerts load it themselves
        log.warning("search indexes not preloaded: %s", type(e).__name__)
    finally:
        con.close()


@asynccontextmanager
async def lifespan(app):
    _setup_logging()
    log.info("%s %s starting", PRODUCT, __version__)
    threading.Thread(target=_warm_indexes, name="index-warmup", daemon=True).start()
    worker = threading.Thread(target=_embed_worker, name="embed-worker", daemon=True)
    worker.start()
    yield
    _stop.set()
    log.info("%s stopping", PRODUCT)


_settings_at_start = Settings.load()
app = FastAPI(
    title=PRODUCT, version=__version__, lifespan=lifespan,
    description="Checks watchlist screening alerts against each client's own whitelist of earlier analyst "
                "decisions. Every call needs the client's **X-API-Key** (click *Authorize*). A key works only "
                "for its own client's records. Results are advisory: a human makes the final call.",
    docs_url="/docs" if _settings_at_start.enable_docs else None,
    redoc_url=None, openapi_url="/openapi.json" if _settings_at_start.enable_docs else None,
)


# ------------------------------------------------------- error handlers -----
@app.exception_handler(ServiceError)
async def _service_error(request: Request, exc: ServiceError):
    headers = {"Retry-After": str(exc.extra.pop("retry_after"))} if "retry_after" in exc.extra else None
    return _error(exc.status, exc.code, exc.message, headers=headers, **exc.extra)


@app.exception_handler(RequestValidationError)
async def _validation_error(request: Request, exc: RequestValidationError):
    """A missing or blank ClientID gets its own error code; anything else is INVALID_REQUEST."""
    missing = []
    for e in exc.errors():
        loc = [p for p in e.get("loc", ()) if p != "body"]
        if loc and loc[-1] == "ClientID":
            at = next((p for p in loc if isinstance(p, int)), None)
            missing.append("the request" if at is None else f"record {at}")
    if missing:
        where = sorted(set(missing), key=str)
        return _error(400, "MISSING_CLIENT_ID",
                      "ClientID is required: whitelist records are only matched within the same client. "
                      f"Missing or blank in {', '.join(where[:20])}{' ...' if len(where) > 20 else ''}.")
    errors = [{"where": ".".join(str(p) for p in e.get("loc", ()) if p != "body"), "problem": e.get("msg")}
              for e in exc.errors() if e.get("type") != "list_type" and "list[" not in str(e.get("loc", ""))]
    return _error(422, "INVALID_REQUEST", "The request does not match the expected format.", errors=errors[:50])


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    codes = {404: "NOT_FOUND", 405: "METHOD_NOT_ALLOWED"}
    return _error(exc.status_code, codes.get(exc.status_code, "HTTP_ERROR"), str(exc.detail))


@app.exception_handler(Exception)
async def _unexpected(request: Request, exc: Exception):
    ref = uuid.uuid4().hex[:12]
    log.exception("unexpected error ref=%s on %s %s", ref, request.method, request.url.path)
    return _error(500, "INTERNAL_ERROR", f"Something went wrong. Reference: {ref}")


# ------------------------------------------------- size limit + access log --
@app.middleware("http")
async def _guard(request: Request, call_next):
    started = time.perf_counter()
    limit = STATE.settings.max_request_mb * 1024 * 1024
    length = request.headers.get("content-length")
    if length and length.isdigit() and int(length) > limit:
        response = _error(413, "REQUEST_TOO_LARGE", f"Requests may be at most {STATE.settings.max_request_mb} MB.")
    else:
        response = await call_next(request)
    access.info("%s %s %s status=%s client=%s ms=%d", request.client.host if request.client else "-",
                request.method, request.url.path, response.status_code,
                getattr(request.state, "client_id", "-"), (time.perf_counter() - started) * 1000)
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


# --------------------------------------------------------- authentication ---
_api_key = APIKeyHeader(name="X-API-Key", auto_error=False,
                        description="The client's API key, from the ClearScreen Admin app.")


def require_client(request: Request, key: str | None = Security(_api_key)) -> str:
    """The calling client, from its API key. Runs before the body is checked, so a request without
    a valid key gets 401 and learns nothing else."""
    client_id, client = client_for_key(STATE.store, key)
    if client_id is None:
        raise ServiceError(401, "INVALID_API_KEY", "A valid X-API-Key header is required.")
    if not client.get("enabled", True):
        raise ServiceError(403, "CLIENT_DISABLED", "This client is disabled. Contact your ClearScreen administrator.")
    request.state.client_id = client_id
    ok, wait = STATE.limiter.allow(client_id, STATE.settings.rate_limit_per_minute)
    if not ok:
        raise ServiceError(429, "RATE_LIMITED", f"Too many requests. Try again in {wait:.0f} s.",
                           retry_after=max(1, int(wait + 0.999)))
    return client_id


def _same_client(client_id: str, records) -> None:
    """Every record must belong to the key's client."""
    wrong = [i for i, r in enumerate(records) if r.ClientID.strip().lower() != client_id.lower()]
    if wrong:
        where = "the request" if len(records) == 1 else "record(s) " + ", ".join(map(str, wrong[:20]))
        raise ServiceError(403, "CLIENT_MISMATCH",
                           f"The ClientID in {where} is not the client this API key belongs to.")


# --------------------------------------------------------------- endpoints ---
AI_LIMIT_MESSAGE = "The AI review's usage limit was reached. Send the alerts in not_run again later."
AI_NOT_CONFIGURED_MESSAGE = "The AI review is not available on this server. Contact your ClearScreen administrator."


def _errors(*codes, ai: bool = False, full: bool = False) -> dict:
    """The error responses a route can give, each with a real example, for the API documentation.
    ai: a /match route (the AI errors apply). full: /match/test, whose limit error also carries
    the AI provider's own message."""
    what = {
        400: ("A record has no ClientID", [("MISSING_CLIENT_ID", "ClientID is required: whitelist records are only "
                                            "matched within the same client. Missing or blank in the request.")]),
        401: ("Missing or wrong API key", [("INVALID_API_KEY", "A valid X-API-Key header is required.")]),
        403: ("The key's client may not do this", [
            ("CLIENT_MISMATCH", "The ClientID in the request is not the client this API key belongs to."),
            ("CLIENT_DISABLED", "This client is disabled. Contact your ClearScreen administrator.")]),
        404: ("No such record", [("NOT_FOUND", "No whitelist record 1 for client CLIENT-1.")]),
        413: ("Too much in one request", [("BATCH_TOO_LARGE", "600 alerts; send at most 500 per request."),
                                          ("REQUEST_TOO_LARGE", "Requests may be at most 100 MB.")]),
        422: ("A field is missing or malformed", [("INVALID_REQUEST", "The request does not match the expected format.")]),
        429: ("Slow down", [("RATE_LIMITED", "Too many requests. Try again in 12 s."),
                            ("AI_LIMIT_REACHED", AI_LIMIT_MESSAGE)]),
        503: ("A part of the service is unavailable", [
            ("DATABASE_UNAVAILABLE", "The database is not reachable right now."),
            ("AI_NOT_CONFIGURED", AI_NOT_CONFIGURED_MESSAGE)]),
    }
    out = {}
    for c in codes:
        title, examples = what[c]
        examples = [e for e in examples if ai or not e[0].startswith("AI_")]
        ex = {code: {"summary": code, "value": {"error_code": code, "message": msg}} for code, msg in examples}
        if c == 422:
            ex["INVALID_REQUEST"]["value"]["errors"] = [{"where": "Matched_Details", "problem": "Field required"}]
        if c == 429 and ai:
            ex["AI_LIMIT_REACHED"]["value"].update(completed=[], not_run=["ALERT-9001"])
            if full:
                ex["AI_LIMIT_REACHED"]["value"]["detail"] = "Groq limit reached (Groq says retry in 60 min)"
        out[c] = {"model": ErrorResponse, "description": f"{title}: " + " or ".join(e[0] for e in examples),
                  "content": {"application/json": {"examples": ex}}}
    return out


_LISTING = ("Entity   Name: ACME TRADING LIMITED   Reference No.:10001   Reason: SDGT   Notes:Program: SDGT   "
            "Address Information    ---------------------------------------    Address Type: Unknown   "
            "City: Tehran   Country: Iran")
_RECORD = {"ClientID": "CLIENT-1", "Unique_ID": "1",
           "Screened_Fields": "Beneficiary Name,Beneficiary Country,Beneficiary City,Beneficiary Province",
           "Screened_Values": "ACME TRADING LTD,Canada,Toronto,Ontario",
           "Database_Screened": "OFAC SDN.BDF", "Screened_Date": "6/12/2014 2:00:00 PM", "Matched_Details": _LISTING,
           "Analyst_Allow_Reasoning": "False match - customer is a Canadian company in Toronto; listed entity is in Iran."}
_ALERT = {k: v for k, v in _RECORD.items() if k != "Analyst_Allow_Reasoning"} | {"Unique_ID": "ALERT-9001"}
ADD_EXAMPLES = {"single": {"summary": "One record", "value": _RECORD},
                "batch": {"summary": "Several records", "value": [_RECORD, _RECORD | {
                    "Unique_ID": "2", "Screened_Values": "ACME TRADING LTD,Canada,Ottawa,Ontario"}]}}
MATCH_EXAMPLES = {"single": {"summary": "One alert", "value": _ALERT},
                  "batch": {"summary": "Several alerts", "value": [_ALERT, _ALERT | {"Unique_ID": "ALERT-9002"}]}}


@app.get("/health", tags=["service"])
def health(key: str | None = Security(_api_key)):
    """Service status. Without a key: whether the parts are up. With a valid key: also the
    caller's whitelist size and records still waiting to be embedded."""
    out = {"status": "ok", "product": PRODUCT, "version": __version__, "components": {}}
    comp = out["components"]
    try:
        con = STATE.connect()
        con.cursor().execute("SELECT 1").fetchone()
        comp["database"] = "ok"
    except ServiceError as e:
        con, comp["database"] = None, e.code.lower()
    try:
        import httpx
        httpx.get(STATE.settings.embed_url.rstrip("/") + "/api/version", timeout=3).raise_for_status()
        comp["embedding_service"] = "ok"
    except Exception:                                             # noqa: BLE001
        comp["embedding_service"] = "unavailable"
    comp["ai_service"] = "configured" if STATE.store.get("groq_api_key") else "not_configured"
    if comp["database"] != "ok" or comp["embedding_service"] != "ok" or comp["ai_service"] != "configured":
        out["status"] = "degraded"
    client_id, client = client_for_key(STATE.store, key)
    if con is not None and client_id:
        out["client"] = {"ClientID": client_id, "name": client.get("name"),
                         "whitelist_records": db.count_by_client(con).get(client_id, 0),
                         "pending_embeddings": similarity.pending_embeddings(con, STATE.sim_cfg(), client_id)}
    if con is not None:
        con.close()
    return out


@app.post("/whitelist/add", response_model=AddResult, tags=["whitelist"],
          responses=_errors(400, 401, 403, 413, 422, 429, 503))
def whitelist_add(body: WLRecord | list[WLRecord] = Body(openapi_examples=ADD_EXAMPLES),
                  client_id: str = Depends(require_client)):
    """Add whitelisted records - one, or a list (e.g. an Excel sheet converted to JSON). A record is
    identified by ClientID + Unique_ID; sending the same pair again replaces the stored record.
    All records of a request are saved, or none. New records are usable by the exact-repeat check at
    once and by the similar-case search after background embedding (see /health)."""
    records = body if isinstance(body, list) else [body]
    if len(records) > STATE.settings.max_add_batch:
        raise ServiceError(413, "BATCH_TOO_LARGE", f"{len(records)} records; send at most "
                                                   f"{STATE.settings.max_add_batch} per request.")
    _same_client(client_id, records)
    con = STATE.connect()
    try:
        status = db.upsert_many(con, [r.model_dump() | {"ClientID": client_id} for r in records])
        con.commit()
    finally:
        con.close()
    return AddResult(added=status.count("added"), replaced=status.count("replaced"),
                     records=[dict(ClientID=client_id, Unique_ID=r.Unique_ID.strip(), status=s)
                              for r, s in zip(records, status)])


@app.get("/whitelist/{ClientID}/{Unique_ID}", tags=["whitelist"], responses=_errors(401, 403, 404, 429, 503))
def whitelist_get(ClientID: str, Unique_ID: str, client_id: str = Depends(require_client)):  # noqa: N803
    """One stored whitelist record of the caller's client."""
    if ClientID.strip().lower() != client_id.lower():
        raise ServiceError(403, "CLIENT_MISMATCH", "This API key belongs to another client.")
    con = STATE.connect()
    try:
        rec = db.get_record(con, client_id, Unique_ID)
    finally:
        con.close()
    if rec is None:
        raise ServiceError(404, "NOT_FOUND", f"No whitelist record {Unique_ID} for client {client_id}.")
    return rec


def _match(body, client_id: str, full: bool) -> list:
    """Shared by /match/main and /match/test: the same checks and decisions; `full` keeps every
    internal detail, otherwise only the decision fields (MAIN_FIELDS) are returned."""
    alerts = body if isinstance(body, list) else [body]
    if len(alerts) > STATE.settings.max_match_batch:
        raise ServiceError(413, "BATCH_TOO_LARGE", f"{len(alerts)} alerts; send at most "
                                                   f"{STATE.settings.max_match_batch} per request.")
    _same_client(client_id, alerts)
    try:
        chat = groq_client.groq_chat(STATE.store, STATE.groq_cfg())
    except groq_client.GroqNotConfigured as e:
        raise ServiceError(503, "AI_NOT_CONFIGURED", str(e) if full else AI_NOT_CONFIGURED_MESSAGE) from e
    model = f"groq {STATE.settings.groq_model}"

    def shape(r: MatchResult):
        return r if full else MatchDecision(**r.model_dump(include=set(MAIN_FIELDS)))
    done = []
    con = STATE.connect()
    try:
        for a in alerts:
            done.append(match_alert(a.model_dump() | {"ClientID": client_id}, con, chat=chat, model=model,
                                    sim_cfg=STATE.sim_cfg(), ai_cfg=STATE.ai_cfg()))
    except groq_client.GroqLimitReached as e:
        extra = dict(completed=[r.model_dump(exclude={"comparison_trace"}) if full else shape(r).model_dump()
                                for r in done],
                     not_run=[a.Unique_ID for a in alerts[len(done):]])
        if full:
            extra["detail"] = str(e)
        raise ServiceError(429, "AI_LIMIT_REACHED", AI_LIMIT_MESSAGE, **extra) from e
    finally:
        con.close()
    return [shape(r) for r in done]


def _test_endpoint_on():
    """/match/test exists only while it is switched on (Admin > Settings); otherwise it looks like
    any unknown address - before the key or the body are even looked at."""
    if not STATE.settings.test_endpoint_enabled:
        raise StarletteHTTPException(404, "Not Found")


@app.post("/match/main", response_model=list[MatchDecision], tags=["match"],
          responses=_errors(400, 401, 403, 413, 422, 429, 503, ai=True))
def match_main(body: MatchRequest | list[MatchRequest] = Body(openapi_examples=MATCH_EXAMPLES),
               client_id: str = Depends(require_client)):
    """Check alerts against the client's whitelist and return the decision on each: disposition,
    confidence, the rule that decided, the explanation and, when a whitelisted record was relied on,
    that record (precedent) and its analyst reasoning. Only whitelist records of the key's client
    are used. If the AI review's usage limit is reached, the request fails with AI_LIMIT_REACHED;
    the decisions finished before that are included (completed) and the rest are listed (not_run)."""
    return _match(body, client_id, full=False)


@app.post("/match/test", response_model=list[MatchResult], tags=["match"], dependencies=[Depends(_test_endpoint_on)],
          responses=_errors(400, 401, 403, 413, 422, 429, 503, ai=True, full=True))
def match_test(body: MatchRequest | list[MatchRequest] = Body(openapi_examples=MATCH_EXAMPLES),
               client_id: str = Depends(require_client)):
    """INTERNAL - the same check as /match/main with every detail: the AI provider and model (Groq),
    AI calls, timings, cost, the similar-case candidates, each AI review and the score trace. Only
    available while switched on in the ClearScreen Admin app (Settings > Test endpoint)."""
    return _match(body, client_id, full=True)


@app.get("/search", response_model=list[SearchHit], tags=["whitelist"], responses=_errors(401, 403, 422, 429, 503))
def search(q: str = Query(..., min_length=2, max_length=2000, description="Free text, e.g. a name or listing text"),
           k: int = Query(10, ge=1, le=50), client_id: str = Depends(require_client)):
    """The caller's whitelisted records whose listing is most similar to the text."""
    con = STATE.connect()
    try:
        return similarity.search(con, STATE.sim_cfg(), client_id, q, k)
    finally:
        con.close()


# ------------------------------------------------------ API documentation ---
_OPENAPI: dict = {}


def _openapi() -> dict:
    """The /docs page and /openapi.json. /match/test is listed only while it is switched on, so
    with it off the published description never mentions the internal endpoint or its models."""
    shown = bool(STATE.settings.test_endpoint_enabled)
    if shown not in _OPENAPI:
        routes = [r for r in app.routes if shown or getattr(r, "path", None) != "/match/test"]
        _OPENAPI[shown] = get_openapi(title=app.title, version=app.version, description=app.description,
                                      routes=routes)
    return _OPENAPI[shown]


app.openapi = _openapi
