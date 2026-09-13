"""Embed, upsert to Vector Search, mirror into Firestore - and reuse what a re-issued document kept."""
import os

from google.cloud import aiplatform
from google.cloud import firestore
from google.cloud.aiplatform_v1.types import IndexDatapoint
from google.cloud.firestore_v1.vector import Vector
from google import genai

from contracts import SCHEMA_VERSION

EMBED_BATCH = 250          # the regional API's per-request ceiling, in texts
# ... and in tokens: text-embedding-005 takes at most 20,000 tokens per REQUEST, across all
# the texts in it. Forty 500-token chunks is 20,000; every long Act failed on exactly this
# the first time the corpus was loaded. Tokens are estimated at three characters each -
# an overestimate for English, so a batch stops early rather than late.
EMBED_TOKENS = 15_000
CHARS_PER_TOKEN = 3
DRY_RUN = os.environ.get("VECTOR_DRY_RUN") == "1"
# ONE declared embedding, stamped on every row (12 September 2026): variables.tf's embedding_model and
# embedding_version reach this worker and rag-api through the same two variables, so query and document vectors
# come from one model by construction, and a model change is a planned migration (make reembed, deploy/INDEXING.md)
# rather than a silent mismatch. The carry-over below reuses a vector only when its stamp is this one.
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-005")
EMBEDDING_VERSION = os.environ.get("EMBEDDING_VERSION", "1")

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
            model=EMBEDDING_MODEL, contents=batch,
            config={"output_dimensionality": 768})
        out.extend([e.values for e in r.embeddings])
    return out


# ------------------------------------------------------------------------- the carry-over (12 September 2026)
def held_vectors(db: firestore.Client, doc, chunks_collection: str = "chunks") -> dict[str, list[float]]:
    """The previous version's CURRENT rows of this source, keyed by chunk_hash - only those made with the embedding
    this worker is configured for. Rows older than schema 2 carry no hash and contribute nothing, which is the
    honest outcome: a vector nobody can prove is the same text is embedded again."""
    out: dict[str, list[float]] = {}
    query = (db.collection(chunks_collection).where("tenant_id", "==", doc.tenant_id)
             .where("source_uri", "==", doc.gcs_uri).where("current", "==", True))
    for snap in query.stream():
        d = snap.to_dict() or {}
        h, vec = d.get("chunk_hash"), d.get("embedding")
        if (h and vec is not None and d.get("embedding_model") == EMBEDDING_MODEL
                and str(d.get("embedding_version")) == str(EMBEDDING_VERSION)):
            out[h] = list(vec)
    return out


def plan_carry_over(chunks: list[dict], held: dict[str, list[float]]) -> tuple[list, list[int]]:
    """Pure: for each chunk, the held vector (by chunk_hash) or None; and the positions that need embedding.
    A one-clause edit of a 283-section handbook comes back as 281 hits and two misses - the clause and the
    preamble that now carries the effective date. tools/check_auth_wiring.py runs this offline."""
    vectors, misses = [], []
    for i, c in enumerate(chunks):
        v = held.get(c.get("chunk_hash") or "")
        vectors.append(v)
        if v is None:
            misses.append(i)
    return vectors, misses


def embed_with_carry_over(db: firestore.Client, doc, chunks: list[dict]) -> tuple[list[list[float]], dict]:
    """Pay for what changed. Returns every chunk's vector, and {reused, embedded}: the counts ingest_ok logs, the
    claim records and the ledger row keeps, so a reindex's cost is a number an operator reads, not a bill they
    discover."""
    held = held_vectors(db, doc)
    vectors, misses = plan_carry_over(chunks, held)
    fresh = embed_all([chunks[i]["text"] for i in misses]) if misses else []
    for i, v in zip(misses, fresh):
        vectors[i] = v
    return vectors, {"reused": len(chunks) - len(misses), "embedded": len(misses)}


def _restricts(tenant_id: str, kind: str, doc_type: str) -> list:
    """The four restrict namespaces every datapoint carries, set HERE, at write time - a filter applied only at
    query time is one forgotten WHERE clause away from a leak. rag-api/retriever.py turns each key of a request's
    `filters` into a Namespace of the same name, so a namespace missing here is a filter that matches nothing.

    tenant_id  what makes one index safe for many customers.
    kind       so a caller can ask for figures only (filters={"kind": "figure"} in rag-api's QueryRequest).
    doc_type   the row's doc_type (12 September 2026): filters={"doc_type": "policy"} used to return an empty
               pool, because the namespace was never written. The value is whatever the row carries - `unknown`
               for a text upload the worker did not classify - the same field the Firestore fallback filters on.
    current    the ledger's (12.5): a new version is current; the worker upserts AFTER the swap and removes the
               retired ids, and the query-time restrict is what a reader asks for."""
    return [IndexDatapoint.Restriction(namespace="tenant_id", allow_list=[tenant_id]),
            IndexDatapoint.Restriction(namespace="kind", allow_list=[kind]),
            IndexDatapoint.Restriction(namespace="doc_type", allow_list=[doc_type]),
            IndexDatapoint.Restriction(namespace="current", allow_list=["true"])]


