import time, json, logging, os, sys
from contextlib import contextmanager
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from schemas import QueryRequest, RAGResponse, RAGAnswer, FILTER_KEYS
from retriever import retrieve, rerank, rerank_fell_back, _fs, embed_query
from generator import generate, generate_stream, _client as _gen_client
from config import settings
from auth import verify_iap, enforce_membership
from cost import price
from router import classify
from breakers import choose_model
from budget import record, spend_pct
import semantic_cache                     # 12.6's answer cache, behind SEMANTIC_CACHE=on (the RAG plan, W4)

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)  # bare JSON -> Cloud Run jsonPayload
log = logging.getLogger("documind-api")

trace.set_tracer_provider(TracerProvider())
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.project_id)))
tracer = trace.get_tracer(__name__)


def _ms(since: float) -> int:
    return int((time.perf_counter() - since) * 1000)


@contextmanager
def stage(stages: dict, name: str):
    """One stage of an answer on its own clock AND its own span. A p95 that moved says nothing about
    which stage moved it, so the row carries retrieve_ms / rerank_ms / generate_ms beside latency_ms
    (tenant_daily reads them, 12.3; make usage groups them), and the same names are spans in Cloud
    Trace for the one slow request someone is looking at."""
    t = time.perf_counter()
    with tracer.start_as_current_span(name):
        try:
            yield
        finally:
            stages[f"{name}_ms"] = _ms(t)


app = FastAPI(title="DocuMind API", version="1.0.0")
FastAPIInstrumentor.instrument_app(app)
# 12.6: gen_ai spans - what ran, not what was said. telemetry.py instruments the google-genai SDK with content capture
# off (NO_CONTENT), so a trace carries the model, the tokens, the latency and the finish reason and never a prompt.
# The import is guarded: an instrumentation that cannot load is a missing span, never a missing API.
try:
    import telemetry  # noqa: E402,F401
except Exception as _e:  # noqa: BLE001
    log.warning(json.dumps({"event": "telemetry_not_instrumented", "error": type(_e).__name__}))
# 9.4's Media Studio, adopted (gap G8): /v1/media/generate and /v1/media/upload-url, behind the
# same verify_iap and the same roster check, writing the same usage row shape with modality=image.
from media import router as media_router  # noqa: E402
app.include_router(media_router)

app.add_middleware(CORSMiddleware,
    allow_origins=["https://documind.example.com"],
    allow_methods=["POST","GET"], allow_headers=["*"])

# verify_iap and enforce_membership live in auth.py: identity is verified
# against the IAP assertion, and the tenant is checked against the Firestore
# roster rather than compared to a header the caller sent.

def modality_of(kinds) -> str:
    """What the answer was made from (gap G8). A single video segment makes the row
    'video'; a figure or a table makes it 'image'; otherwise text. tenant_daily groups
    by it, so media spend per tenant is a query, not a guess."""
    kinds = set(kinds)
    if "segment" in kinds:
        return "video"
    if kinds & {"figure", "table"}:
        return "image"
    return "text"


def choose_model_for(query: str) -> str:
    """10.3, wired behind a flag. ROUTING=on: the classifier (router.py, flash-lite, one short call) names
    the question's tier and the budget breaker (breakers.py) picks the model for it - Pro for a complex
    question while the month is under 80% of budget, flash-lite for everything simple, and nothing
    dearer than flash once the month is past it. Off: the generator model serves everything, which is
    what the lane ran until Module 10. A classifier failure is never an outage: it falls back."""
    if settings.routing != "on":
        return settings.generator_model
    try:
        tier = classify(query, _gen_client).value
        return choose_model(tier, spend_pct(_fs(), settings.budget_usd, settings.spend_pct_override))
    except Exception as e:  # noqa: BLE001
        log.warning(json.dumps({"event": "routing_fallback", "error": type(e).__name__}))
        return settings.generator_model


_TENANT_SETTINGS: dict = {}


