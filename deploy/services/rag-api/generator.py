import json
import logging
import re
import time
from types import SimpleNamespace

import httpx
from fastapi import HTTPException
from pydantic import ValidationError

from google import genai
from google.genai import errors, types
from schemas import DraftCitation, ModelDraft, RAGResponse, resolve
from config import settings
from context_budget import pack_chunks, estimate_tokens, TokenBudget
from cache_manager import TenantCacheManager
from functools import lru_cache


@lru_cache(maxsize=1)
def _caches() -> TenantCacheManager:
    return TenantCacheManager(settings.project_id, settings.region)

log = logging.getLogger("documind-api")   # same logger main.py writes usage rows on
_client = genai.Client(enterprise=True, project=settings.project_id, location="global")  # generation runs on the global endpoint (Gemini 3.x)


@lru_cache(maxsize=8)
def _client_at(location: str) -> genai.Client:
    return _client if location == "global" else genai.Client(enterprise=True, project=settings.project_id, location=location)


def _regional_client() -> genai.Client:
    return _client_at(settings.region)


def _endpoint_location(model: str) -> str:
    """Where a tuned endpoint is served from: GENERATOR_LOCATION when set, else the path's own
    /locations/<x>/ segment, else the service's region. The first live tuning job (10 September) put its
    endpoint in the `us` multi-region while the service assumed us-central1 (F41): the path knows, the
    service does not, and the setting overrides both without a code change."""
    if settings.generator_location:
        return settings.generator_location
    m = re.search(r"/locations/([^/]+)/", model)
    return m.group(1) if m else settings.region


def _client_for(model: str) -> genai.Client:
    """A model NAME is served on the global endpoint; a TUNED model is an endpoint path, served from the
    location the path names (10.1: tuning is regional; the endpoint may land in a multi-region) and the
    global client answers 404 for it. The value of GENERATOR_MODEL decides - so a tuned model behind the
    same retrieve() is a redeploy with one variable, and every surface above the API (MCP, the agents,
    the UI) changes nothing."""
    return _client_at(_endpoint_location(model)) if model.startswith("projects/") else _client


# ----------------------------------------------------------------------------- the gateway backend (Module 11)
# THE BACKEND IS A SETTING. MODEL_BACKEND=vertex is what the lane runs: google.genai, the model above. MODEL_BACKEND=gateway
# sends the SAME prompt - SYSTEM, the packed context, the question - to the LiteLLM gateway (11.3) as an OpenAI-compatible
# chat completion with a JSON response format, and reads the SAME ModelDraft back through the same resolve(). With the
# gateway, GENERATOR_MODEL names a ROUTE (documind-general, documind-slm, documind-inference, documind-gke), so 11.4's
# self-hosted model answers behind the same retrieve() as Gemini, and every surface above the API changes nothing. The
# gateway is a Cloud Run service behind IAM like every other: this service's own account mints the ID token.
GATEWAY_JSON_RULE = ('\nReply with JSON only, exactly this shape: {"answer": str, "citations": [{"source": int, "quote": str}], '
                     '"confidence": "high" | "medium" | "low", "answerable": bool}.')
GATEWAY_ROUTES = ("documind-general", "documind-reasoning", "documind-slm", "documind-inference", "documind-gke")
_gateway_token = {"token": None, "exp": 0.0}


def _gateway_url() -> str:
    if not settings.litellm_url:
        raise HTTPException(502, "MODEL_BACKEND=gateway but LITELLM_URL is not set")
    return settings.litellm_url.rstrip("/")


def _gateway_headers() -> dict:
    """An ID token for the gateway's audience, minted by this service's own account through the metadata server (the
    same leg documind_tools uses on Cloud Run), cached and refreshed five minutes early."""
    if not _gateway_token["token"] or time.time() > _gateway_token["exp"] - 300:
        import google.auth.transport.requests
        import google.oauth2.id_token
        _gateway_token["token"] = google.oauth2.id_token.fetch_id_token(google.auth.transport.requests.Request(), _gateway_url())
        _gateway_token["exp"] = time.time() + 3600
    return {"Authorization": f"Bearer {_gateway_token['token']}", "Content-Type": "application/json"}


