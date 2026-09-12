"""The claim: exactly one worker may process a given document - and the ledger: one current version per document."""
import logging

from google.cloud import firestore

log = logging.getLogger("documind.ingest")


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


def finish(db: firestore.Client, doc_key: str, chunks: int) -> None:
    db.collection("documents").document(doc_key).set(
        {"status": "indexed", "chunks": chunks,
         "indexed_at": firestore.SERVER_TIMESTAMP}, merge=True)


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


def source_id_for(tenant_id: str, name: str) -> str:
    """Firestore ids cannot hold '/'; the object path already starts with the tenant prefix."""
    return name.replace("/", "~")


def _doc_key_of(snap) -> str:
    d = snap.to_dict() or {}
    # Chunks written before the ledger carry no doc_key; their id is <tenant>:<sha256>#<i>.
    return d.get("doc_key") or snap.id.split("#")[0].replace(":", "_", 1)


def retire_previous(db: firestore.Client, tenant_id: str, gcs_uri: str, keep_doc_key: str | None,
                    chunks_collection: str = "chunks") -> dict:
    """Retire every chunk of `gcs_uri` that is not `keep_doc_key`: current=false, superseded_by, superseded_at.

    Runs AFTER the new version's chunks are written, so a reader never sees a document with no current version.
    keep_doc_key=None retires the whole source (reconcile: the object is gone from the bucket). Two equality filters
    need no composite index. Returns the retired keys, ids and count - the worker removes the ids from Vector Search
    on the full profile and logs the count."""
    retired_keys, retired_ids = set(), []
    batch, pending = db.batch(), 0
    query = (db.collection(chunks_collection).where("tenant_id", "==", tenant_id)
             .where("source_uri", "==", gcs_uri))
    for snap in query.stream():
        key = _doc_key_of(snap)
        if (keep_doc_key and key == keep_doc_key) or (snap.to_dict() or {}).get("current") is False:
            continue
        batch.update(snap.reference, {"current": False, "superseded_by": keep_doc_key,
                                      "superseded_at": firestore.SERVER_TIMESTAMP})
        retired_keys.add(key)
        retired_ids.append(snap.id)
        pending += 1
        if pending == 400:
            batch.commit()
            batch, pending = db.batch(), 0
    if pending:
        batch.commit()
    for key in retired_keys:
        db.collection("documents").document(key).set(
            {"status": "superseded", "superseded_by": keep_doc_key,
             "superseded_at": firestore.SERVER_TIMESTAMP}, merge=True)
    return {"retired_doc_keys": sorted(retired_keys), "retired_ids": retired_ids,
            "retired_chunks": len(retired_ids)}


def reactivate(db: firestore.Client, tenant_id: str, gcs_uri: str, doc_key: str,
               chunks_collection: str = "chunks") -> int:
    """The undo. The same bytes uploaded again after a newer version retired them: their chunks are still here,
    flagged, so flipping the flag back is a re-index that costs nothing. The caller retires the newer version next."""
    n, batch, pending = 0, db.batch(), 0
    query = (db.collection(chunks_collection).where("tenant_id", "==", tenant_id)
             .where("source_uri", "==", gcs_uri))
    for snap in query.stream():
        if _doc_key_of(snap) == doc_key and (snap.to_dict() or {}).get("current") is False:
            batch.update(snap.reference, {"current": True, "superseded_by": firestore.DELETE_FIELD,
                                          "superseded_at": firestore.DELETE_FIELD,
                                          "reactivated_at": firestore.SERVER_TIMESTAMP})
            n += 1
            pending += 1
            if pending == 400:
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
                  status: str = "indexed") -> None:
    """The ledger row: what is current for this object path, and since when."""
    db.collection("sources").document(source_id_for(tenant_id, name)).set(
        {"tenant_id": tenant_id, "name": name, "gcs_uri": gcs_uri, "doc_key": doc_key,
         "generation": str(generation), "sha256": sha256, "chunks": chunks,
         "effective_from": effective_from, "status": status,
         "indexed_at": firestore.SERVER_TIMESTAMP}, merge=True)


def drop_tenant_cache(db: firestore.Client, tenant_id: str) -> bool:
    """The cache follows the index. The worker cannot delete the Vertex cache object (that is the API's
    permission) - it deletes the RECORD the API reads, so the next answer runs uncached and `make cache` rebuilds
    the pack from the corpus that changed. Returns whether there was a record to drop."""
    ref = db.collection("tenant_caches").document(tenant_id)
    if not ref.get().exists:
        return False
    ref.delete()
    log.info('{"event":"cache_record_dropped","tenant":"%s"}', tenant_id)
    return True
