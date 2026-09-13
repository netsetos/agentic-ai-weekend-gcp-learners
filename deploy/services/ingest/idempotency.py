"""The claim: exactly one worker may process a given document - and the ledger: one current version per document."""
import hashlib
import logging
import os
from datetime import datetime, timezone

from google.cloud import firestore

log = logging.getLogger("documind.ingest")
BATCH = 400          # a Firestore batch holds 500 writes; commit early
# The undo window (12 September 2026). The worker stamps expire_at = now + RETENTION_DAYS on a retired row and the TTL
# policy deletes it after that, so an undo older than this cannot find every row. Read the way the worker reads it -
# variables.tf's retention_days, passed by make deploy-services - so the two never disagree.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))


def claim(db: firestore.Client, doc_key: str, gcs_uri: str, tenant_id: str, retake: bool = False) -> bool:
    """Claim doc_key. True if THIS caller may proceed, False if someone already did.

    The transaction is the whole point. Read-then-write without one is a race
    with a window measured in milliseconds - and Pub/Sub delivers duplicates
    concurrently, so it is a window that gets hit. Two workers both read
    "absent", both write, and the document is ingested twice.

    The row carries tenant_id (12 September 2026): the MCP server's list_documents filters on the field, never on
    the id's prefix - `acme` must not list `acme_eu_*`. retake=True takes a `superseded` claim back: the worker asks
    for it after reactivate() refused the undo, so the bytes are ingested as a fresh version instead of being acked
    as a duplicate for ever. A `queued` claim belongs to the batch lane and is never retaken.
    """
    ref = db.collection("documents").document(doc_key)
    retakes = ("failed", "superseded") if retake else ("failed",)

    @firestore.transactional
    def _claim(tx: firestore.Transaction) -> bool:
        snap = ref.get(transaction=tx)
        # A claim that ended in `failed` is not a claim, it is a record of one. release()
        # writes it so the error is readable; the NEXT delivery must be allowed to try
        # again, or "give the claim back" gave nothing back: on the first live load every
        # retry of a failed document was acked as a duplicate and the document stayed failed.
        if snap.exists and snap.get("status") not in retakes:
            return False
        tx.set(ref, {"gcs_uri": gcs_uri,
                     "tenant_id": tenant_id,
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
#
# The tombstone and the verified undo (12 September 2026, the R05 findings). A source a person retires by hand
# (make retire) is `withdrawn` in sources/ - distinct from `retired` (its object left the bucket) and from `superseded`
# (a re-issue) - and nothing automatic brings it back: reconcile leaves the object where it is, the same bytes again
# are acked, reactivate() refuses; make restore SOURCE= clears it. And reactivate() counts before it flips: the rows
# still here against the claim's chunk count, the retire stamp (retired_at on documents/{doc_key}) against
# RETENTION_DAYS. A shortfall or a closed window is reactivate_incomplete, and the worker re-ingests the bytes instead
# of retiring the newer version over a partial undo.


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
             "superseded_at": firestore.SERVER_TIMESTAMP,
             "retired_at": firestore.SERVER_TIMESTAMP}, merge=True)     # the undo window's clock (reactivate)
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
             "superseded_at": firestore.SERVER_TIMESTAMP,
             "retired_at": firestore.SERVER_TIMESTAMP}, merge=True)     # the undo window's clock (reactivate)
    return {"activated": activated, "retired_doc_keys": sorted(retired_keys), "retired_ids": retired_ids,
            "retired_chunks": len(retired_ids)}


def _name_of(gcs_uri: str) -> str:
    """gs://bucket/acme/x.md -> acme/x.md: the object name the ledger keys on (source_id_for)."""
    return gcs_uri.split("/", 3)[3] if gcs_uri.startswith("gs://") and gcs_uri.count("/") >= 3 else gcs_uri


def withdrawn(db: firestore.Client, tenant_id: str, gcs_uri: str) -> bool:
    """The tombstone (12 September 2026): a person retired this source by hand (make retire), and only make restore
    may bring it back. The worker asks before the undo; reactivate() asks again, so no caller can skip it."""
    snap = db.collection("sources").document(source_id_for(tenant_id, _name_of(gcs_uri))).get()
    return bool(snap.exists and (snap.to_dict() or {}).get("status") == "withdrawn")


def reactivate(db: firestore.Client, tenant_id: str, gcs_uri: str, doc_key: str,
               chunks_collection: str = "chunks", retention_days: int | None = None) -> int | None:
    """The undo. The same bytes uploaded again after a newer version retired them: their chunks are still here,
    flagged, so flipping the flag back is a re-index that costs nothing. The caller retires the newer version next.
    The retention stamp goes with the flag: a reactivated row is current, and the TTL must not take it.

    Verified before anything is flipped (12 September 2026). The first undo flipped whatever rows remained and
    the worker retired the newer version on the strength of it - after RETENTION_DAYS the TTL policy had taken the
    rows, zero were flipped, and the source was left with no current version at all. So the rows still here are
    counted against the claim's chunk count, and the retire stamp against RETENTION_DAYS; a shortfall or a closed
    window is `reactivate_incomplete` with both numbers, None comes back, and nothing has changed - the worker
    re-ingests the bytes instead. A withdrawn source is refused the same way (`reactivate_withdrawn`). Otherwise
    the rows flipped, as a count, with the claim's stamps cleared."""
    if withdrawn(db, tenant_id, gcs_uri):
        log.info('{"event":"reactivate_withdrawn","doc_key":"%s","gcs_uri":"%s","hint":"make restore SOURCE="}',
                 doc_key, gcs_uri)
        return None
    query = (db.collection(chunks_collection).where("tenant_id", "==", tenant_id)
             .where("source_uri", "==", gcs_uri))
    rows = [snap for snap in query.stream()
            if _doc_key_of(snap) == doc_key and (snap.to_dict() or {}).get("current") is False]
    claim_snap = db.collection("documents").document(doc_key).get()
    record = (claim_snap.to_dict() or {}) if claim_snap.exists else {}
    expected, stamp = record.get("chunks"), record.get("retired_at")
    window = RETENTION_DAYS if retention_days is None else int(retention_days)
    age = None
    if isinstance(stamp, datetime):                     # a claim from before the stamp has no clock: the count decides
        age = (datetime.now(timezone.utc) - (stamp if stamp.tzinfo else stamp.replace(tzinfo=timezone.utc))).days
    short = expected is not None and len(rows) < int(expected)
    if short or (age is not None and age > window):
        log.warning('{"event":"reactivate_incomplete","doc_key":"%s","rows":%d,"chunks":%s,"age_days":%s,'
                    '"retention_days":%d,"reason":"%s"}', doc_key, len(rows),
                    "null" if expected is None else int(expected), "null" if age is None else age, window,
                    "fewer rows than the claim counted" if short else "retired longer ago than the undo window")
        return None
    n, batch, pending = 0, db.batch(), 0
    for snap in rows:
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
        {"status": "indexed", "superseded_by": firestore.DELETE_FIELD, "retired_at": firestore.DELETE_FIELD,
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
