"""The claim: exactly one worker may process a given document - and the ledger: one current version per document."""
import hashlib
import logging

from google.cloud import firestore

log = logging.getLogger("documind.ingest")
BATCH = 400          # a Firestore batch holds 500 writes; commit early


def claim(db: firestore.Client, doc_key: str, gcs_uri: str) -> bool:
    """Claim doc_key. True if THIS caller may proceed, False if someone already did.

    The transaction is the whole point. Read-then-write without one is a race
    with a window measured in milliseconds - and Pub/Sub delivers duplicates
    concurrently, so it is a window that gets hit. Two workers both read
    "absent", both write, and the document is ingested twice.
    """
    ref = db.collection("documents").document(doc_key)

    @firestore.transactional
    def _claim(tx: firestore.Transaction) -> bool:
        snap = ref.get(transaction=tx)
        # A claim that ended in `failed` is not a claim, it is a record of one. release()
        # writes it so the error is readable; the NEXT delivery must be allowed to try
        # again, or "give the claim back" gave nothing back: on the first live load every
        # retry of a failed document was acked as a duplicate and the document stayed failed.
        if snap.exists and snap.get("status") != "failed":
            return False
        tx.set(ref, {"gcs_uri": gcs_uri,
                     "status": "processing",
                     "claimed_at": firestore.SERVER_TIMESTAMP})
        return True

    won = _claim(db.transaction())
    if not won:
        log.info('{"event":"ingest_duplicate","doc_key":"%s"}', doc_key)
    return won


def finish(db: firestore.Client, doc_key: str, chunks: int, counts: dict | None = None,
           generation: str | None = None) -> None:
    """The claim becomes a record: indexed, how many chunks, how many of their vectors were reused by hash and how
    many embedded (12 September 2026), and the object generation the version came from."""
    row = {"status": "indexed", "chunks": chunks, "indexed_at": firestore.SERVER_TIMESTAMP}
    if counts:
        row["reused"], row["embedded"] = int(counts.get("reused", 0)), int(counts.get("embedded", 0))
    if generation is not None:
        row["generation"] = str(generation)
    db.collection("documents").document(doc_key).set(row, merge=True)


def release(db: firestore.Client, doc_key: str, error: str) -> None:
    """Give the claim back so a retry can take it.

    Without this a crash between claim and finish leaves the document claimed
    for ever, and every redelivery sees "already processing" and does nothing.
    The document is then permanently missing and nothing is alerting, because
    from Pub/Sub's point of view every delivery was acked successfully.
    """
    db.collection("documents").document(doc_key).set(
        {"status": "failed", "error": error[:400],
         "failed_at": firestore.SERVER_TIMESTAMP}, merge=True)


# ------------------------------------------------------------------------------ the ledger (11 September 2026)
# A document has an IDENTITY - its object path - and VERSIONS - the content hashes. `documents/{doc_key}` above is
# the per-version claim; `sources/{source_id}` is the per-document ledger: which version is current, its generation,
# when it was indexed, the date it declares. It is what every production indexer keeps (a record manager keyed on the
# source), and it is what lets a re-issued document RETIRE its predecessor's chunks instead of standing beside them.
# Retiring is a flag, never a delete: the audit story survives, the full profile's mirrors keep their history, and a
# bad re-index is undone by uploading the previous bytes again (reactivate) - nothing is re-embedded.
#
# 12 September 2026 (deploy/INDEXING.md): the guard, the swap, the retention and the fingerprint. An event older than
# the ledger's generation is ignored (stale_generation). A new version lands STAGED and invisible, and one pass flips
# it current and retires the old (swap_versions) - a reader never sees two versions of one source. A retired row is
# stamped expire_at; the Firestore TTL policy in firestore_indexes.tf is the only thing that ever deletes a chunk,
# and nothing here calls delete. Every change refreshes the tenant's corpus fingerprint (ledger/{tenant}); the API's
# cache record carries the fingerprint it was packed from and stops being used when they differ.


def source_id_for(tenant_id: str, name: str) -> str:
    """Firestore ids cannot hold '/'; the object path already starts with the tenant prefix."""
    return name.replace("/", "~")


def _doc_key_of(snap) -> str:
    d = snap.to_dict() or {}
    # Chunks written before the ledger carry no doc_key; their id is <tenant>:<sha256>#<i>.
    return d.get("doc_key") or snap.id.split("#")[0].replace(":", "_", 1)


