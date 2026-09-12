"""Embed, upsert to Vector Search, mirror into Firestore."""
import os

from google.cloud import aiplatform
from google.cloud import firestore
from google.cloud.aiplatform_v1.types import IndexDatapoint
from google.cloud.firestore_v1.vector import Vector
from google import genai

EMBED_BATCH = 250          # the regional API's per-request ceiling, in texts
# ... and in tokens: text-embedding-005 takes at most 20,000 tokens per REQUEST, across all
# the texts in it. Forty 500-token chunks is 20,000; every long Act failed on exactly this
# the first time the corpus was loaded. Tokens are estimated at three characters each -
# an overestimate for English, so a batch stops early rather than late.
EMBED_TOKENS = 15_000
CHARS_PER_TOKEN = 3
DRY_RUN = os.environ.get("VECTOR_DRY_RUN") == "1"

# Embeddings are REGIONAL. Generation is global-only; this client is neither
# interchangeable with that one nor optional to get right.
_embed = genai.Client(enterprise=True,
                      project=os.environ["GOOGLE_CLOUD_PROJECT"],
                      location="us-central1")


def batches(texts: list[str]) -> list[list[str]]:
    """Batches of at most EMBED_BATCH texts and about EMBED_TOKENS tokens. Send 251 texts, or
    20,001 tokens, and the request fails - not the last item, the whole call - so a
    300-chunk document would index nothing at all."""
    out, cur, cur_tokens = [], [], 0
    for t in texts:
        tokens = max(1, len(t) // CHARS_PER_TOKEN)
        if cur and (len(cur) >= EMBED_BATCH or cur_tokens + tokens > EMBED_TOKENS):
            out.append(cur); cur, cur_tokens = [], 0
        cur.append(t); cur_tokens += tokens
    if cur:
        out.append(cur)
    return out


def embed_all(texts: list[str]) -> list[list[float]]:
    out: list[list[float]] = []
    for batch in batches(texts):
        r = _embed.models.embed_content(
            model="text-embedding-005", contents=batch,
            config={"output_dimensionality": 768})
        out.extend([e.values for e in r.embeddings])
    return out


def to_datapoints(doc, chunks: list[dict],
                  vectors: list[list[float]]) -> list[IndexDatapoint]:
    """chunks are dicts - {text, kind, media_url?, page_start?, start?, end?} - since the
    corpus grew figures and video segments (9.6). Only the text is embedded."""
    return [
        IndexDatapoint(
            datapoint_id=doc.chunk_id(i),
            feature_vector=v,
            # The tenant restrict is what makes one index safe for many
            # customers. It is set HERE, at write time - a filter applied only
            # at query time is one forgotten WHERE clause away from a leak.
            # `kind` is a second restrict so a caller can ask for figures only
            # (filters={"kind": "figure"} in rag-api's QueryRequest).
            restricts=[IndexDatapoint.Restriction(
                           namespace="tenant_id", allow_list=[doc.tenant_id]),
                       IndexDatapoint.Restriction(
                           namespace="kind", allow_list=[c.get("kind", "text")]),
                       # The ledger (12.5): a new version is current; retire_previous removes the old
                       # ids, and the query-time restrict is what a reader asks for.
                       IndexDatapoint.Restriction(namespace="current", allow_list=["true"])],
        )
        for i, (c, v) in enumerate(zip(chunks, vectors))
    ]


def upsert(index_name: str, datapoints: list[IndexDatapoint]) -> None:
    if DRY_RUN:
        print(f"  [dry-run] would upsert {len(datapoints)} datapoints, "
              f"ids {datapoints[0].datapoint_id} .. {datapoints[-1].datapoint_id}")
        return
    # Streaming upserts need an index created with STREAM_UPDATE. A BATCH_UPDATE
    # index accepts the call and applies nothing until the next batch job, which
    # looks exactly like a slow index.
    aiplatform.MatchingEngineIndex(index_name).upsert_datapoints(
        datapoints=datapoints)


def remove_datapoints(index_name: str, ids: list[str]) -> None:
    """The full profile's half of retiring a version: the old ids leave the ANN tier (permanent there - the
    Firestore rows keep the flag and the history)."""
    if not ids:
        return
    if DRY_RUN:
        print(f"  [dry-run] would remove {len(ids)} datapoints, ids {ids[0]} .. {ids[-1]}")
        return
    aiplatform.MatchingEngineIndex(index_name).remove_datapoints(datapoint_ids=ids)


def mirror_to_firestore(db: firestore.Client, doc, chunks: list[dict],
                        vectors: list[list[float]]) -> None:
    """The payload store, and the chaos fallback.

    Vector Search holds the vectors; Firestore holds the text the model quotes.
    Storing the embedding here TOO, as a Vector field, means find_nearest() can
    answer while Vector Search is unavailable - slower and good enough, instead
    of an outage.

    The document is THE canonical shape (2.3, 4.2, 4.5, rag-api): tenant_id, text,
    source_uri, page_start, doc_type, embedding - plus, for a figure or a video
    segment, kind / media_url / start / end. resolve() in shared/documind_schemas.py
    reads exactly these names into a Citation, so what is written here is what the
    frontend renders as a thumbnail or a timestamp (gap G7). A text chunk carries
    kind="text" and nothing else new, so nothing written before Module 9 changes.
    """
    batch = db.batch()
    for i, (c, vec) in enumerate(zip(chunks, vectors)):
        ref = db.collection("chunks").document(doc.chunk_id(i))
        row = {"tenant_id": doc.tenant_id, "text": c["text"],
               "source_uri": doc.gcs_uri, "page_start": c.get("page_start"),
               "doc_type": doc.doc_type, "kind": c.get("kind", "text"),
               # The ledger (11 September 2026): which version this chunk belongs to, that it is the
               # current one, and when it landed. retire_previous() flips `current` on the predecessor.
               "doc_key": doc.doc_key, "current": True,
               "indexed_at": firestore.SERVER_TIMESTAMP,
               "embedding": Vector(vec)}
        if getattr(doc, "effective_from", None):
            row["effective_from"] = doc.effective_from
        for k in ("media_url", "start", "end"):
            if c.get(k) is not None:
                row[k] = c[k]
        batch.set(ref, row)
    batch.commit()


def mirror_to_bigquery(bq, table: str, doc, chunks: list[dict], pii_chunk_ids: set) -> int:
    """The SQL lane's copy of the REAL chunks (lesson 5.5, gap G9).

    One row per chunk into rag_data.chunk_source - the same canonical names, plus the DLP
    verdict this worker already computed, so 5.5's feature job needs no second scan and no
    BigQuery model to know pii_flag. insertId = chunk id: a retried message re-inserts the
    same rows and BigQuery de-duplicates them. `table` is BQ_CHUNK_TABLE
    (PROJECT.rag_data.chunk_source); unset means the lane is off and nothing is written.
    """
    if not table:
        return 0
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    rows = [{"chunk_id": doc.chunk_id(i), "tenant_id": doc.tenant_id, "text": c["text"],
             "source_uri": doc.gcs_uri, "page_start": c.get("page_start"), "page_end": None,
             "doc_type": doc.doc_type, "kind": c.get("kind", "text"), "heading_path": None,
             "last_revised_at": None, "pii_flag": doc.chunk_id(i) in pii_chunk_ids,
             "ingested_at": now} for i, c in enumerate(chunks)]
    errors = bq.insert_rows_json(table, rows, row_ids=[r["chunk_id"] for r in rows])
    if errors:
        raise RuntimeError(f"BigQuery rejected {len(errors)} chunk_source row(s): {errors[0]}")
    return len(rows)
