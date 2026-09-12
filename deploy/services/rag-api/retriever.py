import json, logging
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

def _firestore_fallback(vec: list[float], tenant_id: str, top_k: int) -> list[dict]:
    """Answer from Firestore when Vector Search will not.

    indexer.py mirrors every embedding here as a Vector field precisely so this
    path exists. It is slower and it skips the ANN tier, but a slower answer is
    a different thing from an outage - and this is the rung the chaos drill
    pulls: undeploy the index, ask a question, get an answer anyway.

    Needs the composite index in 12.5's firestore_indexes.tf. Without it
    Firestore does not degrade, it refuses.
    """
    query = _fs().collection(settings.chunks_collection).where("tenant_id", "==", tenant_id)
    if settings.retrieval_current_only == "on":
        # The ledger's promise (12.5): one current version per document. The pre-filter needs the second
        # vector index in firestore_indexes.tf (tenant_id, current, embedding).
        query = query.where("current", "==", True)
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

def retrieve(query: str, tenant_id: str, top_k: int, filters: dict | None = None) -> list[dict]:
    vec = embed_query(query)
    if settings.retrieval_backend == "firestore":
        # The lean profile (deploy/README.md): no Vector Search endpoint exists, on purpose.
        # Firestore holds every embedding indexer.py wrote and its own vector index answers,
        # tenant pre-filtered - the fallback below, chosen rather than fallen into.
        return prefer_current(_firestore_fallback(vec, tenant_id, settings.top_k_retrieve))
    restricts = [Namespace(name="tenant_id", allow_tokens=[tenant_id])]
    if settings.retrieval_current_only == "on":
        restricts.append(Namespace(name="current", allow_tokens=["true"]))   # indexer.py's third restrict
    if filters:
        for k, v in filters.items():
            restricts.append(Namespace(name=k, allow_tokens=[str(v)]))
    if settings.retrieval_mode == "hybrid":
        # 4.5's hybrid.py, wired. Dense recall misses exact tokens - an
        # invoice number, a clause id - and sparse misses paraphrase. RRF
        # over both is what 4.5 measured; this is where it earns its keep.
        from hybrid import hybrid_find_neighbors
        neighbours = hybrid_find_neighbors(
            _index_endpoint(), settings.vector_deployed_index, vec,
            query, tenant_id, settings.top_k_retrieve, alpha=0.7)
    else:
        try:
            resp = _index_endpoint().find_neighbors(
                deployed_index_id=settings.vector_deployed_index,
                queries=[vec], num_neighbors=settings.top_k_retrieve,
                filter=restricts,
            )
            neighbours = resp[0]
        except Exception as e:
            # The chaos rung. An undeployed or unreachable index raises here;
            # Firestore holds the same vectors, so answer from there and say so
            # in the log rather than returning nothing.
            logging.warning(json.dumps({"event": "vector_search_fallback",
                                        "tenant": tenant_id, "error": str(e)[:200]}))
            return prefer_current(_firestore_fallback(vec, tenant_id, settings.top_k_retrieve))
    ids = [n.id for n in neighbours]
    scores = {n.id: n.distance for n in neighbours}
    # Fan-out to Firestore for chunk payloads. ALL of them: we asked Vector
    # Search for top_k_retrieve (20) and then used to fetch ids[:10], so half
    # of every retrieval was thrown away before the reranker ever saw it.
    # Firestore's `in` takes up to 30 values, so 20 is one query.
    chunks = []
    for doc in _fs().collection(settings.chunks_collection).where(
            "__name__", "in", ids[:settings.top_k_retrieve]).stream():
        d = doc.to_dict(); d["id"] = doc.id; d["score"] = scores.get(doc.id, 0)
        chunks.append(d)
    return prefer_current(chunks)

@lru_cache(maxsize=1)
def _ranker():
    return discoveryengine.RankServiceClient()

def rerank(query: str, chunks: list[dict], k: int) -> list[dict]:
    if not chunks: return chunks
    client = _ranker()
    ranking_config = client.ranking_config_path(
        project=settings.project_id, location="global",
        ranking_config="default_ranking_config")
    records = [discoveryengine.RankingRecord(id=str(i), content=c["text"])
               for i, c in enumerate(chunks)]
    resp = client.rank(request=discoveryengine.RankRequest(
        ranking_config=ranking_config,
        model=settings.rerank_model,
        top_n=k, query=query, records=records))
    out = []
    for r in resp.records:
        chunks[int(r.id)]["rerank_score"] = r.score
        out.append(chunks[int(r.id)])
    return out