def stale_generation(db: firestore.Client, tenant_id: str, name: str, generation) -> str | None:
    """The generation guard. Returns the ledger's generation when this event's is OLDER than it - a late redelivery
    the worker must ignore - and None when the event is as new as the ledger or newer, or the ledger has no row."""
    snap = db.collection("sources").document(source_id_for(tenant_id, name)).get()
    row = (snap.to_dict() or {}) if snap.exists else {}
    have = row.get("generation")
    try:
        if have and int(str(generation)) < int(str(have)):
            return str(have)
    except (TypeError, ValueError):
        return None
    return None


def _retire(batch_state: list, snap, keep_doc_key, expire_at, effective_to) -> None:
    fields = {"current": False, "superseded_by": keep_doc_key, "superseded_at": firestore.SERVER_TIMESTAMP}
    if expire_at is not None:
        fields["expire_at"] = expire_at            # the TTL policy's field: the platform deletes the row after it
    if effective_to:
        fields["effective_to"] = effective_to      # the successor's effective date closes this version's window
    batch_state[0].update(snap.reference, fields)


def retire_previous(db: firestore.Client, tenant_id: str, gcs_uri: str, keep_doc_key: str | None,
                    chunks_collection: str = "chunks", expire_at=None, effective_to: str | None = None) -> dict:
    """Retire every chunk of `gcs_uri` that is not `keep_doc_key`: current=false, superseded_by, superseded_at, and
    expire_at when a retention is given (the worker passes now + RETENTION_DAYS).

    keep_doc_key=None retires the whole source (reconcile: the object is gone from the bucket). Two equality filters
    need no composite index. Returns the retired keys, ids and count - the worker removes the ids from Vector Search
    on the full profile and logs the count."""
    retired_keys, retired_ids = set(), []
    state, pending = [db.batch()], 0
    query = (db.collection(chunks_collection).where("tenant_id", "==", tenant_id)
             .where("source_uri", "==", gcs_uri))
    for snap in query.stream():
        key = _doc_key_of(snap)
        if (keep_doc_key and key == keep_doc_key) or (snap.to_dict() or {}).get("current") is False:
            continue
        _retire(state, snap, keep_doc_key, expire_at, effective_to)
        retired_keys.add(key)
        retired_ids.append(snap.id)
        pending += 1
        if pending == BATCH:
            state[0].commit()
            state, pending = [db.batch()], 0
    if pending:
        state[0].commit()
    for key in retired_keys:
        db.collection("documents").document(key).set(
            {"status": "superseded", "superseded_by": keep_doc_key,
             "superseded_at": firestore.SERVER_TIMESTAMP}, merge=True)
    return {"retired_doc_keys": sorted(retired_keys), "retired_ids": retired_ids,
            "retired_chunks": len(retired_ids)}


def swap_versions(db: firestore.Client, tenant_id: str, gcs_uri: str, new_doc_key: str, expire_at=None,
                  effective_to: str | None = None, chunks_collection: str = "chunks") -> dict:
    """Visibility is a swap, not a stream (12 September 2026). The new version's rows were written staged
    (current=false, staged=true, a one-day expire_at in case nothing ever swaps them); this flips them current -
    and clears the stage marks - then retires every other current row of the source, in that order, in batches of
    400. A document up to ~250 chunks flips in one commit. A longer one flips in two, and the retriever's
    newest-per-source guard (rag-api/retriever.py) is what makes that window invisible: a reader between the
    commits gets the new version only. Returns the activated count with retire_previous's dict."""
    activated, retired_keys, retired_ids = 0, set(), []
    state, pending = [db.batch()], 0
    query = (db.collection(chunks_collection).where("tenant_id", "==", tenant_id)
             .where("source_uri", "==", gcs_uri))
    rows = list(query.stream())
    for snap in rows:                                       # the new version first: a reader never finds no version
        d = snap.to_dict() or {}
        if _doc_key_of(snap) == new_doc_key and d.get("current") is not True:
            state[0].update(snap.reference, {"current": True, "staged": firestore.DELETE_FIELD,
                                             "expire_at": firestore.DELETE_FIELD,
                                             "superseded_by": firestore.DELETE_FIELD,
                                             "superseded_at": firestore.DELETE_FIELD,
                                             "effective_to": firestore.DELETE_FIELD})
            activated += 1
            pending += 1
            if pending == BATCH:
                state[0].commit()
                state, pending = [db.batch()], 0
    for snap in rows:                                       # then the old: a flag, never a delete
        d = snap.to_dict() or {}
        key = _doc_key_of(snap)
        if key == new_doc_key or d.get("current") is False:
            continue
        _retire(state, snap, new_doc_key, expire_at, effective_to)
        retired_keys.add(key)
        retired_ids.append(snap.id)
        pending += 1
        if pending == BATCH:
            state[0].commit()
            state, pending = [db.batch()], 0
    if pending:
        state[0].commit()
    for key in retired_keys:
        db.collection("documents").document(key).set(
            {"status": "superseded", "superseded_by": new_doc_key,
             "superseded_at": firestore.SERVER_TIMESTAMP}, merge=True)
    return {"activated": activated, "retired_doc_keys": sorted(retired_keys), "retired_ids": retired_ids,
            "retired_chunks": len(retired_ids)}


