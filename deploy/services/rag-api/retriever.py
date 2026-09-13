import hashlib, json, logging, re
from functools import lru_cache
from google.cloud import aiplatform
from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import Namespace
from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
from google.cloud.firestore_v1.vector import Vector
from google.cloud import discoveryengine_v1 as discoveryengine
from google import genai
from google.genai import types
from google.cloud import firestore
from config import settings
from shared.documind_graph import FirestoreGraph, choose_mode   # 4.6's store, the lane's copy (13 September 2026)

@lru_cache(maxsize=1)
def _genai_client():
    return genai.Client(enterprise=True, project=settings.project_id, location=settings.region)

@lru_cache(maxsize=1)
def _index_endpoint():
    return aiplatform.MatchingEngineIndexEndpoint(settings.vector_index_endpoint)

@lru_cache(maxsize=1)
def _fs():
    return firestore.Client(project=settings.project_id, database="(default)")

def embed_query(q: str) -> list[float]:
    # settings.embed_model is EMBEDDING_MODEL in the environment - the SAME variable the ingest worker stamps on
    # every row (variables.tf: embedding_model). Query and document vectors come from one declared model.
    resp = _genai_client().models.embed_content(
        model=settings.embed_model, contents=q,
        config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY", output_dimensionality=768))
    return resp.embeddings[0].values

def _firestore_fallback(vec: list[float], tenant_id: str, top_k: int, filters: dict | None = None) -> list[dict]:
    """Answer from Firestore when Vector Search will not.

    indexer.py mirrors every embedding here as a Vector field precisely so this
    path exists. It is slower and it skips the ANN tier, but a slower answer is
    a different thing from an outage - and this is the rung the chaos drill
    pulls: undeploy the index, ask a question, get an answer anyway.

    The SAME predicates as the index query (12 September 2026, R06): the tenant, the
    ledger's `current`, and the caller's filters as equality pre-filters on the row's own
    fields. Until then this path had no `filters` parameter, so a doc_type-filtered
    question answered from here read the whole tenant.

    Needs the composite index in 12.5's firestore_indexes.tf - one per predicate
    combination. Without it Firestore does not degrade, it refuses.
    """
    query = _fs().collection(settings.chunks_collection).where("tenant_id", "==", tenant_id)
    if settings.retrieval_current_only == "on":
        # The ledger's promise (12.5): one current version per document. The pre-filter needs the second
        # vector index in firestore_indexes.tf (tenant_id, current, embedding).
        query = query.where("current", "==", True)
    for k, v in (filters or {}).items():
        query = query.where(k, "==", v)       # doc_type, kind: the keys schemas.FILTER_KEYS allows, main.py checked
    hits = (query
            .find_nearest("embedding", Vector(vec),
                          distance_measure=DistanceMeasure.COSINE,
                          limit=top_k,
                          distance_result_field="d").get())
    out = []
    for h in hits:
        d = h.to_dict()
        d["id"] = h.id
        # COSINE distance: smaller is closer, so flip it to a score the
        # reranker can order the same way it orders Vector Search results.
        d["score"] = 1.0 - d.pop("d", 1.0)
        d.pop("embedding", None)          # never ship 768 floats to the model
        out.append(d)
    return out

def newest_per_source(chunks: list[dict]) -> list[dict]:
    """One version per source, the newest (12 September 2026). The worker swaps a long document in more than one
    batch, so for a moment two versions of one source can both be current, and a candidate set that held both
    would pack both - the reader would be asked to reconcile v1 with v2. Group by source_uri, keep the doc_key
    whose rows landed last (indexed_at, or reactivated_at for the undo), drop the other version's rows. Rows
    without a doc_key or a timestamp (a lane older than the ledger) pass through untouched."""
    newest: dict = {}
    for c in chunks:
        src, key = c.get("source_uri"), c.get("doc_key")
        at = c.get("reactivated_at") or c.get("indexed_at")
        if not (src and key and at is not None):
            continue
        if src not in newest or at > newest[src][1]:
            newest[src] = (key, at)
    return [c for c in chunks
            if not (c.get("doc_key") and c.get("source_uri") in newest and c["doc_key"] != newest[c["source_uri"]][0])]

def prefer_current(chunks: list[dict]) -> list[dict]:
    """Version-chain dedupe, BEFORE the reranker (12.5's ledger). A chunk the ledger has retired is never a
    source, whether or not its successor was retrieved - the model is not asked to reconcile v1 with v2.
    Chunks without the field (a lane older than the ledger) pass through; a no-op until the flag is stamped.
    Then one version per source: the newest-per-source guard closes the swap window on its own."""
    return newest_per_source([c for c in chunks if c.get("current") is not False])

@lru_cache(maxsize=1)
def _rag():
    """vertexai.rag on the corpora's region (P9.4, 13 September 2026): serverless corpora are us-central1-only (4.3),
    and this client never generates - generation stays on the global client the generator holds."""
    import vertexai
    from vertexai import rag
    vertexai.init(project=settings.project_id, location=settings.rag_location)
    return rag

_corpora = {}          # tenant_id -> the corpus's resource name, once found

def _rag_corpus(tenant_id: str) -> str | None:
    """The tenant's corpus, by the mirror's name (documind-{tenant}, services/ingest/managed.py), found once and kept;
    None while the tenant has none - looked up again on the next question, never created here."""
    if tenant_id not in _corpora:
        want = "documind-" + re.sub(r"[^a-z0-9-]+", "-", tenant_id.lower()).strip("-")
        name = next((c.name for c in _rag().list_corpora() if c.display_name == want), None)
        if name is None:
            return None
        _corpora[tenant_id] = name
    return _corpora[tenant_id]

def _version_row(tenant_id: str, doc_key: str) -> dict | None:
    """What a managed context lacks, from the kit's own rows: the version's source_uri, doc_type, effective_from,
    indexed_at - and `current`, read fresh, so a version the ledger retired while the store still held it is dropped
    by prefer_current() like any retired row. One small read per distinct version in the pool."""
    for snap in (_fs().collection(settings.chunks_collection).where("tenant_id", "==", tenant_id)
                 .where("doc_key", "==", doc_key).limit(1).stream()):
        d = snap.to_dict() or {}
        return {k: d.get(k) for k in ("source_uri", "doc_type", "effective_from", "indexed_at", "reactivated_at", "current")}
    return None

def _page_span(ctx) -> str:
    span = getattr(getattr(ctx, "chunk", None), "page_span", None)
    first, last = getattr(span, "first_page", 0) or 0, getattr(span, "last_page", 0) or 0
    return f"p{first}" + (f"-{last}" if last and last != first else "") if first else ""

def _media_rows(vec: list[float], tenant_id: str, top_k: int, filters: dict | None = None) -> list[dict]:
    """The kit's own figure and segment rows (Module 9), for a pool a managed backend served: a store holds text only
    (the plan's D4), so media comes from Firestore's vector index under the tenant + kind index firestore_indexes.tf
    declares. A doc_type filter is applied to the rows returned - the kind index carries no doc_type."""
    query = _fs().collection(settings.chunks_collection).where("tenant_id", "==", tenant_id)
    if settings.retrieval_current_only == "on":
        query = query.where("current", "==", True)
    if filters and filters.get("kind"):
        query = query.where("kind", "==", filters["kind"])          # the caller named the kind: one equality, as the fallback does
    else:
        query = query.where("kind", "in", ["figure", "segment"])     # the media kinds a store never holds
    hits = query.find_nearest("embedding", Vector(vec), distance_measure=DistanceMeasure.COSINE,
                              limit=top_k, distance_result_field="d").get()
    out = []
    for h in hits:
        d = h.to_dict()
        if filters and filters.get("doc_type") and d.get("doc_type") != filters["doc_type"]:
            continue
        d["id"], d["score"] = h.id, 1.0 - d.pop("d", 1.0)
        d.pop("embedding", None)
        out.append(d)
    return out

def _managed_retrieve(query: str, tenant_id: str, top_k: int, filters: dict | None = None,
                      vec: list[float] | None = None) -> list[dict]:
    """RETRIEVAL_BACKEND=rag_engine (P9.4, 13 September 2026): lesson 4.3's corpus as the kit's retrieval stage, and
    nothing more - the reranker, the packing, the generator and the citations stay the kit's (the plan's D1).

    The tenant's corpus is the mirror's (services/ingest/managed.py: one per tenant, a RagFile per version named by
    its doc_key, the version's own text), queried by text with 4.3's distance threshold. Each context comes back with
    the RagFile's display name - the doc_key - and that is enough to make it a chunk of the kit's contract: the
    version's row gives it source_uri, doc_type, effective_from, indexed_at and a fresh `current`; the id is stable
    (tenant, doc_key, a hash of the text) so the answer cache and the citations hold; the score is 1 - distance,
    the scale the Firestore path uses. A caller's filters apply to the mapped chunks as on every path; figure and
    segment rows join from the kit's own index (D4), unless the caller asked for text alone. A tenant with no
    corpus, or a store that will not answer, is the Firestore rung with the filters, logged rag_engine_fallback -
    never an empty pool that reads as a refusal. A tenant whose data_region is `in` never reaches this: main.py's
    choose_for sends it to the kit's own index (policy_fallback) before retrieve() is called (13 September 2026)."""
    kind = (filters or {}).get("kind")
    if kind and kind != "text":                        # figures or segments only: the store has none, the index has them
        return _media_rows(vec, tenant_id, settings.top_k_retrieve, filters)
    corpus = _rag_corpus(tenant_id)
    if corpus is None:
        logging.warning(json.dumps({"event": "rag_engine_fallback", "tenant": tenant_id,
                                    "reason": f"no corpus for the tenant: make rag-corpus TENANT={tenant_id}"}))
        return _firestore_fallback(vec, tenant_id, settings.top_k_retrieve, filters)
    try:
        rag = _rag()
        resp = rag.retrieval_query(
            rag_resources=[rag.RagResource(rag_corpus=corpus)], text=query,
            rag_retrieval_config=rag.RagRetrievalConfig(
                top_k=settings.top_k_retrieve,
                filter=rag.Filter(vector_distance_threshold=settings.rag_distance_threshold)))
        contexts = list(resp.contexts.contexts)
    except Exception as e:
        logging.warning(json.dumps({"event": "rag_engine_fallback", "tenant": tenant_id, "error": str(e)[:200]}))
        return _firestore_fallback(vec, tenant_id, settings.top_k_retrieve, filters)
    rows: dict[str, dict | None] = {}
    out = []
    for ctx in contexts:
        doc_key, text = getattr(ctx, "source_display_name", "") or "", getattr(ctx, "text", "") or ""
        if not doc_key or not text.strip():
            continue
        if doc_key not in rows:
            rows[doc_key] = _version_row(tenant_id, doc_key)
        row = rows[doc_key]
        if row is None:                                # a file the ledger does not know: never served
            continue
        chunk = {"id": f"{tenant_id}:{doc_key}#rag-{hashlib.sha256(text.encode('utf-8')).hexdigest()[:12]}",
                 "text": text, "source_uri": row.get("source_uri") or "", "doc_key": doc_key,
                 "doc_type": row.get("doc_type"), "kind": "text", "effective_from": row.get("effective_from"),
                 "indexed_at": row.get("indexed_at"), "reactivated_at": row.get("reactivated_at"),
                 "current": row.get("current"), "locator": _page_span(ctx),
                 "score": max(0.0, 1.0 - float(getattr(ctx, "score", 0.0) or 0.0)), "found_by": "rag_engine"}
        if any(chunk.get(k) != v for k, v in (filters or {}).items()):
            continue
        out.append(chunk)
    if kind != "text":
        out += _media_rows(vec, tenant_id, max(3, settings.top_k_retrieve // 4), filters)
    return sorted(out, key=lambda c: -c["score"])[:settings.top_k_retrieve]

def _dense_retrieve(query: str, tenant_id: str, top_k: int, filters: dict | None = None,
                    vec: list[float] | None = None, backend: str | None = None) -> list[dict]:
    """The dense pool: Vector Search (dense or hybrid) with the Firestore fallback beneath it, Firestore's own
    vector index on the lean profile, or a managed store (rag_engine, P9.4) - the same tenant / current / filter
    predicates on every path. `backend` is the one main.py chose for THIS request (the tenant's pin, held against
    its data_region - 13 September 2026, evening); the deployment's RETRIEVAL_BACKEND when the caller names none."""
    backend = backend or settings.retrieval_backend
    vec = vec if vec is not None else embed_query(query)    # main.py embeds once: the answer cache looked it up first
    if backend == "rag_engine":
        return prefer_current(_managed_retrieve(query, tenant_id, top_k, filters, vec=vec))
    if backend == "firestore":
        # The lean profile (deploy/README.md): no Vector Search endpoint exists, on purpose.
        # Firestore holds every embedding indexer.py wrote and its own vector index answers,
        # tenant pre-filtered - the fallback below, chosen rather than fallen into.
        return prefer_current(_firestore_fallback(vec, tenant_id, settings.top_k_retrieve, filters))
    restricts = [Namespace(name="tenant_id", allow_tokens=[tenant_id])]
    if settings.retrieval_current_only == "on":
        restricts.append(Namespace(name="current", allow_tokens=["true"]))   # indexer.py's third restrict
    if filters:
        for k, v in filters.items():
            restricts.append(Namespace(name=k, allow_tokens=[str(v)]))
    try:
        if settings.retrieval_mode == "hybrid":
            # 4.5's hybrid.py, wired. Dense recall misses exact tokens - an
            # invoice number, a clause id - and sparse misses paraphrase. RRF
            # over both is what 4.5 measured; this is where it earns its keep.
            # The SAME restricts as the dense query (12 September 2026): a filter is a
            # predicate on the corpus, not on one of the two ways through it.
            from hybrid import hybrid_find_neighbors
            neighbours = hybrid_find_neighbors(
                _index_endpoint(), settings.vector_deployed_index, vec,
                query, tenant_id, settings.top_k_retrieve, alpha=0.7, restricts=restricts)
        else:
            resp = _index_endpoint().find_neighbors(
                deployed_index_id=settings.vector_deployed_index,
                queries=[vec], num_neighbors=settings.top_k_retrieve,
                filter=restricts,
            )
            # One query in, one neighbour list out - and an EMPTY outer list when the index
            # holds nothing for these restricts. That is an empty pool (main.py answers it
            # without a model call), not an outage: it must not fall through to the except
            # below and come back from Firestore as if the index were down (12 September 2026).
            neighbours = resp[0] if resp else []
    except Exception as e:
        # The chaos rung. An undeployed or unreachable index raises here;
        # Firestore holds the same vectors, so answer from there and say so
        # in the log rather than returning nothing. Hybrid takes the same rung:
        # an outage degrades to dense, logged - it never 500s.
        logging.warning(json.dumps({"event": "vector_search_fallback",
                                    "tenant": tenant_id, "error": str(e)[:200]}))
        return prefer_current(_firestore_fallback(vec, tenant_id, settings.top_k_retrieve, filters))
    ids = [n.id for n in neighbours]
    scores = {n.id: n.distance for n in neighbours}
    # Fan-out to Firestore for chunk payloads. ALL of them: we asked Vector
    # Search for top_k_retrieve (20) and then used to fetch ids[:10], so half
    # of every retrieval was thrown away before the reranker ever saw it.
    # Firestore's `in` takes up to 30 values: 20 is one query, and a pool of 50
    # (TOP_K_RETRIEVE, the knob the ablation moves) is two - never one query
    # over the limit, which Firestore refuses rather than truncates.
    return prefer_current(_hydrate(ids[:settings.top_k_retrieve], scores))


def graph_candidates(query: str, tenant_id: str, filters: dict | None = None) -> list[dict]:
    """4.6's graph, on the lane (13 September 2026): the chunks the tenant's knowledge graph points at for this question.

    RETRIEVAL_GRAPH=off returns nothing and touches nothing. `on` walks for every question; `auto` walks only when the
    question is relational and a seed entity is found (shared/documind_graph.choose_mode - no model call). The walk is
    the notebook's own class (FirestoreGraph: seed by containment, expand one or two hops, capped) and it is
    tenant-scoped in every read; the chunk ids it hands over are fetched from the one chunks collection and checked
    once more - the tenant, the caller's filters, the ledger's current - the same predicates every other path applies.
    A tenant with no graph, or a question with no seed, is an empty list: dense retrieval answers as before."""
    mode = settings.retrieval_graph
    if mode == "off":
        return []
    g = FirestoreGraph(_fs())
    seeds = g.seed(query, tenant_id)
    if not seeds or (mode == "auto" and choose_mode(query, seeds) != "graph"):
        return []
    nodes = g.expand([s["node_id"] for s in seeds], tenant_id, hops=settings.graph_hops, cap=settings.graph_cap)
    out = []
    for cid in sorted({c for n in nodes for c in n["chunk_ids"]}):
        snap = _fs().collection(settings.chunks_collection).document(cid).get()
        if not snap.exists:
            continue
        d = snap.to_dict() or {}
        if d.get("tenant_id") != tenant_id:            # the walk is tenant-scoped; the id it hands over is checked once more
            continue
        if any(d.get(k) != v for k, v in (filters or {}).items()):   # doc_type, kind: the same predicates as every path
            continue
        if settings.retrieval_current_only == "on" and d.get("current") is not True:
            continue
        d["id"] = cid
        d["score"] = 1.0                               # fetched by id: an exact hit, ahead of the dense pool
        d["found_by"] = "graph"
        d.pop("embedding", None)
        out.append(d)
    return out


def retrieve(query: str, tenant_id: str, top_k: int, filters: dict | None = None,
             vec: list[float] | None = None, backend: str | None = None) -> list[dict]:
    """The pool the reranker sees: the graph's chunks first, when RETRIEVAL_GRAPH says so, then the dense candidates
    that are not already in it, cut to TOP_K_RETRIEVE; a retired version never survives either half. `backend` is
    the request's (main.py), the setting by default."""
    graph = graph_candidates(query, tenant_id, filters)
    dense = _dense_retrieve(query, tenant_id, top_k, filters, vec=vec, backend=backend)
    if not graph:
        return dense
    seen = {c["id"] for c in graph}
    return prefer_current(graph + [c for c in dense if c["id"] not in seen])[:settings.top_k_retrieve]


def _hydrate(ids: list[str], scores: dict) -> list[dict]:
    """The chunk payloads for Vector Search's ids, in Vector Search's order, 30 ids per `in` query; nothing for none."""
    by_id: dict[str, dict] = {}
    for start in range(0, len(ids), 30):
        for doc in _fs().collection(settings.chunks_collection).where(
                "__name__", "in", ids[start:start + 30]).stream():
            d = doc.to_dict(); d["id"] = doc.id; d["score"] = scores.get(doc.id, 0)
            by_id[doc.id] = d
    return [by_id[i] for i in ids if i in by_id]

@lru_cache(maxsize=1)
def _ranker():
    return discoveryengine.RankServiceClient()

def _by_retrieval_score(chunks: list[dict], k: int) -> list[dict]:
    """The pool by retrieval score, cut to k, every row marked - what rerank() returns when the Ranking API
    cannot answer. `score` is a similarity on both backends (DOT_PRODUCT on the index, vector.tf; 1 - cosine
    on the Firestore fallback): highest first, ties in the order retrieval gave."""
    out = sorted(chunks, key=lambda c: -(c.get("score") or 0.0))[:k]
    for c in out:
        c["rerank_fallback"] = True
    return out


def rerank_fell_back(chunks: list[dict]) -> bool:
    """True when rerank() stood the pool in for the Ranking API: the handlers put stages['rerank_fallback'] = 1 on
    the row and in the answer, so a day of fallbacks is a count, not a hunch about worse answers."""
    return any(c.get("rerank_fallback") for c in chunks)


def rerank(query: str, chunks: list[dict], k: int, tenant_id: str | None = None) -> list[dict]:
    """The Ranking API's order, top k - or, when it does not answer inside RERANK_TIMEOUT_S or raises at all,
    the pool by retrieval score, logged as rerank_fallback (12 September 2026, R06). Before this a slow or absent
    ranker was a hung request and then a 500; a worse order is a degraded answer, and the row says so."""
    if not chunks: return chunks
    chunks = chunks[:200]          # the Ranking API takes at most 200 records; the pool is TOP_K_RETRIEVE, never more
    try:
        client = _ranker()
        ranking_config = client.ranking_config_path(
            project=settings.project_id, location="global",
            ranking_config="default_ranking_config")
        records = [discoveryengine.RankingRecord(id=str(i), content=c["text"])
                   for i, c in enumerate(chunks)]
        resp = client.rank(request=discoveryengine.RankRequest(
            ranking_config=ranking_config,
            model=settings.rerank_model,
            top_n=k, query=query, records=records),
            timeout=settings.rerank_timeout_s)      # gapic's deadline for the whole call, retries included
    except Exception as e:  # noqa: BLE001 - a deadline, a quota, a client that will not build: one answer
        logging.warning(json.dumps({"event": "rerank_fallback", "tenant": tenant_id,
                                    "error": type(e).__name__}))
        return _by_retrieval_score(chunks, k)
    out = []
    for r in resp.records:
        chunks[int(r.id)]["rerank_score"] = r.score
        out.append(chunks[int(r.id)])
    return out