def to_datapoints(doc, chunks: list[dict],
                  vectors: list[list[float]]) -> list[IndexDatapoint]:
    """chunks are dicts - {text, kind, media_url?, page_start?, start?, end?} - since the
    corpus grew figures and video segments (9.6). Only the text is embedded."""
    return [
        IndexDatapoint(
            datapoint_id=doc.chunk_id(i),
            feature_vector=v,
            restricts=_restricts(doc.tenant_id, c.get("kind", "text"), doc.doc_type),
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
    Firestore rows keep the flag and the history, and reupsert() below is how the undo puts them back)."""
    if not ids:
        return
    if DRY_RUN:
        print(f"  [dry-run] would remove {len(ids)} datapoints, ids {ids[0]} .. {ids[-1]}")
        return
    aiplatform.MatchingEngineIndex(index_name).remove_datapoints(datapoint_ids=ids)


def reupsert(index_name: str, db: firestore.Client, tenant_id: str, gcs_uri: str, doc_key: str,
             chunks_collection: str = "chunks") -> int:
    """The undo's half on the full profile (12 September 2026). remove_datapoints() took the retired ids out of the
    ANN tier for good, so an undo that only flipped its Firestore rows left the version current in Firestore and
    absent from Vector Search - retrievable on the lean profile, invisible on the full one. The rows kept their
    vectors (the `embedding` field is the chaos fallback's), so the current rows of the reactivated version go back
    up from there, with the same four restricts to_datapoints() writes, and nothing is embedded. Returns the
    datapoints upserted; the worker calls it BEFORE it retires the newer version, so the tier never holds none."""
    query = (db.collection(chunks_collection).where("tenant_id", "==", tenant_id)
             .where("source_uri", "==", gcs_uri).where("current", "==", True))
    points = []
    for snap in query.stream():
        d = snap.to_dict() or {}
        key = d.get("doc_key") or snap.id.split("#")[0].replace(":", "_", 1)     # idempotency._doc_key_of's rule
        if key != doc_key or d.get("embedding") is None:
            continue
        points.append(IndexDatapoint(datapoint_id=snap.id, feature_vector=list(d["embedding"]),
                                     restricts=_restricts(d.get("tenant_id") or tenant_id, d.get("kind") or "text",
                                                          d.get("doc_type") or "unknown")))
    if points:
        upsert(index_name, points)
    return len(points)


def mirror_to_firestore(db: firestore.Client, doc, chunks: list[dict],
                        vectors: list[list[float]], staged: bool = False, stage_expire_at=None) -> None:
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

    Schema 2 (12 September 2026) adds chunk_hash and locator (the chunk's identity across versions),
    the embedding stamp, schema_version - and, with staged=True, a row that is NOT current yet:
    current=false, staged=true, expire_at a day out. idempotency.swap_versions makes it current in one
    pass and clears the stage marks; a stage nothing ever swaps expires by policy, like a retired row.
    """
    batch, pending = db.batch(), 0
    for i, (c, vec) in enumerate(zip(chunks, vectors)):
        ref = db.collection("chunks").document(doc.chunk_id(i))
        row = {"tenant_id": doc.tenant_id, "text": c["text"],
               "source_uri": doc.gcs_uri, "page_start": c.get("page_start"),
               "doc_type": doc.doc_type, "kind": c.get("kind", "text"),
               # The ledger (11 September 2026): which version this chunk belongs to, that it is the
               # current one, and when it landed. The swap flips `current` on the predecessor.
               "doc_key": doc.doc_key, "current": not staged,
               "indexed_at": firestore.SERVER_TIMESTAMP,
               "chunk_hash": c.get("chunk_hash"), "locator": c.get("locator"),
               "embedding_model": EMBEDDING_MODEL, "embedding_version": EMBEDDING_VERSION,
               "schema_version": SCHEMA_VERSION,
               "embedding": Vector(vec)}
        if c.get("section"):
            row["section"] = c["section"]
        if staged:
            row["staged"] = True
            if stage_expire_at is not None:
                row["expire_at"] = stage_expire_at
        if getattr(doc, "effective_from", None):
            row["effective_from"] = doc.effective_from
        for k in ("media_url", "start", "end"):
            if c.get(k) is not None:
                row[k] = c[k]
        batch.set(ref, row)
        pending += 1
        if pending == 400:                     # a Firestore batch holds 500 writes; a long Act is more
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
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
             "doc_type": doc.doc_type, "kind": c.get("kind", "text"), "heading_path": c.get("section"),
             "last_revised_at": None, "pii_flag": doc.chunk_id(i) in pii_chunk_ids,
             "ingested_at": now} for i, c in enumerate(chunks)]
    errors = bq.insert_rows_json(table, rows, row_ids=[r["chunk_id"] for r in rows])
    if errors:
        raise RuntimeError(f"BigQuery rejected {len(errors)} chunk_source row(s): {errors[0]}")
    return len(rows)