def tenant_settings(tenant_id: str) -> dict:
    """11.4's "pin one tenant": tenant_settings/{tenant} may name a model_backend and a generator_model, and the
    residency customer's answers come from the self-hosted route while everyone else's come from Gemini. Read once a
    minute per tenant; a missing document or a failed read is the global setting. A field edit, never a redeploy."""
    now = time.time()
    hit = _TENANT_SETTINGS.get(tenant_id)
    if hit and now - hit[0] < 60:
        return hit[1]
    try:
        snap = _fs().collection("tenant_settings").document(tenant_id).get()
        doc = (snap.to_dict() or {}) if snap.exists else {}
    except Exception:  # noqa: BLE001 - the pin is a convenience; the setting is the default
        doc = {}
    _TENANT_SETTINGS[tenant_id] = (now, doc)
    return doc


def choose_for(req) -> tuple[str, str]:
    """(backend, model) for this request: the tenant's pin first, else the routed tier (10.3), else the settings."""
    ts = tenant_settings(req.tenant_id)
    backend = ts.get("model_backend") or settings.model_backend
    if ts.get("generator_model"):
        return backend, ts["generator_model"]
    return backend, choose_model_for(req.query)


def _fingerprint(tenant_id: str) -> str:
    """The ledger's corpus fingerprint (12.5: ledger/{tenant}; the identity 10.2's context cache follows too), or ""
    on a lane that has never reindexed. The answer cache is keyed on it: a reindex makes every earlier answer a miss."""
    snap = _fs().collection("ledger").document(tenant_id).get()
    return ((snap.to_dict() or {}).get("fingerprint") or "") if snap.exists else ""


def _semantic_hit(req, qvec, fingerprint):
    """12.6's answer cache, when SEMANTIC_CACHE=on: the stored answer for a near-enough earlier question of this tenant
    under this corpus, as a RAGResponse that cost nothing and cites what the original cited - or None. A cache that
    fails is a miss and a warning, never an error: the answer is still one retrieval away."""
    if settings.semantic_cache != "on":
        return None
    try:
        hit = semantic_cache.lookup(_fs(), req.tenant_id, qvec, fingerprint,
                                    scope=semantic_cache.scope_of(req.filters, req.top_k, settings.prompt_version),
                                    question=req.query)   # the exact rung first: the same words never reach the vector search
    except Exception as e:  # noqa: BLE001
        log.warning(json.dumps({"event": "semantic_cache_failed", "tenant": req.tenant_id, "error": type(e).__name__}))
        return None
    if not hit:
        return None
    return RAGResponse(**hit["answer"], model=hit.get("model") or settings.generator_model, backend="cache",
                       cost_usd=0.0, tokens_in=0, tokens_out=0, latency_ms=0, cache_hit="semantic")


def _semantic_store(req, qvec, ans, fingerprint) -> None:
    """After /v1/query has an answer the caller is getting - answerable, cited, not blocked: a refusal is not worth a
    day and a blocked answer is not worth anything. Only the contract is stored (RAGAnswer: answer, citations,
    confidence, answerable), never the envelope. Failing to store is a warning, never a failed request."""
    if settings.semantic_cache != "on" or not (ans.answerable and ans.citations):
        return
    try:
        semantic_cache.store(_fs(), req.tenant_id, req.query, qvec,
                             RAGAnswer.model_validate(ans.model_dump()).model_dump(), fingerprint, model=ans.model,
                             scope=semantic_cache.scope_of(req.filters, req.top_k, settings.prompt_version))
    except Exception as e:  # noqa: BLE001
        log.warning(json.dumps({"event": "semantic_cache_store_failed", "tenant": req.tenant_id, "error": type(e).__name__}))


def check_filters(filters: dict | None) -> None:
    """The request's filters, or a 400 (12 September 2026, R06). A key that is not a restrict namespace on the
    index and a field on the row filters nothing on one path and everything on another; tenant_id and `current`
    are the roster's and the ledger's, never the caller's - a body field is a header in disguise. A typo is a 400
    that names the allowed keys, not an empty pool that reads like an honest "nothing found"."""
    if not filters:
        return
    bad = sorted(set(filters) - set(FILTER_KEYS))
    if bad:
        raise HTTPException(400, f"unknown filter key(s) {', '.join(bad)}; allowed: {', '.join(FILTER_KEYS)}")
    for k, v in filters.items():
        if not isinstance(v, str) or not v:
            raise HTTPException(400, f"filter {k} must be a non-empty string")


EMPTY_POOL_ANSWER = ("The corpus holds nothing near this question: no passage of this tenant's current documents was "
                     "retrieved, so there is nothing to answer from and nothing to cite. Check that the document you "
                     "expect is ingested and current, or ask with the words it uses.")