def _gateway_route(model: str) -> str:
    """A Gemini name or a tuned endpoint under the gateway backend means the gateway's Gemini route; a route name is
    passed through. The tuned Gemini endpoint (10.1) is served directly by the vertex backend, never through the gateway."""
    if model.startswith("gemini-3.1-pro"):
        return "documind-reasoning"
    if model.startswith("gemini-") or model.startswith("projects/"):
        return "documind-general"
    return model


def _gateway_messages(prompt: str) -> list[dict]:
    """The prompt _call() sends, split into the OpenAI shape: SYSTEM (plus the JSON rule) as the system turn, the
    context and the question as the user turn. Text only: a self-hosted model reads the caption, never the pixels."""
    user = prompt[len(SYSTEM):].lstrip() if prompt.startswith(SYSTEM) else prompt
    return [{"role": "system", "content": SYSTEM + GATEWAY_JSON_RULE}, {"role": "user", "content": user}]


class _GatewayReply:
    """The attributes _draft() and _finish_reason() read, over an OpenAI-compatible reply from the gateway."""
    parsed = None
    prompt_feedback = None

    def __init__(self, j: dict, cost_usd: float | None, model: str):
        choice = (j.get("choices") or [{}])[0]
        self.text = (choice.get("message") or {}).get("content") or ""
        fr = (choice.get("finish_reason") or "stop")
        self.candidates = [SimpleNamespace(finish_reason="MAX_TOKENS" if fr == "length" else fr.upper())]
        u = j.get("usage") or {}
        self.usage_metadata = SimpleNamespace(prompt_token_count=u.get("prompt_tokens") or 0,
                                              candidates_token_count=u.get("completion_tokens") or 0,
                                              thoughts_token_count=None, cached_content_token_count=0)
        self.model = j.get("model") or model
        self.cost_usd = cost_usd


def _gateway_call(prompt: str, packed: list[dict], tenant_id: str | None, max_tokens: int, model: str) -> _GatewayReply:
    body = {"model": model, "messages": _gateway_messages(prompt), "response_format": {"type": "json_object"},
            "max_tokens": max_tokens, "metadata": {"tenant": tenant_id or ""}}
    try:
        r = httpx.post(f"{_gateway_url()}/v1/chat/completions", json=body, headers=_gateway_headers(),
                       timeout=settings.gateway_timeout_s)
    except httpx.HTTPError as e:
        raise HTTPException(502, f"gateway unreachable: {type(e).__name__}") from e
    if r.status_code != 200:
        raise HTTPException(502, f"gateway {r.status_code}: {r.text[:200]}")
    cost = r.headers.get("x-litellm-response-cost")           # the gateway prices the route it served, fallbacks included
    return _GatewayReply(r.json(), float(cost) if cost else None, model)


def _gateway_stream(prompt: str, packed: list[dict], tenant_id: str | None, model: str):
    """The gateway's SSE, re-yielded as ("token", text) then ("usage", dict) - the same two kinds /v1/stream emits."""
    body = {"model": model, "messages": _gateway_messages(prompt), "stream": True, "stream_options": {"include_usage": True},
            "max_tokens": settings.max_answer_tokens, "metadata": {"tenant": tenant_id or ""}}
    usage = {"tokens_in": 0, "tokens_out": 0, "cached_tokens": 0}
    cost = None
    with httpx.stream("POST", f"{_gateway_url()}/v1/chat/completions", json=body, headers=_gateway_headers(),
                      timeout=settings.gateway_timeout_s) as r:
        if r.status_code != 200:
            raise HTTPException(502, f"gateway {r.status_code}")
        for line in r.iter_lines():
            if not line.startswith("data:"):
                continue
            payload = line[5:].strip()
            if payload == "[DONE]":
                break
            j = json.loads(payload)
            for ch in j.get("choices") or []:
                t = (ch.get("delta") or {}).get("content")
                if t:
                    yield "token", t
            if j.get("usage"):
                usage = {"tokens_in": j["usage"].get("prompt_tokens") or 0,
                         "tokens_out": j["usage"].get("completion_tokens") or 0, "cached_tokens": 0}
        cost = r.headers.get("x-litellm-response-cost")
    yield "usage", {**usage, "model": model, "backend": "gateway", "cost_usd": float(cost) if cost else None}