def reactivate(db: firestore.Client, tenant_id: str, gcs_uri: str, doc_key: str,
               chunks_collection: str = "chunks") -> int:
    """The undo. The same bytes uploaded again after a newer version retired them: their chunks are still here,
    flagged, so flipping the flag back is a re-index that costs nothing. The caller retires the newer version next.
    The retention stamp goes with the flag: a reactivated row is current, and the TTL must not take it."""
    n, batch, pending = 0, db.batch(), 0
    query = (db.collection(chunks_collection).where("tenant_id", "==", tenant_id)
             .where("source_uri", "==", gcs_uri))
    for snap in query.stream():
        if _doc_key_of(snap) == doc_key and (snap.to_dict() or {}).get("current") is False:
            batch.update(snap.reference, {"current": True, "superseded_by": firestore.DELETE_FIELD,
                                          "superseded_at": firestore.DELETE_FIELD,
                                          "expire_at": firestore.DELETE_FIELD,
                                          "effective_to": firestore.DELETE_FIELD,
                                          "reactivated_at": firestore.SERVER_TIMESTAMP})
            n += 1
            pending += 1
            if pending == BATCH:
                batch.commit()
                batch, pending = db.batch(), 0
    if pending:
        batch.commit()
    db.collection("documents").document(doc_key).set(
        {"status": "indexed", "superseded_by": firestore.DELETE_FIELD,
         "reactivated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    return n


def status_of(db: firestore.Client, doc_key: str) -> str | None:
    snap = db.collection("documents").document(doc_key).get()
    return (snap.to_dict() or {}).get("status") if snap.exists else None


def record_source(db: firestore.Client, tenant_id: str, name: str, gcs_uri: str, doc_key: str,
                  generation: str, sha256: str, chunks: int, effective_from: str | None = None,
                  status: str = "indexed", reused: int = 0, embedded: int = 0, retired: int = 0,
                  embedding_model: str | None = None, embedding_version: str | None = None) -> None:
    """The ledger row: what is current for this object path, since when, and what the last reindex cost - the
    chunks whose vectors were reused by hash, the chunks embedded, the rows retired - and the embedding the
    current version was made with."""
    row = {"tenant_id": tenant_id, "name": name, "gcs_uri": gcs_uri, "doc_key": doc_key,
           "generation": str(generation), "sha256": sha256, "chunks": chunks,
           "effective_from": effective_from, "status": status,
           "reused": int(reused), "embedded": int(embedded), "retired": int(retired),
           "indexed_at": firestore.SERVER_TIMESTAMP}
    if embedding_model:
        row["embedding_model"], row["embedding_version"] = embedding_model, str(embedding_version or "")
    db.collection("sources").document(source_id_for(tenant_id, name)).set(row, merge=True)


def corpus_fingerprint(db: firestore.Client, tenant_id: str) -> tuple[str, int]:
    """The hash of the tenant's sorted current doc_keys: the identity of the corpus a cache was packed from. It changes
    on every reindex, retirement and reactivation, and on nothing else."""
    keys = sorted((s.to_dict() or {}).get("doc_key") or "" for s in
                  db.collection("sources").where("tenant_id", "==", tenant_id).where("status", "==", "indexed").stream())
    return hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16], len(keys)


def refresh_fingerprint(db: firestore.Client, tenant_id: str, event: str) -> str:
    """The cache follows the ledger. Every change to what is current re-computes the tenant's fingerprint into
    ledger/{tenant}; rag-api's cache_manager compares it with the fingerprint on the cache record and runs uncached
    when they differ, until make cache packs the corpus that changed. The record is never deleted here - the API
    reads the mismatch, and says so in its log (cache_stale)."""
    fp, n = corpus_fingerprint(db, tenant_id)
    db.collection("ledger").document(tenant_id).set(
        {"tenant_id": tenant_id, "fingerprint": fp, "versions": n, "last_event": event,
         "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    log.info('{"event":"ledger_fingerprint","tenant":"%s","fingerprint":"%s","versions":%d}', tenant_id, fp, n)
    return fp
