"""DocuMind agent tools - ONE implementation, consumed by every brain.

Module 8 ships three agent runtimes (ADK, LangGraph, and the direct RAG path) and Modules 6 and 7
ship two more surfaces (a LangChain loop and an MCP server). All of them answer questions about the
same documents. If each owns its own retrieval, they drift - and this course has already watched
that happen four times to a single cost function, which arrived in Module 6 with three different
shapes and in Module 7 and 8 with two more.

So retrieval lives here, once. A brain may decide WHEN to retrieve and WHAT to do with the result.
It does not get to decide what retrieval means.

The rule: no second implementation of `retrieve` should exist in the repo. Grep before a release
(`python tools/check_one_retrieval.py deploy/` does it properly).

TWO LANES, ONE FUNCTION (gap G3, 2026-09-05). `DOCUMIND_PROFILE=gcp` reaches rag-api over the
network; `DOCUMIND_PROFILE=local` reads the Chroma directory `shared/profile.py` opens, with no
cloud credential. The private helper below is not a second implementation - it is what the
one implementation does when the switch is set, and the contract out is identical.

TWO CREDENTIALS, ONE REQUEST (gap G4, 2026-09-05). Until today this file minted the ID token
correctly (lesson 7.3) and then sent two headers nothing verified - x-user-email and
x-tenant-id - under a comment claiming rag-api read them. A header anyone can set is a claim,
not a credential. The assertion of the PERSON the call is for is forwarded instead, when
there is one; otherwise the token's own identity is the caller (shared/iap.py, both legs).

Verified 2026-09-04 against deploy/services/rag-api/schemas.py.
"""
from __future__ import annotations

import logging
import math
import os
import re
import time
from typing import Any

import requests

logger = logging.getLogger("documind.agents.tools")

RAG_API_URL = os.environ.get("RAG_API_URL", "http://rag-api:8080")
RAG_TIMEOUT_S = float(os.environ.get("RAG_TIMEOUT_S", "20"))
PROFILE = os.environ.get("DOCUMIND_PROFILE", "gcp")      # the same switch shared/profile.py reads
ASSERTION_HEADER = "x-goog-iap-jwt-assertion"

USD_INR = 85  # course-wide conversion rate
RATES = {"standard": 0.05, "priority": 0.12, "bulk": 0.03}

CITATION_KEYS = ("chunk_id", "source_uri", "page", "quote", "score",
                 # 9.6: optional and additive, so a text citation written before Module 9
                 # comes through byte-identical.
                 "kind", "media_url", "start", "end")


def _id_token(audience: str) -> str:
    """Google-signed ID token for a Cloud Run service.

    Minted per call. These last an hour, and a process that captures one at import works all
    afternoon and starts returning 401 overnight - the failure lesson 7.3 exists to teach.
    """
    import google.auth.transport.requests
    import google.oauth2.id_token

    impersonate = os.environ.get("DOCUMIND_IMPERSONATE_SA")
    if impersonate:
        # A notebook or a shell has no metadata server: mint AS a roster member instead - the
        # same thing `gcloud auth print-identity-token --impersonate-service-account` does for
        # smoke.py, with the audience and the email both set (lesson 7.1, the local server).
        import google.auth
        from google.auth import impersonated_credentials
        source, _ = google.auth.default()
        target = impersonated_credentials.Credentials(
            source_credentials=source, target_principal=impersonate,
            target_scopes=["https://www.googleapis.com/auth/cloud-platform"])
        idc = impersonated_credentials.IDTokenCredentials(target, target_audience=audience, include_email=True)
        idc.refresh(google.auth.transport.requests.Request())
        return idc.token
    return google.oauth2.id_token.fetch_id_token(
        google.auth.transport.requests.Request(), audience
    )