SYSTEM = """You are DocuMind, a retrieval-grounded assistant.
Rules:
1. Answer ONLY from the numbered context below. Never invent sources.
2. Cite using [N] where N is the chunk number. Multiple chunks: [1,2].
3. If the context does not contain the answer, set answerable=false and say so.
4. Keep answers under 300 words unless asked for more.
5. A quote is the clause that answers - at most twenty-five words, never a whole section.
"""

# The ledger's second half (12.5, 11 September 2026): a document may declare when it applies, and two sources
# in one context may then disagree by date. The rule is added ONLY when a packed chunk carries a date - SYSTEM
# itself stays byte-identical to the tuning dataset's (evals/make_trainset.py), so the tuned model is served
# behind the prompt it was trained behind.
DATED_RULE = ("\n6. Some sources carry an effective date. Where sources disagree, follow the one with the latest"
              " effective date, cite it, and say which source you followed and from when it applies.")


def _dated_rule(packed: list[dict]) -> str:
    return DATED_RULE if any(c.get("effective_from") for c in packed) else ""


def _budget(query: str, count_fn=estimate_tokens) -> TokenBudget:
    """The request's budget lines (4.5's TokenBudget, imported by nothing until 12 September 2026): the fixed parts
    of the prompt are counted - SYSTEM, the dated rule and the question with its scaffolding - and the chunks get
    what is LEFT of max_context_tokens. Until then the whole of max_context_tokens went to the chunks and the fixed
    parts rode on top, so the prompt exceeded the configured total by their size on every full request (R06). The
    dated rule is reserved whether or not a packed chunk turns out to carry a date: that is known only after
    packing, and holding its room is the safe side. count_fn is estimate_tokens (len // 4), the counter pack_chunks
    uses per block; a real count - client.models.count_tokens - can be injected here and there alike."""
    fixed = f"{SYSTEM}{DATED_RULE}\n\nContext:\n\n\nQuestion: {query}"      # the prompt below, with nothing packed
    return TokenBudget.fit(settings.max_context_tokens, fixed, count_fn, answer=settings.max_answer_tokens)


def _pack(query: str, chunks: list[dict]) -> tuple[str, list[dict]]:
    """The context and the packed set for the prompt: pack_chunks inside the chunk budget, the drops logged.
    THREE values from pack_chunks, not one: (context, packed, dropped) - 4.5's contract, and its own docstring
    says so. Assigning the tuple to `chunks` and handing it to build_context() raised AttributeError: 'str'
    object has no attribute 'get' on EVERY request, while /health and /ready both stayed green."""
    context, packed, dropped = pack_chunks(chunks, _budget(query).chunks, estimate_tokens)
    if dropped:
        # Not silent. A dropped chunk is a passage the model was never shown, and
        # "the answer got worse after we added documents" starts here.
        log.info(json.dumps({"event": "context_budget_drop",
                             "packed": len(packed), "dropped": len(dropped)}))
    return context, packed


def _usage(r) -> dict:
    """What one attempt billed (6.2, 12 September 2026): the prompt tokens - the cached ones among them, priced at
    the cache rate by cost.py - and the output, which on the 3.x family is the candidates AND the thinking:
    thoughts_token_count is billed as output whether or not a thought is shown, and until now it was never added,
    so every row under-counted what the month's counter (budget.py) then read."""
    u = getattr(r, "usage_metadata", None)
    n = lambda k: getattr(u, k, None) or 0  # noqa: E731 - None from the SDK means "not this call", not 0 tokens
    return {"tokens_in": n("prompt_token_count"),
            "tokens_out": n("candidates_token_count") + n("thoughts_token_count"),
            "cached_tokens": n("cached_content_token_count")}


def _add(a: dict, b: dict) -> dict:
    """Two attempts' usage as one bill: the tokens summed, the gateway's price summed when either attempt carried one."""
    out = {k: (a.get(k) or 0) + (b.get(k) or 0) for k in ("tokens_in", "tokens_out", "cached_tokens")}
    if a.get("cost_usd") is not None or b.get("cost_usd") is not None:
        out["cost_usd"] = (a.get("cost_usd") or 0.0) + (b.get("cost_usd") or 0.0)
    return out


