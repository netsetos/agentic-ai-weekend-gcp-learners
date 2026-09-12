import time, json, logging, os, sys
from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from opentelemetry import trace
from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.exporter.cloud_trace import CloudTraceSpanExporter
from schemas import QueryRequest, RAGResponse
from retriever import retrieve, rerank, _fs
from generator import generate, generate_stream, _client as _gen_client
from config import settings
from auth import verify_iap, enforce_membership
from cost import price
from router import classify
from breakers import choose_model
from budget import record, spend_pct

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)  # bare JSON -> Cloud Run jsonPayload
log = logging.getLogger("documind-api")

trace.set_tracer_provider(TracerProvider())
trace.get_tracer_provider().add_span_processor(
    BatchSpanProcessor(CloudTraceSpanExporter(project_id=settings.project_id)))
tracer = trace.get_tracer(__name__)

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


def usage_row(req, user, ans_tokens_in, ans_tokens_out, cached, latency_ms,
              answerable, confidence, surface, modality="text", model=None, backend=None, cost_usd=None, guard="off"):
    """The ONE shape every observability consumer reads.

    tenant_daily.sql selects exactly these fields, so a column added there
    without a field added here is a column of NULLs that looks like it works.
    `model` is the model that ANSWERED - the routed tier or the tuned endpoint, when there is one -
    priced at its own rate (cost.py), so tenant_daily's cost column is what was billed.
    """
    model = model or settings.generator_model
    # The gateway prices what it served, fallbacks included (Module 11); otherwise cost.py's rate for the model.
    cost = cost_usd if cost_usd is not None else price(model, ans_tokens_in, ans_tokens_out, cached)["usd"]
    return {"event": surface, "tenant": req.tenant_id, "user": user["email"],
            "tokens_in": ans_tokens_in, "tokens_out": ans_tokens_out,
            "cached_tokens": cached, "cost_usd": round(cost, 6),
            "latency_ms": latency_ms, "answerable": answerable,
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
            "git_sha": os.environ.get("GIT_SHA", "unknown")}

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
    t0 = time.time()
    backend, model = choose_for(req)
    guard = screen_prompt(req.query, req.tenant_id)       # 12.6: before retrieval, or not at all (ARMOR=off)
    with tracer.start_as_current_span("retrieve"):
        chunks = retrieve(req.query, req.tenant_id, req.top_k, req.filters)
    with tracer.start_as_current_span("rerank"):
        chunks = rerank(req.query, chunks, req.top_k)
    with tracer.start_as_current_span("generate"):
        ans = generate(req.query, chunks, req.tenant_id, model=model, backend=backend)
    ans.latency_ms = int((time.time() - t0) * 1000)
    guard, reason = screen_response(ans.answer, guard)   # 12.6: the buffered final answer, never a token
    # ans.model is the model that ANSWERED: the routed tier, the default it fell back to on a 429 (F45), or the
    # gateway route (Module 11); ans.backend says which door it went through.
    row = usage_row(req, user, ans.tokens_in, ans.tokens_out, getattr(ans, "cached_tokens", 0),
                    ans.latency_ms, ans.answerable, ans.confidence, "query",
                    modality=modality_of(c.kind for c in ans.citations), model=ans.model or model,
                    backend=ans.backend, cost_usd=ans.cost_usd, guard=guard)
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
    guard = screen_prompt(req.query, req.tenant_id)       # 12.6: before the stream starts, so a block is a 400, not a broken stream
    def sse():
        t0 = time.time()
        backend, model = choose_for(req)
        chunks = retrieve(req.query, req.tenant_id, req.top_k, req.filters)
        chunks = rerank(req.query, chunks, req.top_k)
        # Citations first: they come from retrieval, so they are known before a
        # single token exists. The UI can render the sources while the answer
        # is still being written.
        for i, c in enumerate(chunks, 1):
            # The same fields a Citation carries, so the UI renders a figure or a video
            # segment from the stream exactly as it would from /v1/query (gap G7).
            yield f"event: citation\ndata: {json.dumps({'n': i, 'chunk_id': c.get('id'), 'source': c['source_uri'], 'page': c.get('page_start'), 'quote': c['text'][:240], 'kind': c.get('kind', 'text'), 'media_url': c.get('media_url'), 'start': c.get('start'), 'end': c.get('end'), 'effective_from': c.get('effective_from')})}\n\n"
        usage = {"tokens_in": 0, "tokens_out": 0}
        held = []                                    # 12.6: with the guard on, the answer is screened whole, then sent
        for kind, payload in generate_stream(req.query, chunks, req.tenant_id, model=model, backend=backend):
            if kind == "token":
                if settings.armor == "on":
                    held.append(payload)
                else:
                    yield f"event: token\ndata: {json.dumps({'t': payload})}\n\n"
            else:
                usage = payload
        verdict, reason = screen_response("".join(held), guard) if held else (guard, "")
        if reason:
            yield f"event: error\ndata: {json.dumps({'error': reason})}\n\n"
        else:
            for t in held:
                yield f"event: token\ndata: {json.dumps({'t': t})}\n\n"
        model = usage.get("model") or model          # the model that answered (F45: a tier can fall back)
        backend = usage.get("backend") or backend    # and the door it went through (Module 11)
        done = {**usage, "latency_ms": int((time.time() - t0) * 1000),
                "model": model, "backend": backend,
                "prompt": f"{settings.prompt_id}@{settings.prompt_version}"}
        row = usage_row(req, user, usage.get("tokens_in", 0), usage.get("tokens_out", 0),
                        usage.get("cached_tokens", 0), done["latency_ms"],
                        True, "medium", "stream",
                        modality=modality_of(c.get("kind", "text") for c in chunks), model=model,
                        backend=backend, cost_usd=usage.get("cost_usd"), guard=verdict)
        log.info(json.dumps(row))
        _record(row["cost_usd"])
        yield f"event: done\ndata: {json.dumps(done)}\n\n"
    return StreamingResponse(sse(), media_type="text/event-stream")
