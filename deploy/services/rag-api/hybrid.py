"""Hybrid retrieval for rag-api (lesson 4.5): dense + sparse with Reciprocal Rank Fusion.

Production path: Vector Search HybridQuery fuses server-side with `rrf_ranking_alpha`.
Fallback / learner lane: `rrf_fuse()` merges two id lists in-process (Firestore dense + BM25).
The sparse encoder is shared with ingestion and repair so document and query vectors always
occupy the same deterministic space.
"""
from __future__ import annotations

from shared.sparse_encoder import sparse_encode


def rrf_fuse(dense_ids: list[str], sparse_ids: list[str], alpha: float = 0.5, k: int = 60) -> list[tuple[str, float]]:
    """alpha=1 dense only, 0 sparse only. Same semantics as HybridQuery.rrf_ranking_alpha."""
    fused: dict[str, float] = {}
    for rank, cid in enumerate(dense_ids):
        fused[cid] = fused.get(cid, 0.0) + alpha / (k + rank + 1)
    for rank, cid in enumerate(sparse_ids):
        fused[cid] = fused.get(cid, 0.0) + (1 - alpha) / (k + rank + 1)
    return sorted(fused.items(), key=lambda kv: -kv[1])


def hybrid_find_neighbors(index_endpoint, deployed_index_id: str, dense_vec: list[float], query_text: str,
                          tenant_id: str, k: int = 20, alpha: float = 0.5, restricts: list | None = None) -> list:
    """Vector Search hybrid query with the tenant restrict in the query (never post-filter).

    Returns the ONE query's neighbours - a flat list of MatchNeighbor - and [] when there are none.
    find_neighbors answers List[List[...]], one inner list per query sent, and this sends one query.

    `restricts`: the Namespace list the dense path sends - the tenant, the ledger's `current`,
    and the caller's filters. The tenant restrict is always inserted exactly once.
    """
    from google.cloud.aiplatform.matching_engine.matching_engine_index_endpoint import HybridQuery, Namespace
    vals, dims = sparse_encode(query_text)
    q = HybridQuery(dense_embedding=dense_vec, sparse_embedding_values=vals,
                    sparse_embedding_dimensions=dims, rrf_ranking_alpha=alpha)
    filters = [Namespace(name="tenant_id", allow_tokens=[tenant_id])]
    filters += [r for r in (restricts or []) if getattr(r, "name", None) != "tenant_id"]
    resp = index_endpoint.find_neighbors(
        deployed_index_id=deployed_index_id, queries=[q], num_neighbors=k, filter=filters)
    return resp[0] if resp else []
