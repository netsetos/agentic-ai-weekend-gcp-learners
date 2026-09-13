"""Answer the question you already answered - for this tenant, under this corpus, and not for long.

Shipped in April and imported by nothing until 12 September 2026; wired since then behind SEMANTIC_CACHE=on
(main.py, the RAG plan's W4). Before retrieval, the query's own embedding - the one retrieval reuses - looks up
the tenant's nearest earlier question. Near enough, answered under the corpus the tenant has NOW (the ledger's
fingerprint, the same identity 10.2's context cache follows) and not yet expired, the stored answer comes back
with its original citations: no retrieval, no reranker, no model call, cost 0, model_backend=cache on the row.
Off by default until the threshold is measured on labelled paraphrase pairs from the golden set.
"""
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone

from google.cloud import firestore
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector

# 0.95, not 0.92: RedisVL's default distance of 0.1 is the same neighbourhood, and a cache that answers "what is
# the notice period" to "what is the probation period" is a wrong answer served fast. Tight first, then measured.
THRESHOLD = float(os.environ.get("SEMANTIC_CACHE_THRESHOLD", "0.95"))
TTL_HOURS = int(os.environ.get("SEMANTIC_CACHE_TTL_H", "24"))
CANDIDATES = 5     # nearest earlier questions read per lookup: the first that is near, current and alive wins


def qhash(question: str) -> str:
    """The exact rung's key: the question lower-cased, punctuation out, whitespace collapsed, hashed. The same
    question asked twice never reaches the vector search - two equality filters answer it."""
    words = re.sub(r"[^\w\s]", " ", question.lower()).split()
    return hashlib.sha256(" ".join(words).encode("utf-8")).hexdigest()[:24]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def scope_of(filters: dict | None, top_k: int, prompt_version: str) -> str:
    """What else has to match for an earlier answer to be THIS question's answer: the request's filters (a
    doc_type-filtered question is not the unfiltered one), its top_k, and the prompt version it was answered
    under. One short hash on the entry, compared on lookup; the tenant and the fingerprint are checked beside it."""
    return hashlib.sha256(json.dumps({"filters": filters or {}, "top_k": top_k, "prompt": prompt_version},
                                     sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _alive(d: dict, fingerprint: str | None, scope: str, now: datetime) -> bool:
    """Answered under the corpus the tenant has now, for the same filters / top_k / prompt, and not expired."""
    if d.get("fingerprint", "") != (fingerprint or ""):
        return False                                 # answered under an older corpus
    if d.get("scope", "") != (scope or ""):
        return False                                 # answered for other filters, another top_k or another prompt
    exp = d.get("expire_at")
    return exp is None or exp > now                  # expired entries wait for the TTL policy; they are not served


def lookup(db: firestore.Client, tenant_id: str, qvec: list[float], fingerprint: str | None,
           scope: str = "", question: str | None = None) -> dict | None:
    """The stored entry for the nearest earlier question of THIS tenant - if it is near enough, was answered
    under the corpus the tenant has now, and has not expired. None otherwise.

    Two rungs. Exact first (12 September 2026): the same words, by qhash, cost two equality filters and no
    embedding comparison - the cheapest hit there is, and the one a demo asks for by repeating a question.
    Then near: the CANDIDATES nearest earlier questions by cosine, the first at or above THRESHOLD wins.

    Per tenant, always. A cache keyed on the question alone would serve one customer's answer to another - and
    it would look like a performance win right up until someone noticed. Per fingerprint, always: every entry
    carries the fingerprint it was answered under, so a reindex makes every earlier entry a miss without a
    scan, and a stale twin of the same question cannot hide the current one (CANDIDATES, not 1).
    """
    now = _now()
    if question:
        exact = (db.collection("answer_cache")
                   .where("tenant_id", "==", tenant_id)
                   .where("qhash", "==", qhash(question))
                   .limit(CANDIDATES).get())
        for h in exact:
            d = h.to_dict()
            if _alive(d, fingerprint, scope, now):
                d["rung"] = "exact"
                return d
    hits = (db.collection("answer_cache")
              .where("tenant_id", "==", tenant_id)
              .find_nearest("embedding", Vector(qvec),
                            distance_measure=DistanceMeasure.COSINE,
                            limit=CANDIDATES,
                            distance_result_field="d").get())
    for h in hits:                                   # nearest first
        d = h.to_dict()
        # COSINE distance: smaller is closer. 1 - d is the similarity; the rest are farther still.
        if (1.0 - d.get("d", 1.0)) < THRESHOLD:
            break
        if _alive(d, fingerprint, scope, now):
            d["rung"] = "near"
            return d
    return None


def store(db: firestore.Client, tenant_id: str, question: str, qvec: list[float], answer: dict,
          fingerprint: str | None, model: str = "", scope: str = "") -> None:
    """One entry per answered question: the answer as the contract serialises it (RAGAnswer.model_dump()), the
    model that gave it, the fingerprint it was answered under, and an expire_at the Firestore TTL policy reaps
    (terraform/firestore_indexes.tf, answer_cache_expire_at) - the only thing that ever deletes an entry."""
    db.collection("answer_cache").add({
        "tenant_id": tenant_id, "question": question, "qhash": qhash(question),
        "embedding": Vector(qvec), "answer": answer, "model": model,
        "fingerprint": fingerprint or "", "scope": scope or "",
        "created_at": firestore.SERVER_TIMESTAMP,
        "expire_at": _now() + timedelta(hours=TTL_HOURS),
    })
