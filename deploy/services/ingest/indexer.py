"""Embed, upsert to Vector Search, mirror into Firestore, and repair the vector tier.

Dense and sparse representations are produced together. The sparse encoder is shared with
rag-api/hybrid.py, so ingestion, undo/reupsert, backfill and query-time hybrid retrieval all use
the same deterministic sparse space.
"""
import os
import math

from google.cloud import aiplatform
from google.cloud import firestore
from google.cloud.aiplatform_v1.types import IndexDatapoint
from google.cloud.firestore_v1.vector import Vector
from google import genai

from contracts import SCHEMA_VERSION
from shared.sparse_encoder import SPARSE_ENCODER_VERSION, sparse_encode

EMBED_BATCH = 250
EMBED_TOKENS = 15_000
CHARS_PER_TOKEN = 3
DRY_RUN = os.environ.get("VECTOR_DRY_RUN") == "1"
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-005")
EMBEDDING_VERSION = os.environ.get("EMBEDDING_VERSION", "1")
EMBEDDING_TASK_TYPE = "RETRIEVAL_DOCUMENT"
EMBEDDING_DIMENSIONS = 768

_embed = genai.Client(
    enterprise=True,
    project=os.environ["GOOGLE_CLOUD_PROJECT"],
    location="us-central1",
)