def empty_pool_answer(model: str, stages: dict) -> RAGResponse:
    """The answer for an empty pool (12 September 2026, R06): retrieval found no candidate, so there is nothing for
    the reranker to order and nothing for the model to read - a model call would buy an invented answer or a refusal
    at full price. A refusal in the contract's own shape (answerable=False, no citations, confidence low), backend
    "none", zero tokens, zero cost; the rerank and generate clocks read 0 because they never ran. Never stored in the
    answer cache (_semantic_store refuses an unanswerable answer), and the row's answerable=False is what the
    unanswerable alert counts: a question the corpus cannot reach is its business."""
    stages["rerank_ms"] = stages["generate_ms"] = 0
    return RAGResponse(answer=EMPTY_POOL_ANSWER, citations=[], confidence="low", answerable=False, model=model,
                       backend="none", cost_usd=0.0, tokens_in=0, tokens_out=0, latency_ms=0, cache_hit="none")


def usage_row(req, user, ans_tokens_in, ans_tokens_out, cached, latency_ms,
              answerable, confidence, surface, modality="text", model=None, backend=None, cost_usd=None, guard="off",
              stages=None):
    """The ONE shape every observability consumer reads.

    tenant_daily.sql selects exactly these fields, so a column added there
    without a field added here is a column of NULLs that looks like it works.
    `model` is the model that ANSWERED - the routed tier or the tuned endpoint, when there is one -
    priced at its own rate (cost.py), so tenant_daily's cost column is what was billed.
    """
    model = model or settings.generator_model
    stages = stages or {}
    # The gateway prices what it served, fallbacks included (Module 11); otherwise cost.py's rate for the model.
    cost = cost_usd if cost_usd is not None else price(model, ans_tokens_in, ans_tokens_out, cached)["usd"]
    return {"event": surface, "tenant": req.tenant_id, "user": user["email"],
            "tokens_in": ans_tokens_in, "tokens_out": ans_tokens_out,
            "cached_tokens": cached, "cost_usd": round(cost, 6),
            "latency_ms": latency_ms, "answerable": answerable,
            # Where the time went: the three stages on their own clocks (stage() above) and the size of the pool
            # the reranker saw - the number TOP_K_RETRIEVE sets and evals/ablate.py decides. "p95 is 3 s" is a
            # page; "generate is 2.6 s of it" is a fix. A row from before these fields is NULL in the view, not 0.
            "retrieve_ms": stages.get("retrieve_ms", 0), "rerank_ms": stages.get("rerank_ms", 0),
            "generate_ms": stages.get("generate_ms", 0), "pool": stages.get("pool", 0),
            # 1 when the Ranking API did not answer and the pool stood in by retrieval score (retriever.rerank):
            # a degraded order, counted - the rerank_fallback log event beside it carries the error type.
            "rerank_fallback": stages.get("rerank_fallback", 0),
            "confidence": confidence,
            # An explicit 0/1 beside the boolean. Cloud Logging's
            # value_extractor pulls a NUMBER out of a log entry; it cannot
            # turn true/false into one, so the metric behind the
            # unanswerable-rate alert would have nothing to read.
            "unanswerable_flag": 0 if answerable else 1,
            "model_backend": backend or settings.model_backend,    # what ANSWERED: vertex, or the gateway (Module 11)
            "guard": guard,                                       # 12.6: off | pass | blocked_response - what Model Armor said, when asked
            "model": model,
            "prompt_version": settings.prompt_version,
            "retrieval_mode": settings.retrieval_mode,
            "modality": modality, "surface": surface,
            # 8.7's question - which harness costs what - answered from the warehouse: the
            # chat service labels its brain on every call, the UI's own stream is "ui".
            "brain": getattr(req, "brain", None) or "ui"}


@app.get("/health")
def health(): return {"status": "ok"}