# build_context() is GONE, deliberately. pack_chunks() already returns a formatted
# context, and keeping both meant two functions formatting the same sources two
# different ways - `[i] src page N` here and `[Source n] ...` in context_budget.py.
# One of them was always going to be the one nobody updated.
def _contents(prompt: str, packed: list[dict]) -> list:
    """9.6 cell 15, shipped: show the model the figure it is about to cite.

    A verbalised caption is enough to RETRIEVE a figure; it is not always enough to ANSWER
    from one - "which segment grew fastest" needs the chart, not the sentence about the chart.
    So the image Part is appended for every packed chunk that is a figure or a table and has a
    locator. Only those: each crop is ~258+ tokens, and most questions are answered by the
    caption alone. from_uri: the asset stays in GCS and never passes through this process.
    """
    contents: list = [prompt]
    for i, c in enumerate(packed, 1):
        if c.get("kind") in ("figure", "table") and c.get("media_url"):
            mime = "image/jpeg" if str(c["media_url"]).lower().endswith((".jpg", ".jpeg")) else "image/png"
            contents.append(types.Part.from_uri(file_uri=c["media_url"], mime_type=mime))
            contents.append(f"(the image above is [Source {i}])")
    return contents


def _cache_kwargs(tenant_id: str | None, model: str | None = None) -> dict:
    """4.5's cache_manager, wired. Empty dict when the tenant has no live cache - or when the cache
    was made for another model (10.2: a cache is the model's, not the tenant's alone)."""
    if not tenant_id:
        return {}
    try:
        return _caches().generate_config_kwargs(tenant_id, model)
    except Exception:
        # A cache miss must never fail the answer. Caching is an optimisation,
        # and an optimisation that can take the service down is a liability.
        return {}


def _call(prompt: str, packed: list[dict], tenant_id: str | None, max_tokens: int, model: str | None = None):
    """One generate_content call with the structured-answer config, on the client the model needs."""
    model = model or settings.generator_model
    return _client_for(model).models.generate_content(
        model=model,
        contents=_contents(prompt, packed),   # + the figures being cited (9.6, gap G7)
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            # ModelDraft, from shared/documind_schemas.py - the same class 3.2 teaches.
            # The model cites by [Source N] and QUOTES the words it relied on; resolve()
            # below turns that into Citations with ids, pages and scores it never saw.
            response_schema=ModelDraft,
            max_output_tokens=max_tokens,
            # NO temperature / top_p / top_k: gemini-3.6-flash ignores all
            # three, and passing them reads like a knob that does something.
            # thinking_level, not thinking_budget=0 - Gemini 3.x thinking
            # cannot be switched off, and budget=0 is a 2.5-era setting.
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
            **_cache_kwargs(tenant_id, model),
        ),
    )


def _exhausted_tier(e: BaseException, model: str) -> bool:
    """A 429 from a ROUTED tier (10.3) is a quota on that tier, not an outage: the default model answers
    instead and the row says so. The first routed eval (10 September) served two rows a 500 because the Pro
    tier's quota ran out (F45). A 429 from the default model itself, or from a tuned endpoint that IS the
    default, is still the caller's error to see."""
    return isinstance(e, errors.APIError) and getattr(e, "code", None) == 429 and model != settings.generator_model


def _call_or_fallback(prompt: str, packed: list[dict], tenant_id: str | None, max_tokens: int, model: str):
    """_call, and on an exhausted tier the same call on the default model. Returns (response, model that answered)."""
    try:
        return _call(prompt, packed, tenant_id, max_tokens, model), model
    except errors.APIError as e:
        if not _exhausted_tier(e, model):
            raise
        log.warning(json.dumps({"event": "tier_exhausted", "tenant": tenant_id, "model": model,
                                "fallback": settings.generator_model}))
        return _call(prompt, packed, tenant_id, max_tokens, settings.generator_model), settings.generator_model


def _quote_limit() -> int:
    """The contract's ceiling on a draft quote (shared/documind_schemas.py: 200 characters)."""
    for m in DraftCitation.model_fields["quote"].metadata:
        if getattr(m, "max_length", None):
            return int(m.max_length)
    return 200


def _problems(e: ValidationError) -> list[str]:
    return [".".join(str(x) for x in err["loc"]) + ": " + err["msg"] for err in e.errors()][:4]