def batches(texts: list[str]) -> list[list[str]]:
    out, cur, cur_tokens = [], [], 0
    for text in texts:
        tokens = max(1, len(text) // CHARS_PER_TOKEN)
        if cur and (len(cur) >= EMBED_BATCH or cur_tokens + tokens > EMBED_TOKENS):
            out.append(cur)
            cur, cur_tokens = [], 0
        cur.append(text)
        cur_tokens += tokens
    if cur:
        out.append(cur)
    return out


def embed_all(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for batch in batches(texts):
        response = _embed.models.embed_content(
            model=EMBEDDING_MODEL,
            contents=batch,
            config={"output_dimensionality": EMBEDDING_DIMENSIONS, "task_type": EMBEDDING_TASK_TYPE},
        )
        vectors = [list(embedding.values) for embedding in (response.embeddings or [])]
        if len(vectors) != len(batch) or any(not valid_vector(v) for v in vectors):
            raise ValueError("Embedding response must contain one finite 768-dimensional vector per text")
        out.extend(vectors)
    return out


def valid_vector(vector) -> bool:
    return (vector is not None and len(list(vector)) == EMBEDDING_DIMENSIONS
            and all(math.isfinite(v) for v in vector))


def document_embedding_matches(row: dict) -> bool:
    """Unstamped legacy vectors used the API default (RETRIEVAL_QUERY), not document encoding."""
    return (row.get("embedding_model") == EMBEDDING_MODEL
            and str(row.get("embedding_version")) == str(EMBEDDING_VERSION)
            and row.get("embedding_task_type") == EMBEDDING_TASK_TYPE
            and valid_vector(row.get("embedding")))


def held_vectors(db: firestore.Client, doc, chunks_collection: str = "chunks") -> dict[str, list[float]]:
    """Reuse a prior CURRENT dense vector only when the chunk hash and embedding stamp match."""
    out: dict[str, list[float]] = {}
    query = (
        db.collection(chunks_collection)
        .where("tenant_id", "==", doc.tenant_id)
        .where("source_uri", "==", doc.gcs_uri)
        .where("current", "==", True)
    )
    for snap in query.stream():
        row = snap.to_dict() or {}
        chunk_hash, vector = row.get("chunk_hash"), row.get("embedding")
        if chunk_hash and document_embedding_matches(row):
            out[chunk_hash] = list(vector)
    return out


def plan_carry_over(chunks: list[dict], held: dict[str, list[float]]) -> tuple[list, list[int]]:
    vectors, misses = [], []
    for index, chunk in enumerate(chunks):
        vector = held.get(chunk.get("chunk_hash") or "")
        vectors.append(vector)
        if vector is None:
            misses.append(index)
    return vectors, misses


def embed_with_carry_over(db: firestore.Client, doc, chunks: list[dict]) -> tuple[list[list[float]], dict]:
    held = held_vectors(db, doc)
    vectors, misses = plan_carry_over(chunks, held)
    fresh = embed_all([chunks[index]["text"] for index in misses]) if misses else []
    for index, vector in zip(misses, fresh):
        vectors[index] = vector
    return vectors, {"reused": len(chunks) - len(misses), "embedded": len(misses)}


def _restricts(tenant_id: str, kind: str, doc_type: str) -> list:
    return [
        IndexDatapoint.Restriction(namespace="tenant_id", allow_list=[tenant_id]),
        IndexDatapoint.Restriction(namespace="kind", allow_list=[kind]),
        IndexDatapoint.Restriction(namespace="doc_type", allow_list=[doc_type]),
        IndexDatapoint.Restriction(namespace="current", allow_list=["true"]),
    ]


def _sparse(text: str) -> IndexDatapoint.SparseEmbedding:
    values, dimensions = sparse_encode(text)
    return IndexDatapoint.SparseEmbedding(values=values, dimensions=dimensions)


def _datapoint(datapoint_id: str, dense_vector: list[float], text: str,
               tenant_id: str, kind: str, doc_type: str) -> IndexDatapoint:
    """The one production datapoint shape used by ingest, undo and repair."""
    return IndexDatapoint(
        datapoint_id=datapoint_id,
        feature_vector=list(dense_vector),
        sparse_embedding=_sparse(text),
        restricts=_restricts(tenant_id, kind, doc_type),
    )


def to_datapoints(doc, chunks: list[dict], vectors: list[list[float]]) -> list[IndexDatapoint]:
    """Build dense+sparse Vector Search datapoints from canonical chunk text."""
    return [
        _datapoint(
            doc.chunk_id(index),
            vector,
            chunk["text"],
            doc.tenant_id,
            chunk.get("kind", "text"),
            doc.doc_type,
        )
        for index, (chunk, vector) in enumerate(zip(chunks, vectors))
    ]


def upsert(index_name: str, datapoints: list[IndexDatapoint]) -> None:
    if not datapoints:
        return
    if DRY_RUN:
        print(
            f"  [dry-run] would upsert {len(datapoints)} datapoints, "
            f"ids {datapoints[0].datapoint_id} .. {datapoints[-1].datapoint_id}"
        )
        return
    aiplatform.MatchingEngineIndex(index_name).upsert_datapoints(datapoints=datapoints)


def remove_datapoints(index_name: str, ids: list[str]) -> None:
    if not ids:
        return
    if DRY_RUN:
        print(f"  [dry-run] would remove {len(ids)} datapoints, ids {ids[0]} .. {ids[-1]}")
        return
    aiplatform.MatchingEngineIndex(index_name).remove_datapoints(datapoint_ids=ids)


def reupsert(index_name: str, db: firestore.Client, tenant_id: str, gcs_uri: str, doc_key: str,
             chunks_collection: str = "chunks") -> int:
    """Restore one reactivated document to Vector Search without changing its ids."""
    query = (
        db.collection(chunks_collection)
        .where("tenant_id", "==", tenant_id)
        .where("source_uri", "==", gcs_uri)
        .where("current", "==", True)
    )
    snapshots = [snap for snap in query.stream()
                 if ((snap.to_dict() or {}).get("doc_key")
                     or snap.id.split("#")[0].replace(":", "_", 1)) == doc_key]
    return _repair_snapshots(index_name, db, snapshots)


def _repair_snapshots(index_name: str, db: firestore.Client, snapshots, batch_size: int = 100) -> int:
    """Upsert first, then checkpoint repaired Firestore vectors with optimistic concurrency.

    Run during an ingestion maintenance window: Vector Search and Firestore do not share a
    transaction. A failed ANN write never marks a row migrated. A failed Firestore commit leaves
    the whole batch retryable. Existing ids, text, current flags and source generations are retained.
    """
    if DRY_RUN:
        raise ValueError("VECTOR_DRY_RUN must be unset for vector repair; use the CLI without --apply to plan")
    if not 1 <= batch_size <= 100:
        raise ValueError("batch_size must be between 1 and 100")
    total, pending = 0, []

    def repair(group):
        rows = [snap.to_dict() or {} for snap in group]
        for snap, row in zip(group, rows):
            if not row.get("current") or row.get("staged") or not row.get("tenant_id") or not row.get("text"):
                raise ValueError(f"Refusing invalid current chunk: {snap.id}")
        misses = [i for i, row in enumerate(rows) if not document_embedding_matches(row)]
        fresh = embed_all([rows[i]["text"] for i in misses]) if misses else []
        replacement = dict(zip(misses, fresh))
        points = [_datapoint(snap.id, replacement[i] if i in replacement else list(row["embedding"]),
                             row["text"], row["tenant_id"], row.get("kind") or "text",
                             row.get("doc_type") or "unknown")
                  for i, (snap, row) in enumerate(zip(group, rows))]
        upsert(index_name, points)
        if replacement:
            batch = db.batch()
            for i, vector in replacement.items():
                snap = group[i]
                batch.update(snap.reference, {
                    "embedding": Vector(vector), "embedding_model": EMBEDDING_MODEL,
                    "embedding_version": EMBEDDING_VERSION, "embedding_task_type": EMBEDDING_TASK_TYPE,
                    "embedding_updated_at": firestore.SERVER_TIMESTAMP,
                }, option=db.write_option(last_update_time=snap.update_time))
            batch.commit()
        return len(group)

    for snap in snapshots:
        pending.append(snap)
        if len(pending) == batch_size:
            total += repair(pending)
            pending = []
    if pending:
        total += repair(pending)
    return total


def backfill(index_name: str, db: firestore.Client, tenant_id: str | None = None,
             chunks_collection: str = "chunks", batch_size: int = 100) -> int:
    """Rebuild CURRENT datapoints and persist missing/legacy document embeddings.

    Already stamped model/version/task/dimension matches reuse their vectors on a retry.
    Re-uploading unchanged source bytes is deliberately not required.
    """
    query = db.collection(chunks_collection).where("current", "==", True)
    if tenant_id:
        query = query.where("tenant_id", "==", tenant_id)
    return _repair_snapshots(index_name, db, list(query.stream()), batch_size)


def mirror_to_firestore(db: firestore.Client, doc, chunks: list[dict],
                        vectors: list[list[float]], staged: bool = False, stage_expire_at=None) -> None:
    """Write the canonical payload row and dense fallback vector to Firestore."""
    batch, pending = db.batch(), 0
    for index, (chunk, vector) in enumerate(zip(chunks, vectors)):
        ref = db.collection("chunks").document(doc.chunk_id(index))
        row = {
            "tenant_id": doc.tenant_id,
            "text": chunk["text"],
            "source_uri": doc.gcs_uri,
            "page_start": chunk.get("page_start"),
            "doc_type": doc.doc_type,
            "kind": chunk.get("kind", "text"),
            "doc_key": doc.doc_key,
            "current": not staged,
            "indexed_at": firestore.SERVER_TIMESTAMP,
            "chunk_hash": chunk.get("chunk_hash"),
            "locator": chunk.get("locator"),
            "embedding_model": EMBEDDING_MODEL,
            "embedding_version": EMBEDDING_VERSION,
            "embedding_task_type": EMBEDDING_TASK_TYPE,
            "sparse_encoder_version": SPARSE_ENCODER_VERSION,
            "schema_version": SCHEMA_VERSION,
            "embedding": Vector(vector),
        }
        if chunk.get("section"):
            row["section"] = chunk["section"]
        if staged:
            row["staged"] = True
            if stage_expire_at is not None:
                row["expire_at"] = stage_expire_at
        if getattr(doc, "effective_from", None):
            row["effective_from"] = doc.effective_from
        for key in ("media_url", "start", "end"):
            if chunk.get(key) is not None:
                row[key] = chunk[key]
        batch.set(ref, row)
        pending += 1
        if pending == 400:
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()


def mirror_to_bigquery(bq, table: str, doc, chunks: list[dict], pii_chunk_ids: set) -> int:
    if not table:
        return 0
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    rows = [
        {
            "chunk_id": doc.chunk_id(index),
            "tenant_id": doc.tenant_id,
            "text": chunk["text"],
            "source_uri": doc.gcs_uri,
            "page_start": chunk.get("page_start"),
            "page_end": None,
            "doc_type": doc.doc_type,
            "kind": chunk.get("kind", "text"),
            "heading_path": chunk.get("section"),
            "last_revised_at": None,
            "pii_flag": doc.chunk_id(index) in pii_chunk_ids,
            "ingested_at": now,
        }
        for index, chunk in enumerate(chunks)
    ]
    errors = bq.insert_rows_json(table, rows, row_ids=[row["chunk_id"] for row in rows])
    if errors:
        raise RuntimeError(f"BigQuery rejected {len(errors)} chunk_source row(s): {errors[0]}")
    return len(rows)