def retrieve(query: str, tenant_id: str, top_k: int = 5,
             doc_type: str | None = None, assertion: str | None = None,
             brain: str | None = None) -> dict[str, Any]:
    """Retrieve grounded passages for a question from DocuMind's corpus.

    THE single retrieval entry point. Every brain calls this one.

    Args:
        query: The question, in natural language.
        tenant_id: Whose corpus to search. Never inferred from the question.
        top_k: How many passages to return (1-20).
        doc_type: Optional filter. The values are defined by the CORPUS, not by
            this function - the teaching corpus in Modules 6-8 holds policy,
            contract, invoice, form and research_paper; the eval corpus under
            evals/ adds report, and image and video for the Doc AI paths. Pass
            one the tenant's corpus actually contains: an unknown value filters
            everything out and returns answerable=False, which reads exactly
            like a corpus that cannot answer the question.
        assertion: The IAP assertion of the PERSON this call is for, when there is
            one - the chat service reads it off its own request and passes it
            through, and rag-api verifies it against the surfaces it accepts.
            Leave it None from a notebook or a batch job: the ID token alone is
            then the identity, and the account that minted it must be on the
            tenant's roster.
        brain: Which harness is asking (langchain | langgraph | adk | direct | mcp), so
            rag-api's usage row records it and 8.7's cost comparison can be run from
            the warehouse rather than a notebook. Purely a label.

    Returns:
        {"citations": [{chunk_id, source_uri, page, quote, score}], "answerable": bool,
         "confidence": "high"|"medium"|"low"} - plus "answer", rag-api's own grounded
        answer, when the gcp lane produced one (the direct brain uses it; the agents ignore it)
        or {"error": ...} - returned as DATA so the model can explain the failure rather than
        the turn dying on an exception.
    """
    if PROFILE == "local":
        return _retrieve_local(query, tenant_id, top_k, doc_type)

    payload: dict[str, Any] = {
        "query": query,
        "tenant_id": tenant_id,
        "user_id": "agent",
        "top_k": max(1, min(top_k, 20)),
        "stream": False,
    }
    if doc_type:
        payload["filters"] = {"doc_type": doc_type}
    if brain:
        payload["brain"] = brain

    # Two credentials on one request (lesson 12.8). The Bearer ID token is IAM: Cloud Run
    # checks roles/run.invoker on it before rag-api's code runs, and it names the CALLER.
    # The forwarded assertion, when there is one, names the PERSON; rag-api checks it first
    # and falls back to the token's own identity. Nothing else is sent, because nothing
    # else is verified.
    headers = {"Authorization": f"Bearer {_id_token(RAG_API_URL)}"}
    if assertion:
        headers[ASSERTION_HEADER] = assertion

    started = time.monotonic()
    try:
        resp = requests.post(f"{RAG_API_URL}/v1/query", json=payload, headers=headers,
                             timeout=RAG_TIMEOUT_S)
        resp.raise_for_status()
    except requests.RequestException as exc:
        logger.warning("retrieve failed: %s", exc)
        return {"error": "document retrieval is unavailable",
                "citations": [], "answerable": False, "confidence": "low"}
    finally:
        logger.info("retrieve took %.2fs", time.monotonic() - started)

    answer = resp.json()
    out: dict[str, Any] = {
        "citations": [{k: c[k] for k in CITATION_KEYS if k in c}
                      for c in answer.get("citations", [])],
        "answerable": answer.get("answerable", False),
        "confidence": answer.get("confidence", "low"),
    }
    if answer.get("answer"):
        out["answer"] = answer["answer"]    # rag-api's grounded answer; the direct brain's whole job
    return out


_STOP = frozenset("""what which when where whom does have this that with from about many much will your been
than then them they their there these those after before each into also only over such some were being
between within would could should shall under upon while whose every other another
the and for are how can who you did was its our not any all one two per get has had but may did
who why yet off out own too via
say says said gets give gives given provide provides provided apply applies allow allows allowed
need needs require required requires mean means make makes made take takes taken""".split())
EVIDENCE_FLOOR = 0.5     # the retrieved set must carry at least half of the question's information
EVIDENCE_MODE = "set"    # "set": the top-k chunks together; "top": the best chunk alone
# The corpus's own acronyms, spelt out the way the documents spell them. A question says "DPDP";
# the Act never does, and a word the corpus never uses would otherwise count against the answer.
_ACRONYMS = {
    "dpdp": "digital personal data protection", "posh": "sexual harassment women workplace",
    "osh": "occupational safety health working conditions", "cgst": "central goods services tax",
    "gst": "goods services tax", "msa": "master services agreement", "sow": "statement work",
    "epf": "provident fund", "esi": "state insurance", "gstin": "goods services tax", "pan": "permanent account",
}


