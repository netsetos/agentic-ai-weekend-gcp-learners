"""Hybrid retrieval for rag-api (lesson 4.5): dense + sparse with Reciprocal Rank Fusion.

Production path: Vector Search HybridQuery fuses server-side with `rrf_ranking_alpha`.
Fallback / learner lane: `rrf_fuse()` merges two id lists in-process (Firestore dense + BM25).
Sparse vectors here use the hashing trick with TF weights; 12.5 replaces the encoder with the
BM25 vocabulary fitted at ingest so query and document vectors share one space.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter

SPARSE_DIMS = 1 << 20


def _tok(text: str) -> list[str]:
    return re.findall(r"[a-z0-9\-]+", text.lower())


def sparse_encode(text: str) -> tuple[list[float], list[int]]:
    """(values, dimensions) as Vector Search expects for a sparse embedding."""
    counts = Counter(_tok(text))
    dims, vals = [], []
    for token, n in counts.items():
        h = int(hashlib.blake2b(token.encode(), digest_size=4).hexdigest(), 16) % SPARSE_DIMS
        dims.append(h)
        vals.append(1.0 + (n - 1) * 0.5)
    return vals, dims


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
    find_neighbors answers List[List[...]], one inner list per query sent, and this sends one query;
    until 12 September 2026 the outer list came back as it was, so retriever.py iterated a single
    element that was itself a list and stopped on `.id` (R06). An empty answer is an empty pool,
    not an error: main.py answers it without a model call.

    `restricts` (12 September 2026): the Namespace list the dense path sends - the tenant, the
    ledger's `current`, the caller's filters - so a doc_type filter means the same thing on both
    paths (R06, "filters dropped"). The tenant restrict is put in whatever the caller passed: a
    hybrid query without it would read every tenant's rows.
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
