"""One deterministic sparse encoder shared by ingestion, repair and query-time hybrid retrieval.

The Vector Search hybrid path only works when document sparse vectors and query sparse vectors
live in the same space. Keep this module dependency-free so both Cloud Run images and offline
checks can import the exact same implementation.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter

SPARSE_DIMS = 1 << 20
SPARSE_ENCODER_VERSION = "blake2b-tf-v1"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9\-]+", (text or "").lower())


def sparse_encode(text: str) -> tuple[list[float], list[int]]:
    """Return ``(values, dimensions)`` in Vertex AI Vector Search sparse format.

    Tokens are hashed into a fixed 20-bit space and weighted with the same simple TF curve used
    by the original query-side hybrid implementation. Hash collisions are folded into one
    dimension so every sparse embedding has unique, sorted dimension ids.
    """
    per_dimension: dict[int, float] = {}
    for token, count in Counter(_tokens(text)).items():
        dimension = int(hashlib.blake2b(token.encode(), digest_size=4).hexdigest(), 16) % SPARSE_DIMS
        weight = 1.0 + (count - 1) * 0.5
        per_dimension[dimension] = per_dimension.get(dimension, 0.0) + weight
    dimensions = sorted(per_dimension)
    return [per_dimension[d] for d in dimensions], dimensions