def _words(query: str) -> list[str]:
    """Content words: three letters or more (GST, PAN, USB, MSA are three-letter facts in this
    corpus), minus question words and function words, with the corpus's acronyms spelt out."""
    raw = re.findall(r"[a-z0-9]+", query.lower())
    expanded = []
    for w in raw:
        expanded += _ACRONYMS[w].split() if w in _ACRONYMS else [w]
    return sorted({w for w in expanded if len(w) >= 3 and w not in _STOP})


def _match(word: str, token: str) -> bool:
    """A query word matches a token that starts with it (notice/notices), one it starts with
    (agreements/agreement), or one sharing its first five letters (eligible/eligibility). A
    five-letter prefix is as much stemming as a Rs 0 lane needs."""
    return (token.startswith(word) or (len(token) >= 4 and word.startswith(token))
            or (word.isalpha() and len(word) >= 6 and len(token) >= 6 and token[:5] == word[:5]))


def _lexical_rank(query: str, rows: list[tuple[str, dict]]) -> tuple[list[tuple[float, list[str], str, dict]], dict[str, float]]:
    """BM25 over a tenant's chunks - the textbook form, no package - with 6.4's gate made
    honest for a 465-chunk corpus. Query words are content words (four letters or more, not a
    question word). A word present in more than half the tenant's chunks (acme, days, employee)
    is dropped as evidence, because a match on it says nothing. The question's INFORMATION is the
    idf of its remaining words - including the ones the corpus never uses, which is exactly how
    "sabbatical", or "gratuity" asked of a tenant that holds no Gratuity Act, stays
    unanswerable. Returns ([(bm25, matched words, text, metadata)] best first, {word: idf})."""
    words = _words(query)
    if not rows or not words:
        return [], {}
    toks = [re.findall(r"[a-z0-9]+", text.lower()) for text, _ in rows]
    n, avgdl = len(toks), sum(map(len, toks)) / max(1, len(toks))
    tf = [{w: sum(1 for t in doc if _match(w, t)) for w in words} for doc in toks]
    df = {w: sum(1 for d in tf if d[w]) for w in words}
    if n >= 8:                                                # a tiny set has no "ubiquitous"
        words = [w for w in words if df[w] / n <= 0.5]
    if not words:
        return [], {}
    idf = {w: math.log(1 + (n - df[w] + 0.5) / (df[w] + 0.5)) for w in words}
    k1, b = 1.5, 0.75
    ranked = []
    for (text, meta), doc, f in zip(rows, toks, tf):
        hit = [w for w in words if f[w]]
        if not hit:
            continue
        s = sum(idf[w] * f[w] * (k1 + 1) / (f[w] + k1 * (1 - b + b * len(doc) / avgdl)) for w in hit)
        ranked.append((s, hit, text, meta))
    ranked.sort(key=lambda r: (-r[0], -len(r[1]), len(r[2])))
    return ranked, idf


def _snippet(text: str, words: list[str], width: int = 500) -> str:
    """The sentences of a chunk that carry the question, in order, at most `width` characters.
    Citation.quote is capped at 500 and the chat model answers from the quotes it is handed; a
    2,000-character statute window keeps its answer in one or two sentences, and an invoice
    keeps its total on the last line, so the quote is extractive - the matching sentences and
    lines, joined - rather than the first 500 characters. Sentences holding more of the
    question's words come first (weighting them by idf instead was measured and lost a row:
    tools/check_local_lane.py, 2026-09-05)."""
    if len(text) <= width:
        return text
    # A sentence ends at . or ; - and a line break only starts a new segment when the next line
    # does not continue a sentence in lower case: pypdf breaks statute lines mid-sentence
    # ("a minimum bonus which shall / be 8.33 per cent"), while an invoice's or a list's lines
    # ("Total payable", "(iv) monthly basis") each stand on their own.
    segments = [re.sub(r"\s*\n\s*", " ", s).strip()
                for s in re.split(r"(?<=[.;])\s+|\n+(?=[^a-z\s])", text) if s.strip()]
    scored = []
    for pos, seg in enumerate(segments):
        toks = re.findall(r"[a-z0-9]+", seg.lower())
        found = {w for w in words if any(_match(w, t) for t in toks)}
        if found:
            scored.append((-len(found), pos, seg[:width]))
    keep, used = [], 0
    for _, pos, piece in sorted(scored):          # the densest sentences first, then earlier ones
        if used + len(piece) + 1 > width:
            continue
        keep.append((pos, piece))
        used += len(piece) + 1
    return " ".join(p for _, p in sorted(keep)) if keep else text[:width]