def _draft(r) -> ModelDraft | None:
    """The model's structured answer, or None when there is nothing parseable to answer from.

    The SDK validates the JSON against ModelDraft and hands back None on ANY violation. The
    third live eval (7 Sept 2026) found the violation that matters: a quote longer than the
    contract's 200 characters. A statute provision is one long sentence, "exact words from
    that source" invites the model to copy it whole, and nine correct answers were thrown
    away for an excerpt that was merely long - resolve() would have cut it to 500 anyway,
    but never got the chance. So: the JSON is read, an over-long quote is trimmed to the
    contract, the draft is validated again, and the repair is logged with the field that
    failed. Anything else that fails validation is logged the same way and stays a failure.
    """
    if isinstance(r.parsed, ModelDraft):
        return r.parsed
    if r.parsed:
        return ModelDraft.model_validate(r.parsed)
    try:
        text = r.text
    except Exception:
        text = None
    if not text:
        return None
    try:
        obj = json.loads(text)
    except ValueError:
        return None
    try:
        return ModelDraft.model_validate(obj)
    except ValidationError as e:
        problems = _problems(e)
    limit = _quote_limit()
    for c in obj.get("citations") or []:
        if isinstance(c, dict) and isinstance(c.get("quote"), str) and len(c["quote"]) > limit:
            c["quote"] = c["quote"][: limit - 3].rstrip() + "..."
    try:
        draft = ModelDraft.model_validate(obj)
    except ValidationError as e:
        log.error(json.dumps({"event": "generation_invalid", "problems": _problems(e)}))
        return None
    log.warning(json.dumps({"event": "generation_repaired", "problems": problems}))
    return draft


def _finish_reason(r) -> str:
    """Why the model stopped: STOP, MAX_TOKENS, SAFETY, ... or why it never started."""
    cands = r.candidates or []
    if not cands:
        block = getattr(getattr(r, "prompt_feedback", None), "block_reason", None)
        return f"BLOCKED:{getattr(block, 'name', block)}" if block else "NO_CANDIDATES"
    fr = getattr(cands[0], "finish_reason", None)
    return getattr(fr, "name", str(fr)) if fr is not None else "UNKNOWN"



def generate(query: str, chunks: list[dict], tenant_id: str | None = None, model: str | None = None,
             backend: str | None = None) -> RAGResponse:
    """`model`: the routed tier (10.3) or the generator model; a tuned endpoint (10.1) is a value of either.
    `backend`: vertex (google.genai) or gateway (11.3's LiteLLM, where `model` names a route) - the setting, or 11.4's
    per-tenant pin."""
    model = model or settings.generator_model
    backend = backend or settings.model_backend
    if backend == "gateway":
        model = _gateway_route(model)
    # 4.5's context_budget, wired. Until now settings.max_context_tokens was
    # declared in config.py and read by nothing, so a long retrieval went to
    # the model whole and the budget was decorative. _pack() packs inside what is
    # left of it after the prompt's fixed parts (_budget, 12 September 2026).
    context, packed = _pack(query, chunks)
    prompt = f"{SYSTEM}{_dated_rule(packed)}\n\nContext:\n{context}\n\nQuestion: {query}"

    if backend == "gateway":
        r = _gateway_call(prompt, packed, tenant_id, settings.max_answer_tokens, model)
    else:
        r, model = _call_or_fallback(prompt, packed, tenant_id, settings.max_answer_tokens, model)
    billed = {**_usage(r), "cost_usd": getattr(r, "cost_usd", None)}   # every paid attempt counts: this one, and the retry
    draft = _draft(r)
    if draft is None and _finish_reason(r) == "MAX_TOKENS":
        # Cut off, not refused. A statute answer with its quotes - and, on the 3.x family,
        # the thinking drawn from the same budget before them - can outrun the first cap.
        # Once more with three times the room. The second live eval (7 Sept 2026) scored
        # fourteen of these as refusals: every one a real document with a real answer.
        log.warning(json.dumps({"event": "generation_truncated", "tenant": tenant_id,
                                "max_output_tokens": settings.max_answer_tokens,
                                "tokens_out": r.usage_metadata.candidates_token_count or 0,
                                "thoughts": getattr(r.usage_metadata, "thoughts_token_count", None)}))
        if backend == "gateway":
            r = _gateway_call(prompt, packed, tenant_id, settings.max_answer_tokens * 3, model)
        else:
            r, model = _call_or_fallback(prompt, packed, tenant_id, settings.max_answer_tokens * 3, model)
        # The first attempt was billed too. Rebinding `r` dropped it from the row until 12 September 2026 (6.2):
        # a truncated-then-retried answer cost the tenant two calls and was counted as one.
        billed = _add(billed, {**_usage(r), "cost_usd": getattr(r, "cost_usd", None)})
        draft = _draft(r)
    if draft is None:
        # NOT a refusal. This used to substitute answerable=False, confidence="low" and no
        # citations - the exact shape of the model declining - so a parse failure scored as
        # the lane saying "not in my documents" and nothing anywhere said otherwise. A 502
        # is what happened; the gate counts it as plumbing, which is where it belongs.
        reason = _finish_reason(r)
        log.error(json.dumps({"event": "generation_unparsed", "tenant": tenant_id,
                              "finish_reason": reason,
                              "tokens_out": r.usage_metadata.candidates_token_count or 0}))
        raise HTTPException(502, f"generation produced no parseable answer ({reason})")
    # Resolve against PACKED, not `chunks`. The model numbered what it SAW, and the budget
    # may have dropped something in between - indexing the pre-budget list shifted every
    # citation after a dropped chunk by one, silently. Gap G1 found it; resolve() owns it.
    ans = resolve(draft, packed)
    return RAGResponse(
        **ans.model_dump(),
        model=model,
        backend=backend,
        **billed,            # tokens_in, tokens_out (thinking included), cached_tokens, cost_usd - across every attempt
        latency_ms=0,
    )