@app.get("/version")
def version():
    # What is actually serving. When an answer changes and no one deployed,
    # this is the first thing to check - and it is the same triple that goes
    # on every log line and every span.
    return {"model_backend": settings.model_backend,
            "generator_model": settings.generator_model,
            "prompt": f"{settings.prompt_id}@{settings.prompt_version}",
            "retrieval_mode": settings.retrieval_mode,
            # 12 September 2026: the embedding the query vector comes from - the same pair the worker stamps on
            # every row - and whether the ledger's pre-filter is on. A reindex that "changed nothing" and a
            # retrieval that "got worse" both start here.
            "embedding": f"{settings.embed_model}@{settings.embedding_version}",
            "retrieval_current_only": settings.retrieval_current_only,
            "semantic_cache": settings.semantic_cache,   # 12.6's answer cache: off | on (smoke.py asks twice when on)
            "git_sha": os.environ.get("GIT_SHA", "unknown")}

@app.get("/v1/sources")
def sources(tenant_id: str, user=Depends(verify_iap)):
    """The versions view (12 September 2026): the tenant's ledger - every source's current version, its object
    generation, what the last reindex cost (chunks reused by hash, embedded, retired), the date it declares, when
    it landed - and the corpus fingerprint the cache is keyed to. Read-only, and only for a tenant the caller is
    on the roster of: one customer's ledger is not another's to read. The UI's Documents page renders it;
    `make sources TENANT=` prints the same rows from the shell (reconcile.py --report)."""
    enforce_membership(user["email"], tenant_id)
    fs = _fs()
    rows = []
    for s in fs.collection("sources").where("tenant_id", "==", tenant_id).stream():
        d = s.to_dict() or {}
        at = d.get("indexed_at")
        rows.append({"name": d.get("name"), "status": d.get("status"), "doc_key": d.get("doc_key"),
                     "generation": d.get("generation"), "chunks": d.get("chunks"),
                     "reused": d.get("reused"), "embedded": d.get("embedded"), "retired": d.get("retired"),
                     "effective_from": d.get("effective_from"),
                     "embedding": f"{d.get('embedding_model') or '?'}@{d.get('embedding_version') or '?'}",
                     "indexed_at": at.isoformat() if hasattr(at, "isoformat") else None})
    rows.sort(key=lambda r: r["name"] or "")
    led = fs.collection("ledger").document(tenant_id).get()
    l = (led.to_dict() or {}) if led.exists else {}
    return {"tenant_id": tenant_id, "fingerprint": l.get("fingerprint"), "versions": l.get("versions"),
            "last_event": l.get("last_event"), "sources": rows}

@app.get("/ready")
def ready():
    # Lazy-init resource probes keep cold start fast; only warm when ready is probed
    from config import settings
    from retriever import _genai_client, _index_endpoint, _fs
    _ = _genai_client(); _ = _fs()
    if settings.retrieval_backend == "vector":      # the lean profile has no endpoint to warm
        _ = _index_endpoint()
    return {"status": "ready"}

def _guard():
    """12.6's guard, imported only when the revision says ARMOR=on: guard.py builds its Model Armor client and names
    its template at import, and a revision that never asked for the guard must neither pay for the client nor fail on
    a template it does not have."""
    import guard  # noqa: WPS433
    return guard


def screen_prompt(text: str, tenant_id: str) -> str:
    """Before retrieval, never after: an injection that reaches the retriever has already chosen which documents the
    model reads. A block is a 400 with the reason - a refusal dressed as an answer would hide the rate."""
    if settings.armor != "on":
        return "off"
    ok, reason = _guard().check_prompt(text)
    if not ok:
        log.info(json.dumps({"event": "guard", "tenant": tenant_id, "verdict": "blocked_prompt", "reason": reason}))
        raise HTTPException(400, reason)
    return "pass"


def screen_response(text: str, guard: str) -> tuple[str, str]:
    """On the buffered final answer - a token stream cannot be screened, so /v1/stream holds its tokens when the guard
    is on. Returns the row's verdict and, when blocked, the reason the caller raises AFTER the row is logged."""
    if settings.armor != "on":
        return guard, ""
    ok, reason = _guard().check_response(text)
    return ("pass", "") if ok else ("blocked_response", reason)