def _retrieve_local(query: str, tenant_id: str, top_k: int,
                    doc_type: str | None) -> dict[str, Any]:
    """The local lane of the one retrieve: Chroma on disk, the same tenant filter, the same
    contract out. The store is read with get() - the tenant (and doc_type) predicate, every
    matching chunk, no embedding involved - and ranked lexically (BM25, above), because the
    default embedding is deterministic-but-fake (see shared/profile.build_store): a nearest-
    neighbour search over it would hand the gate 20 arbitrary chunks out of 465 and miss the
    clause that answers the question. A question the corpus cannot answer returns NOTHING and
    answerable=False, exactly like production."""
    from shared.profile import build_store

    try:
        store = build_store()
        coll = getattr(store, "_collection", None)
        if coll is not None and coll.count() == 0:
            return {"error": "local corpus is empty - run: python -m shared.local_corpus",
                    "citations": [], "answerable": False, "confidence": "low"}
        where: dict[str, Any] = {"tenant_id": tenant_id}
        if doc_type:
            where = {"$and": [{"tenant_id": tenant_id}, {"doc_type": doc_type}]}
        got = store.get(where=where, include=["documents", "metadatas"])
        rows = list(zip(got.get("documents") or [], got.get("metadatas") or []))
    except Exception as exc:                     # noqa: BLE001 - as data, like the gcp lane
        logger.warning("local retrieve failed: %s", exc)
        return {"error": "document retrieval is unavailable",
                "citations": [], "answerable": False, "confidence": "low"}

    ranked, idf = _lexical_rank(query, rows)
    ranked = ranked[:max(1, min(top_k, 20))]
    # The gate: the retrieved SET - a join question's answer sits in two chunks - must carry at
    # least EVIDENCE_FLOOR of the question's information, else this is a question the corpus
    # does not answer and the honest result is nothing. EVIDENCE_MODE "top" gates on the best
    # chunk alone (stricter: fewer false answers, joins suffer); "set" on the union.
    total = sum(idf.values())
    covered = {w for r in (ranked if EVIDENCE_MODE == "set" else ranked[:1]) for w in r[1]}
    share = sum(idf[w] for w in covered) / total if total else 0.0
    if share < EVIDENCE_FLOOR:
        ranked = []
    top = ranked[0][0] if ranked else 0.0
    return {
        "citations": [{"chunk_id": m.get("chunk_id", ""),
                       "source_uri": m.get("source_uri", ""),
                       "page": m.get("page"),
                       "quote": _snippet(text, hit),
                       "score": round(s / top, 3) if top else 0.0} for s, hit, text, m in ranked],
        "answerable": bool(ranked),
        "confidence": "high" if ranked and share >= 0.75 else "medium" if ranked else "low",
    }


def calculate_processing_cost(total_pages: int, num_documents: int = 1,
                              processing_type: str = "standard") -> dict[str, Any]:
    """Estimate document processing cost in USD and INR.

    Args:
        total_pages: Total page count across all documents.
        num_documents: How many documents those pages are spread across.
        processing_type: Service tier - standard, priority, or bulk.
    """
    if total_pages <= 0:
        raise ValueError("total_pages must be positive")
    if processing_type not in RATES:
        raise ValueError(f"unknown tier {processing_type!r}; expected one of {sorted(RATES)}")
    cost = total_pages * RATES[processing_type]
    return {"num_documents": num_documents, "total_pages": total_pages,
            "processing_type": processing_type, "rate_per_page": RATES[processing_type],
            "cost_usd": round(cost, 2), "cost_inr": round(cost * USD_INR, 2)}


TOOLS = [retrieve, calculate_processing_cost]