def generate_stream(query: str, chunks: list[dict], tenant_id: str | None = None, model: str | None = None,
                    backend: str | None = None):
    """Yield ("packed", chunks) once, then ("token", text) as the model produces it, then ("usage", dict).

    REAL streaming, unlike the version this replaced, which called the blocking
    generate(), waited for the whole answer and then split it on whitespace.
    That looked identical in a browser and had exactly the same time to first
    token as /v1/query - the feature was absent and the demo could not show it.

    No response_schema here on purpose: you cannot usefully stream structured
    JSON. The citations do not need it - they are known BEFORE generation - but
    they are the PACKED set, not the reranked pool: the budget may drop a chunk
    between the reranker and the prompt, and a citation to a passage the model
    never read is not a citation. So the first event is the packed list
    (12 September 2026, R06) and /v1/stream cites from it before the first token.
    """
    context, packed = _pack(query, chunks)
    yield "packed", packed
    prompt = f"{SYSTEM}{_dated_rule(packed)}\n\nContext:\n{context}\n\nQuestion: {query}"

    model = model or settings.generator_model
    backend = backend or settings.model_backend
    if backend == "gateway":
        yield from _gateway_stream(prompt, packed, tenant_id, _gateway_route(model))
        return
    yield from _vertex_stream(prompt, packed, tenant_id, model)


def _vertex_stream(prompt: str, packed: list[dict], tenant_id: str | None, model: str):
    """The tokens and the usage from google.genai - and, on an exhausted routed tier before the first token,
    the default model's instead (F45). Below generate_stream so the packed event is yielded exactly once,
    whichever model ends up answering."""
    usage = {"tokens_in": 0, "tokens_out": 0}
    yielded = 0
    try:
        for part in _client_for(model).models.generate_content_stream(
            model=model,
            contents=_contents(prompt, packed),   # + the figures being cited (9.6, gap G7)
            config=types.GenerateContentConfig(
                max_output_tokens=settings.max_answer_tokens,
                thinking_config=types.ThinkingConfig(thinking_level="LOW"),
                **_cache_kwargs(tenant_id, model),
            ),
        ):
            if part.text:
                yielded += 1
                yield "token", part.text
            if part.usage_metadata:
                # The last chunk carries the totals; earlier ones may carry partials. Thinking counts as
                # output (6.2), and 10.2's caching only shows up in the bill if something records it.
                # This is that something.
                usage = _usage(part)
    except errors.APIError as e:
        # An exhausted routed tier before the first token: the default model streams instead (F45).
        if yielded or not _exhausted_tier(e, model):
            raise
        log.warning(json.dumps({"event": "tier_exhausted", "tenant": tenant_id, "model": model,
                                "fallback": settings.generator_model}))
        yield from _vertex_stream(prompt, packed, tenant_id, settings.generator_model)
        return
    # The model that answered, for the usage row: the tier, or the default it fell back to.
    yield "usage", {**usage, "model": model}