@app.post("/v1/query", response_model=RAGResponse)
def query(req: QueryRequest, user=Depends(verify_iap)):
    enforce_membership(user["email"], req.tenant_id)
    check_filters(req.filters)                            # a 400 before any work: an unknown key is a typo, not an empty pool
    t0 = time.time()
    backend, model = choose_for(req)
    guard = screen_prompt(req.query, req.tenant_id)       # 12.6: before retrieval, or not at all (ARMOR=off)
    stages: dict = {}
    fingerprint = _fingerprint(req.tenant_id) if settings.semantic_cache == "on" else ""
    with stage(stages, "retrieve"):
        qvec = embed_query(req.query)                     # once: the answer cache and the retrieval share it
        hit = _semantic_hit(req, qvec, fingerprint)
        chunks = [] if hit else retrieve(req.query, req.tenant_id, req.top_k, req.filters, vec=qvec)
    stages["pool"] = len(chunks)                          # what the reranker sees: TOP_K_RETRIEVE, as served
    if hit:
        ans = hit                                         # served from answer_cache: no reranker, no model
        stages["rerank_ms"] = stages["generate_ms"] = 0
    elif not chunks:
        ans = empty_pool_answer(model, stages)            # nothing retrieved: no reranker, no model, nothing stored
    else:
        with stage(stages, "rerank"):
            chunks = rerank(req.query, chunks, req.top_k, tenant_id=req.tenant_id)
        if rerank_fell_back(chunks):
            stages["rerank_fallback"] = 1                 # the pool by retrieval score stood in for the Ranking API
        with stage(stages, "generate"):
            ans = generate(req.query, chunks, req.tenant_id, model=model, backend=backend)
    ans.latency_ms = int((time.time() - t0) * 1000)
    ans.stages = dict(stages)                             # the same clocks in the answer, for the caller
    guard, reason = screen_response(ans.answer, guard)   # 12.6: the buffered final answer, never a token
    if not (hit or reason):
        _semantic_store(req, qvec, ans, fingerprint)      # only what the caller is getting: answerable, cited, not blocked
    # ans.model is the model that ANSWERED: the routed tier, the default it fell back to on a 429 (F45), or the
    # gateway route (Module 11); ans.backend says which door it went through.
    row = usage_row(req, user, ans.tokens_in, ans.tokens_out, getattr(ans, "cached_tokens", 0),
                    ans.latency_ms, ans.answerable, ans.confidence, "query",
                    modality=modality_of(c.kind for c in ans.citations), model=ans.model or model,
                    backend=ans.backend, cost_usd=ans.cost_usd, guard=guard, stages=stages)
    log.info(json.dumps(row))
    _record(row["cost_usd"])
    if reason:
        raise HTTPException(502, reason)                  # the row above says blocked_response; the caller gets the reason
    return ans


def _record(usd: float) -> None:
    """The month's counter (budget.py). Never in the answer's way: a counter that fails fails quietly."""
    try:
        record(_fs(), usd)
    except Exception as e:  # noqa: BLE001
        log.warning(json.dumps({"event": "budget_record_failed", "error": type(e).__name__}))

