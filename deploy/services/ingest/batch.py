#!/usr/bin/env python3
"""The batch lane's consumer (13 September 2026): the documents the push path handed off.

    python batch.py --project P                 # drain ingest_batch/: every queued claim, oldest first
    python batch.py --project P --queued        # print the queue and exit (make queued)
    python batch.py --project P --limit 1 --dry-run

A document over MAX_INLINE_PAGES cannot finish inside a push request's 600 seconds, so the worker writes a claim
(`ingest_batch/{doc_key}`, `documents/{doc_key}` = queued) and returns. This is the consumer: a Cloud Run JOB on the
same image (batch.tf, `make batch-job`), with no request deadline, that the worker starts when it queues a document
(BATCH_JOB) and a schedule starts hourly regardless (`make batch` starts it by hand). It takes each queued claim in a
transaction (two runs never index one document twice), downloads the bytes BY GENERATION, and runs the worker's own
pipeline - main.index_document(): media or text, chunks, DLP, the carry-over, the staged write, the swap, the claim,
the ledger row, the fingerprint - with lane="batch", so the page ceiling that queued the document is not consulted
again. A failure leaves the claim `failed` with the error (release) and the batch record says the same; the next run
does not retake it - make reindex, or the same bytes uploaded again, is the way back. A generation that is gone from
the bucket (overwritten, unversioned) is recorded as gone: the newer generation's own event indexes what is there now.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import sys

log = logging.getLogger("documind.ingest")


def queued(db, limit: int | None = None) -> list[dict]:
    """The queue, oldest first: every ingest_batch/ record whose status is queued."""
    rows = []
    for snap in db.collection("ingest_batch").where("status", "==", "queued").stream():
        d = snap.to_dict() or {}
        d["doc_key"] = snap.id
        rows.append(d)
    rows.sort(key=lambda r: (str(r.get("queued_at") or ""), r["doc_key"]))
    return rows[:limit] if limit else rows


def _split(gcs_uri: str) -> tuple[str, str]:
    bucket, _, name = gcs_uri[len("gs://"):].partition("/")
    return bucket, name


def run_one(db, gcs, rec: dict, worker) -> str:
    """One queued claim through the worker's pipeline. Returns indexed | skipped | gone | failed."""
    from google.api_core.exceptions import NotFound
    from contracts import DocumentContract, IngestMessage, sha256_of
    from idempotency import release, take_batch

    doc_key = rec["doc_key"]
    if not take_batch(db, doc_key):
        log.info(json.dumps({"event": "batch_skipped", "doc_key": doc_key, "reason": "the claim is not queued any more"}))
        return "skipped"
    bucket, name = (rec.get("bucket"), rec.get("name"))
    if not bucket or not name:
        bucket, name = _split(rec["gcs_uri"])
    record = db.collection("ingest_batch").document(doc_key)
    try:
        content = gcs.bucket(bucket).blob(name, generation=int(rec["generation"])).download_as_bytes()
    except NotFound:
        release(db, doc_key, "generation gone: the object was overwritten before the batch lane read it")
        record.set({"status": "gone"}, merge=True)
        log.info(json.dumps({"event": "batch_gone", "doc_key": doc_key, "gcs_uri": rec["gcs_uri"],
                             "generation": rec["generation"]}))
        return "gone"
    msg = IngestMessage(bucket=bucket, name=name, size=max(1, int(rec.get("size") or len(content))),
                        contentType=rec.get("content_type") or "application/pdf",
                        generation=str(rec["generation"]), tenant_id=rec["tenant_id"])
    doc = DocumentContract(tenant_id=rec["tenant_id"], sha256=sha256_of(content), gcs_uri=rec["gcs_uri"],
                           pages=int(rec.get("pages") or 0))
    if doc.doc_key != doc_key:
        release(db, doc_key, f"the bytes at generation {rec['generation']} hash to {doc.doc_key}, not this claim")
        record.set({"status": "failed", "error": "content hash does not match the claim"}, merge=True)
        return "failed"
    try:
        out = worker.index_document(doc, msg, content, lane="batch")
    except worker.IngestFailed as e:                   # the claim is released and ingest_failed logged already
        record.set({"status": "failed", "error": str(e)[:300]}, merge=True)
        return "failed"
    record.set({"status": "indexed", "chunks": int(out.get("chunks", 0)),
                "reused": int(out.get("reused", 0)), "embedded": int(out.get("embedded", 0))}, merge=True)
    return "indexed"


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    ap.add_argument("--limit", type=int, help="at most this many documents this run")
    ap.add_argument("--queued", action="store_true", help="print the queue and exit")
    ap.add_argument("--dry-run", action="store_true", help="say what would run; take and index nothing")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    from google.cloud import firestore
    db = firestore.Client(project=args.project)
    q = queued(db, args.limit)
    if args.queued or args.dry_run:
        print(f"{len(q)} queued document(s)" + (" (limit applied)" if args.limit else ""))
        for r in q:
            print(f"  {r['doc_key'][:20]}  {r.get('gcs_uri', '')}  pages={r.get('pages')}  generation={r.get('generation')}")
        return 0
    if not q:
        log.info(json.dumps({"event": "batch_run", "queued": 0, "indexed": 0, "failed": 0}))
        return 0
    from google.cloud import storage
    import main as worker                                # the worker's module: its clients, its pipeline, its env
    gcs = storage.Client(project=args.project)
    tally = {"indexed": 0, "skipped": 0, "gone": 0, "failed": 0}
    for rec in q:
        tally[run_one(db, gcs, rec, worker)] += 1
    log.info(json.dumps({"event": "batch_run", "queued": len(q), **tally}))
    return 1 if tally["failed"] else 0


if __name__ == "__main__":
    sys.exit(main())