@app.post("/v1/stream")
def stream(req: QueryRequest, user=Depends(verify_iap)):
    enforce_membership(user["email"], req.tenant_id)
    check_filters(req.filters)                            # a 400 before the stream starts, like the guard's
    guard = screen_prompt(req.query, req.tenant_id)       # 12.6: before the stream starts, so a block is a 400, not a broken stream
    def sse():
        t0 = time.time()
        backend, model = choose_for(req)
        # The same three clocks as /v1/query, without the spans: a generator suspended between tokens is
        # no place to hold a span's context, and the trace already carries the request's own span.
        stages: dict = {}
        fingerprint = _fingerprint(req.tenant_id) if settings.semantic_cache == "on" else ""
        tick = time.perf_counter()
        qvec = embed_query(req.query)
        hit = _semantic_hit(req, qvec, fingerprint)      # the stream reads the answer cache; only /v1/query fills it
        chunks = [] if hit else retrieve(req.query, req.tenant_id, req.top_k, req.filters, vec=qvec)
        stages["retrieve_ms"], stages["pool"] = _ms(tick), len(chunks)
        tick = time.perf_counter()
        if chunks:                                   # a hit brought none; an empty pool has nothing to rank
            chunks = rerank(req.query, chunks, req.top_k, tenant_id=req.tenant_id)
            if rerank_fell_back(chunks):
                stages["rerank_fallback"] = 1
        stages["rerank_ms"] = _ms(tick)
        # The empty pool (12 September 2026): nothing retrieved is nothing to read and nothing to cite - one
        # token that says so, then done, with no model call; the row's answerable=False feeds the alert.
        empty = None if (hit or chunks) else empty_pool_answer(model, stages)
        kinds = [c.kind for c in hit.citations] if hit else []      # what the answer was made from, for the row
        for i, c in enumerate(hit.citations if hit else [], 1):
            # A hit's citations are the contract's, resolved when the answer was first given: the same event shape.
            yield f"event: citation\ndata: {json.dumps({'n': i, 'chunk_id': c.chunk_id, 'source': c.source_uri, 'page': c.page, 'quote': c.quote[:240], 'kind': c.kind, 'media_url': c.media_url, 'start': c.start, 'end': c.end})}\n\n"
        usage = {"tokens_in": 0, "tokens_out": 0}
        held = []                                    # 12.6: with the guard on, the answer is screened whole, then sent
        tick = time.perf_counter()
        if hit:                                      # the stored answer as one token: the UI cannot tell, the row can
            usage = {"tokens_in": 0, "tokens_out": 0, "cached_tokens": 0, "cost_usd": 0.0, "model": hit.model, "backend": "cache"}
        elif empty:                                  # the refusal as one token: no model, no cost, backend "none"
            usage = {"tokens_in": 0, "tokens_out": 0, "cached_tokens": 0, "cost_usd": 0.0, "model": empty.model, "backend": "none"}
        for kind, payload in ([("token", hit.answer)] if hit else [("token", empty.answer)] if empty else
                              generate_stream(req.query, chunks, req.tenant_id, model=model, backend=backend)):
            if kind == "packed":
                # Citations first, and from the PACKED set (12 September 2026, R06): they are known before a
                # single token exists, so the UI renders the sources while the answer is written - but the
                # budget may drop a chunk between the reranker and the prompt, and a passage the model never
                # read is not a source. generate_stream yields this list before its first token.
                kinds = [c.get("kind", "text") for c in payload]
                for i, c in enumerate(payload, 1):
                    # The same fields a Citation carries, so the UI renders a figure or a video
                    # segment from the stream exactly as it would from /v1/query (gap G7).
                    yield f"event: citation\ndata: {json.dumps({'n': i, 'chunk_id': c.get('id'), 'source': c['source_uri'], 'page': c.get('page_start'), 'quote': c['text'][:240], 'kind': c.get('kind', 'text'), 'media_url': c.get('media_url'), 'start': c.get('start'), 'end': c.get('end'), 'effective_from': c.get('effective_from')})}\n\n"
            elif kind == "token":
                if settings.armor == "on":
                    held.append(payload)
                else:
                    yield f"event: token\ndata: {json.dumps({'t': payload})}\n\n"
            else:
                usage = payload
        stages["generate_ms"] = 0 if empty else _ms(tick)   # first token to last: the model's whole turn, as the client felt it
        verdict, reason = screen_response("".join(held), guard) if held else (guard, "")
        if reason:
            yield f"event: error\ndata: {json.dumps({'error': reason})}\n\n"
        else:
            for t in held:
                yield f"event: token\ndata: {json.dumps({'t': t})}\n\n"
        model = usage.get("model") or model          # the model that answered (F45: a tier can fall back)
        backend = usage.get("backend") or backend    # and the door it went through (Module 11)
        done = {**usage, "latency_ms": int((time.time() - t0) * 1000), "stages": stages,
                "cache_hit": "semantic" if hit else "none",
                "model": model, "backend": backend,
                "prompt": f"{settings.prompt_id}@{settings.prompt_version}"}
        # The row's verdict: a refusal for the empty pool; otherwise the stream still says answerable (6.3 of the
        # plan gives the stream a real verdict) - the empty pool no longer hides in that.
        answerable, confidence = (False, "low") if empty else (True, hit.confidence if hit else "medium")
        row = usage_row(req, user, usage.get("tokens_in", 0), usage.get("tokens_out", 0),
                        usage.get("cached_tokens", 0), done["latency_ms"],
                        answerable, confidence, "stream",
                        modality=modality_of(kinds), model=model,
                        backend=backend, cost_usd=usage.get("cost_usd"), guard=verdict, stages=stages)
        log.info(json.dumps(row))
        _record(row["cost_usd"])
        yield f"event: done\ndata: {json.dumps(done)}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream")
