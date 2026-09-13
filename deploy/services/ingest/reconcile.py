"""The ledger's full reconciliation (12.5, 11 September 2026): the bucket against sources/.

    python reconcile.py --project P                 # the plan: what would be retired, re-ingested, backfilled
    python reconcile.py --project P --apply         # do it; the last line carries the DRIFT the night measured
    python reconcile.py --project P --backfill --apply    # chunks and documents written before the ledger get
                                                          # current=true, doc_key, and a sources/ row each
    python reconcile.py --project P --retire gs://P-uploads/acme/old.pdf --apply     # withdraw by hand: a tombstone
    python reconcile.py --project P --restore gs://P-uploads/acme/old.pdf --apply    # and the way back
    python reconcile.py --project P --report [--tenant acme] [--json]   # the versions view: every source's current
                                                          # version, generation, counts, dates, the corpus fingerprint
    python reconcile.py --project P --purge [--apply]     # the manual twin of the TTL policy: retired rows past expire_at
    python reconcile.py --selftest                  # the planning logic, offline

The worker handles the incremental case on every object.finalized. This is the other half every production
indexer has (LangChain's `full` cleanup, Vertex AI Search's FULL reconciliation, Bedrock's sync): objects gone
from the bucket are RETIRED (flagged, not deleted, stamped expire_at), objects whose current generation is newer
than the ledger's are re-ingested by rewriting them onto themselves - a new generation fires the same finalize
event the worker already handles, so there is one ingestion path, not two - and objects the ledger never saw are
either backfilled from documents/ (same bytes, already indexed) or ingested the same way. Deletions need no
trigger. The walk ends with one number, drift: how far the ledger stood from the bucket when it started
(alerts.tf makes it a metric; the policy pages when it stays above zero for two nights). `make reconcile` runs
it from a shell; reconcile.tf declares the Cloud Run job on the ingest image and schedules it nightly beside
documind-off. The TTL policy in firestore_indexes.tf is the only deleter on the lane; --purge exists for a lane
that has not applied it, prints by default, and says so.

Two states the walk leaves alone (12 September 2026). A source a person retired by hand (--retire, make retire)
is `withdrawn`: its object is kept, and the first version of this planner read "retired in the ledger but back in
the bucket" and re-ingested it the same night - the worker's undo brought back what a person had just taken
down. Now the plan says "withdrawn, object kept" and does nothing; --restore (make restore) clears the tombstone
and rewrites the object so the worker's own path brings it back. And a document the worker handed to the batch
lane (`queued` on documents/, the batch job's to take) is reported as queued, not hashed and not rewritten every night.
Neither counts as drift.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

SKIP_SUFFIXES = (".segments.json",)     # ground truth for 9.4's diarisation cell, not a document (evals/upload.sh)
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))


def source_id_for(tenant_id: str, name: str) -> str:
    """idempotency.source_id_for, repeated so the planning logic runs without the cloud clients."""
    return name.replace("/", "~")


def _name_of(gcs_uri: str) -> str:
    """gs://bucket/acme/x.md -> acme/x.md (idempotency._name_of, repeated for the same reason)."""
    return gcs_uri.split("/", 3)[3] if gcs_uri.startswith("gs://") and gcs_uri.count("/") >= 3 else gcs_uri


def plan(objects: list[dict], ledger: dict[str, dict], documents: dict[str, dict]) -> list[dict]:
    """Pure: what to do for each object and each ledger row.

    objects:   [{name, generation, tenant_id}] - the bucket's CURRENT generations (one per name)
    ledger:    {source_id: {gcs_uri, doc_key, generation, sha256, status}}
    documents: {doc_key: {status, gcs_uri, generation?}} - the per-version claims
    Returns actions: retire | reingest | backfill | check_bytes | ok - and two that act on nothing (12 September
    2026): `withdrawn`, a source a person retired by hand, whose object stays where it is until make restore; and
    `queued`, a generation the worker handed to the batch lane (the batch job indexes it, batch.py), which must not be
    hashed or rewritten onto itself every night. Neither is drift."""
    actions = []
    seen = set()
    # The batch lane's claims, by object name and generation: a later generation of the same name is a new
    # version and is planned as usual, so a queued claim never hides what came after it.
    queued = {(_name_of(d.get("gcs_uri") or ""), str(d.get("generation") or ""))
              for d in documents.values() if d.get("status") == "queued"}
    for o in objects:
        if o["name"].endswith(SKIP_SUFFIXES) or "/" not in o["name"]:
            continue
        sid = source_id_for(o["tenant_id"], o["name"])
        seen.add(sid)
        row = ledger.get(sid)
        if (o["name"], str(o["generation"])) in queued or (o["name"], "") in queued:
            actions.append({"action": "queued", "name": o["name"], "tenant_id": o["tenant_id"],
                            "generation": o["generation"],
                            "why": "handed to the batch lane; the batch job indexes it, the claim says queued"})
        elif row is None:
            # Never in the ledger. Same bytes may already be indexed (a lane older than the ledger): that is a
            # backfill, decided once the bytes are hashed; otherwise ingest it through the normal path.
            actions.append({"action": "check_bytes", "name": o["name"], "tenant_id": o["tenant_id"],
                            "generation": o["generation"], "why": "not in the ledger"})
        elif row.get("status") == "withdrawn":
            # The tombstone. The object is kept on purpose; nothing here rewrites it - make restore does.
            moved = str(row.get("generation")) != str(o["generation"])
            actions.append({"action": "withdrawn", "name": o["name"], "tenant_id": o["tenant_id"],
                            "generation": o["generation"],
                            "why": "withdrawn, object kept" + (f" (generation {row.get('generation')} -> {o['generation']}: "
                                                              "make restore re-ingests what the object holds now)" if moved else "")})
        elif row.get("status") == "retired":
            actions.append({"action": "reingest", "name": o["name"], "tenant_id": o["tenant_id"],
                            "generation": o["generation"], "why": "retired in the ledger but back in the bucket"})
        elif str(row.get("generation")) != str(o["generation"]):
            actions.append({"action": "check_bytes", "name": o["name"], "tenant_id": o["tenant_id"],
                            "generation": o["generation"], "why": f"generation {row.get('generation')} -> {o['generation']}"})
        else:
            actions.append({"action": "ok", "name": o["name"], "tenant_id": o["tenant_id"]})
    for sid, row in ledger.items():
        if sid in seen or row.get("status") == "retired":
            continue
        if row.get("status") == "withdrawn":
            # Its rows were retired when it was withdrawn; the object leaving changes nothing but the way back.
            actions.append({"action": "withdrawn", "name": row.get("name") or sid.replace("~", "/"),
                            "tenant_id": row.get("tenant_id"), "gcs_uri": row.get("gcs_uri"),
                            "why": "withdrawn, object gone: make restore needs the bytes uploaded again"})
            continue
        actions.append({"action": "retire", "name": row.get("name") or sid.replace("~", "/"),
                        "tenant_id": row.get("tenant_id"), "gcs_uri": row.get("gcs_uri"),
                        "why": "gone from the bucket"})
    return actions


def decide_bytes(sha: str, tenant_id: str, ledger_row: dict | None, documents: dict[str, dict]) -> str:
    """After hashing an object: backfill (already indexed under this sha), skip (metadata-only change), queued
    (the batch lane holds this version) or reingest."""
    key = f"{tenant_id}_{sha}"
    if ledger_row and ledger_row.get("sha256") == sha:
        return "touch"          # the bytes did not change: record the new generation, nothing to index
    status = documents.get(key, {}).get("status")
    if status in ("indexed", "superseded"):
        return "backfill"       # the version exists; the ledger just never heard of it
    if status == "queued":
        return "queued"         # the worker handed these bytes to the batch lane; a rewrite would only re-queue them
    return "reingest"


def drift_of(summary: dict) -> int:
    """The night's number (12 September 2026): how far the ledger stood from the bucket at the walk. A source gone
    from the bucket (retire), one whose bytes changed under a lost event (reingest), one the ledger never saw
    (backfill, or reingest) count one each; a metadata-only change (touch) does not - the content was right.
    reconcile_done carries it, alerts.tf turns it into a metric, and the policy pages when it stays above zero
    for two runs: one night's drift is a lost event, two nights' is a lane nobody is reconciling. A withdrawn
    source and a queued document are decisions, not drift; they are counted beside it."""
    return int(summary.get("retire", 0)) + int(summary.get("reingest", 0)) + int(summary.get("backfill", 0))


def expired(rows: list[dict], now) -> list[dict]:
    """Pure: the retired rows whose expire_at has passed - what the TTL policy deletes on its own within about
    a day of the stamp. A current row is never in the list, whatever its stamp says."""
    return [r for r in rows
            if r.get("current") is False and r.get("expire_at") is not None and r["expire_at"] <= now]


def selftest() -> int:
    from contracts import is_stale
    objs = [{"name": "acme/a.md", "generation": "2", "tenant_id": "acme"},
            {"name": "acme/b.md", "generation": "7", "tenant_id": "acme"},
            {"name": "acme/new.pdf", "generation": "1", "tenant_id": "acme"},
            {"name": "acme/x.segments.json", "generation": "1", "tenant_id": "acme"}]
    ledger = {"acme~a.md": {"gcs_uri": "gs://b/acme/a.md", "doc_key": "acme_s1", "generation": "2", "sha256": "s1", "status": "indexed"},
              "acme~b.md": {"gcs_uri": "gs://b/acme/b.md", "doc_key": "acme_s2", "generation": "5", "sha256": "s2", "status": "indexed"},
              "acme~gone.md": {"gcs_uri": "gs://b/acme/gone.md", "doc_key": "acme_s3", "generation": "1", "sha256": "s3", "status": "indexed", "name": "acme/gone.md", "tenant_id": "acme"}}
    docs = {"acme_s9": {"status": "indexed", "gcs_uri": "gs://b/acme/new.pdf"}}
    acts = {(a["action"], a["name"]) for a in plan(objs, ledger, docs)}
    assert ("ok", "acme/a.md") in acts, acts
    assert ("check_bytes", "acme/b.md") in acts and ("check_bytes", "acme/new.pdf") in acts, acts
    assert ("retire", "acme/gone.md") in acts and not any(n.endswith(".segments.json") for _, n in acts), acts
    assert decide_bytes("s2", "acme", ledger["acme~b.md"], docs) == "touch"
    assert decide_bytes("s9", "acme", None, docs) == "backfill"
    assert decide_bytes("s8", "acme", ledger["acme~b.md"], docs) == "reingest"
    # the generation guard the worker applies before it downloads anything (contracts.is_stale)
    assert is_stale("5", "7") and is_stale(5, "7"), "an older generation is a late redelivery"
    assert not is_stale("7", "7") and not is_stale("9", "7"), "the same or a newer generation is never stale"
    assert not is_stale("x", "7") and not is_stale("5", None) and not is_stale("5", ""), "no ledger, nothing stale"
    # the drift line, and the purge plan
    assert drift_of({"retire": 1, "reingest": 2, "backfill": 1, "touch": 3, "ok": 40}) == 4 and drift_of({"ok": 5}) == 0
    now = datetime(2026, 10, 12, tzinfo=timezone.utc)
    rows = [{"id": "a", "current": False, "expire_at": now - timedelta(days=1)},
            {"id": "b", "current": False, "expire_at": now + timedelta(days=1)},
            {"id": "c", "current": True, "expire_at": now - timedelta(days=1)},
            {"id": "d", "current": False}]
    assert [r["id"] for r in expired(rows, now)] == ["a"], "only a retired row past its stamp expires"
    # the tombstone and the batch lane (12 September 2026): neither is re-ingested, hashed or retired by the walk
    ledger["acme~kept.md"] = {"gcs_uri": "gs://b/acme/kept.md", "doc_key": "acme_s4", "generation": "4", "sha256": "s4",
                              "status": "withdrawn", "name": "acme/kept.md", "tenant_id": "acme"}
    ledger["acme~left.md"] = {"gcs_uri": "gs://b/acme/left.md", "doc_key": "acme_s5", "generation": "1", "sha256": "s5",
                              "status": "withdrawn", "name": "acme/left.md", "tenant_id": "acme"}
    objs += [{"name": "acme/kept.md", "generation": "4", "tenant_id": "acme"},
             {"name": "acme/big.pdf", "generation": "2", "tenant_id": "acme"}]
    docs["acme_s7"] = {"status": "queued", "gcs_uri": "gs://b/acme/big.pdf", "generation": "2"}
    acts = [(a["action"], a["name"]) for a in plan(objs, ledger, docs)]
    assert ("withdrawn", "acme/kept.md") in acts and ("reingest", "acme/kept.md") not in acts, acts
    assert ("retire", "acme/left.md") not in acts and ("withdrawn", "acme/left.md") in acts, acts
    assert ("queued", "acme/big.pdf") in acts and ("check_bytes", "acme/big.pdf") not in acts, acts
    assert decide_bytes("s7", "acme", None, docs) == "queued"
    later = plan([{"name": "acme/big.pdf", "generation": "3", "tenant_id": "acme"}], ledger, docs)
    assert later[0]["action"] == "check_bytes", "a later generation of a queued name is a new version"
    assert drift_of({"retire": 1, "withdrawn": 2, "queued": 1}) == 1, "a withdrawn source and a queued document are not drift"
    print("selftest OK: a retired source, two byte checks, an untouched one, the diarisation file skipped; "
          "touch / backfill / reingest decided from the hash; the generation guard (5 < 7 stale, 7 and 9 not); "
          "drift 4 of {retire 1, reingest 2, backfill 1, touch 3}; one expired row of four; a withdrawn source kept "
          "and one gone, neither re-ingested nor retired; a queued document skipped until its next generation")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project")
    ap.add_argument("--bucket", help="default <project>-uploads")
    ap.add_argument("--tenant", help="one tenant prefix only")
    ap.add_argument("--apply", action="store_true", help="act; the default prints the plan")
    ap.add_argument("--backfill", action="store_true", help="chunks and documents written before the ledger")
    ap.add_argument("--retire", help="a gs:// URI (or tenant/name) to withdraw by hand: a tombstone the walk honours")
    ap.add_argument("--restore", help="a gs:// URI (or tenant/name) withdrawn by --retire: clear the tombstone, re-ingest")
    ap.add_argument("--report", action="store_true", help="the versions view: sources/ and the corpus fingerprint")
    ap.add_argument("--purge", action="store_true", help="retired rows past expire_at (the TTL policy's manual twin)")
    ap.add_argument("--json", action="store_true", help="--report as JSON lines")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.project:
        ap.error("--project is required")
    # The cloud clients and the worker's modules are imported here, not at the top: the selftest and the
    # gate run the planning logic on a machine with neither.
    from google.cloud import firestore, storage
    from contracts import sha256_of
    from idempotency import record_source, refresh_fingerprint, retire_previous
    from managed import Mirror
    db = firestore.Client(project=a.project)
    # The managed mirror (P9.2): a version this walk retires leaves the tenant's managed stores too. MANAGED_MIRROR
    # and RESIDENCY come from the shell (make reconcile / make retire) or the job's env (reconcile.tf); off is silent.
    mirror = Mirror.from_env(db, os.environ.get("MANAGED_MIRROR", "off"), os.environ.get("RESIDENCY", "us"))
    gcs = storage.Client(project=a.project)
    bucket_name = a.bucket or f"{a.project}-uploads"
    bucket = gcs.bucket(bucket_name)
    expire_at = datetime.now(timezone.utc) + timedelta(days=RETENTION_DAYS)

    if a.report:
        # THE VERSIONS VIEW: what the API serves as GET /v1/sources and the UI's Documents page shows.
        q = db.collection("sources")
        if a.tenant:
            q = q.where("tenant_id", "==", a.tenant)
        rows = sorted(((s.to_dict() or {}) for s in q.stream()),
                      key=lambda r: (r.get("tenant_id") or "", r.get("name") or ""))
        ledgers = {l.id: (l.to_dict() or {}) for l in db.collection("ledger").stream()
                   if not a.tenant or l.id == a.tenant}
        if a.json:
            for r in rows:
                r = {k: (v.isoformat() if hasattr(v, "isoformat") else v) for k, v in r.items()}
                print(json.dumps(r))
        else:
            print(f"{'source':44} {'status':10} {'gen':>16} {'chunks':>6} {'reused':>6} {'embed':>5} {'retired':>7} "
                  f"{'effective':10} {'embedding':22} indexed_at")
            for r in rows:
                at = r.get("indexed_at")
                print(f"{(r.get('name') or '')[:44]:44} {(r.get('status') or '')[:10]:10} {str(r.get('generation') or '')[-16:]:>16} "
                      f"{r.get('chunks') or 0:>6} {r.get('reused') if r.get('reused') is not None else '-':>6} "
                      f"{r.get('embedded') if r.get('embedded') is not None else '-':>5} "
                      f"{r.get('retired') if r.get('retired') is not None else '-':>7} {(r.get('effective_from') or '-'):10} "
                      f"{(r.get('embedding_model') or '-') + '@' + str(r.get('embedding_version') or '-'):22} "
                      f"{at.isoformat()[:19] if hasattr(at, 'isoformat') else '-'}")
        for t, l in sorted(ledgers.items()):
            print(json.dumps({"ledger": t, "fingerprint": l.get("fingerprint"), "versions": l.get("versions"),
                              "last_event": l.get("last_event")}))
        return 0

    if a.purge:
        # The manual twin of the TTL policy: what firestore_indexes.tf's policy deletes on its own within a day of
        # expire_at. Prints the plan; --apply deletes. On a lane with the policy applied this finds nothing to do,
        # which is the point of running it once: the platform got there first.
        now = datetime.now(timezone.utc)
        q = db.collection("chunks").where("current", "==", False)
        if a.tenant:
            q = q.where("tenant_id", "==", a.tenant)
        q = q.select(["tenant_id", "source_uri", "current", "expire_at"])
        rows = [dict(snap.to_dict() or {}, ref=snap.reference) for snap in q.stream()]
        due = expired(rows, now)
        for uri, n in sorted(Counter(r.get("source_uri") for r in due).items()):
            print(json.dumps({"purge": uri, "rows": n}))
        if a.apply and due:
            batch, pending = db.batch(), 0
            for r in due:
                batch.delete(r["ref"])
                pending += 1
                if pending == 400:
                    batch.commit()
                    batch, pending = db.batch(), 0
            if pending:
                batch.commit()
        print(json.dumps({"event": "reconcile_purged" if a.apply else "reconcile_purge_plan", "expired": len(due),
                          "retired": len(rows), "applied": a.apply,
                          "note": "the TTL policy on chunks.expire_at (firestore_indexes.tf) deletes these on its own within a day; "
                                  "this is the manual twin for a lane that has not applied it"}))
        return 0

    if a.retire:
        # WITHDRAWAL IS A TOMBSTONE (12 September 2026). The rows are retired as for any retirement - flagged,
        # stamped, never deleted - but the ledger row says `withdrawn`, not `retired`: `retired` means the object
        # left the bucket and an object that comes back is re-ingested, which for a hand retirement was exactly
        # wrong - the object never left, and the next night's walk resurrected what a person had taken down.
        # A withdrawn source is left alone by the walk, acked by the worker, refused by the undo; --restore ends it.
        uri = a.retire if a.retire.startswith("gs://") else f"gs://{bucket_name}/{a.retire}"
        name = uri.split(f"gs://{bucket_name}/", 1)[-1]
        tenant = name.split("/", 1)[0]
        if not a.apply:
            print(f"would withdraw {uri}: every current chunk retired (the rows expire {RETENTION_DAYS} days later), the ledger "
                  f"row withdrawn - the object stays, the night's walk leaves it, make restore SOURCE= brings it back (--apply to do it)")
            return 0
        gone = retire_previous(db, tenant, uri, None, expire_at=expire_at)
        mirror.retired(tenant, gone.get("retired_doc_keys", []), "withdrawn")
        db.collection("sources").document(source_id_for(tenant, name)).set(
            {"status": "withdrawn", "withdrawn_at": firestore.SERVER_TIMESTAMP}, merge=True)
        fp = refresh_fingerprint(db, tenant, "reconcile_withdrawn")
        print(json.dumps({"event": "reconcile_withdrawn", "gcs_uri": uri, "fingerprint": fp, **gone,
                          "note": "a tombstone: the object is kept and nothing automatic re-ingests it; make restore SOURCE= does"}))
        return 0

    if a.restore:
        # THE WAY BACK. The tombstone goes first, to `retired` - the state a lost event recovers from: if the
        # rewrite's finalize event never arrives, the next walk plans the reingest itself. Then the rewrite: a new
        # generation, the same event, the worker's one path - reactivate when every retired row is still inside the
        # undo window (nothing embedded), a fresh ingest when the TTL policy has been at them.
        uri = a.restore if a.restore.startswith("gs://") else f"gs://{bucket_name}/{a.restore}"
        name = uri.split(f"gs://{bucket_name}/", 1)[-1]
        tenant = name.split("/", 1)[0]
        ref = db.collection("sources").document(source_id_for(tenant, name))
        snap = ref.get()
        row = (snap.to_dict() or {}) if snap.exists else {}
        if row.get("status") != "withdrawn":
            print(json.dumps({"event": "reconcile_restore_refused", "gcs_uri": uri, "status": row.get("status"),
                              "why": "only a withdrawn source is restored: a retired one comes back when its object does, "
                                     "an indexed one is already live"}))
            return 1
        blob = bucket.get_blob(name)
        if blob is None:
            print(json.dumps({"event": "reconcile_restore_refused", "gcs_uri": uri, "status": "withdrawn",
                              "why": "the object is gone: upload the bytes again under this name and the worker ingests them"}))
            return 1
        if not a.apply:
            print(f"would restore {uri}: the tombstone cleared, the object rewritten onto itself, the worker reactivates the rows "
                  f"inside the {RETENTION_DAYS}-day undo window or re-ingests the bytes after it (--apply to do it)")
            return 0
        ref.set({"status": "retired", "restored_at": firestore.SERVER_TIMESTAMP,
                 "withdrawn_at": firestore.DELETE_FIELD}, merge=True)
        blob.rewrite(blob)
        print(json.dumps({"event": "reconcile_restored", "gcs_uri": uri, "generation": str(blob.generation),
                          "next": "ingest_reactivated inside the undo window, ingest_ok (a fresh version) after it"}))
        return 0

    if a.backfill:
        # Chunks from before the ledger: current=true and a doc_key from the id; a sources/ row per indexed version.
        n_chunks, n_rows = 0, 0
        batch, pending = db.batch(), 0
        q = db.collection("chunks")
        if a.tenant:
            q = q.where("tenant_id", "==", a.tenant)
        for snap in q.stream():
            d = snap.to_dict() or {}
            if "current" in d:
                continue
            key = d.get("doc_key") or snap.id.split("#")[0].replace(":", "_", 1)
            if a.apply:
                batch.update(snap.reference, {"current": True, "doc_key": key, "schema_version": 1})
                pending += 1
                if pending == 400:
                    batch.commit()
                    batch, pending = db.batch(), 0
            n_chunks += 1
        if pending:
            batch.commit()
        tenants = set()
        for snap in db.collection("documents").stream():
            d = snap.to_dict() or {}
            uri = d.get("gcs_uri") or ""
            if d.get("status") != "indexed" or not uri.startswith(f"gs://{bucket_name}/"):
                continue
            name = uri.split(f"gs://{bucket_name}/", 1)[1]
            tenant = name.split("/", 1)[0]
            if a.tenant and tenant != a.tenant:
                continue
            if a.apply:
                blob = bucket.get_blob(name)
                record_source(db, tenant, name, uri, snap.id, blob.generation if blob else "",
                              snap.id.split("_", 1)[1], int(d.get("chunks") or 0))
                tenants.add(tenant)
            n_rows += 1
        for tenant in sorted(tenants):
            refresh_fingerprint(db, tenant, "reconcile_backfill")
        print(json.dumps({"event": "reconcile_backfill", "chunks": n_chunks, "sources": n_rows,
                          "applied": a.apply}))
        return 0

    prefix = f"{a.tenant}/" if a.tenant else None
    objects = [{"name": b.name, "generation": str(b.generation), "tenant_id": b.name.split("/", 1)[0]}
               for b in bucket.list_blobs(prefix=prefix) if "/" in b.name]
    ledger = {s.id: (s.to_dict() or {}) for s in db.collection("sources").stream()
              if not a.tenant or (s.to_dict() or {}).get("tenant_id") == a.tenant}
    documents = {s.id: (s.to_dict() or {}) for s in db.collection("documents").stream()}
    actions = plan(objects, ledger, documents)
    summary = {"retire": 0, "reingest": 0, "backfill": 0, "touch": 0, "ok": 0, "withdrawn": 0, "queued": 0}
    touched = set()
    for act in actions:
        if act["action"] == "ok":
            summary["ok"] += 1
            continue
        if act["action"] == "check_bytes":
            blob = bucket.get_blob(act["name"])
            sha = sha256_of(blob.download_as_bytes())
            row = ledger.get(source_id_for(act["tenant_id"], act["name"]))
            act["action"] = decide_bytes(sha, act["tenant_id"], row, documents)
            act["sha256"] = sha
        summary[act["action"]] += 1
        print(json.dumps({"reconcile": act["action"], "name": act["name"], "why": act.get("why", "")}))
        if not a.apply:
            continue
        uri = f"gs://{bucket_name}/{act['name']}"
        if act["action"] == "retire":
            gone = retire_previous(db, act["tenant_id"], act["gcs_uri"] or uri, None, expire_at=expire_at)
            mirror.retired(act["tenant_id"], gone.get("retired_doc_keys", []), "retired")
            db.collection("sources").document(source_id_for(act["tenant_id"], act["name"])).set(
                {"status": "retired", "retired_at": firestore.SERVER_TIMESTAMP}, merge=True)
            touched.add(act["tenant_id"])
            print(json.dumps({"event": "reconcile_retired", "gcs_uri": uri, **gone}))
        elif act["action"] == "touch":
            db.collection("sources").document(source_id_for(act["tenant_id"], act["name"])).set(
                {"generation": act["generation"]}, merge=True)
        elif act["action"] == "backfill":
            key = f"{act['tenant_id']}_{act['sha256']}"
            record_source(db, act["tenant_id"], act["name"], uri, key, act["generation"], act["sha256"],
                          int(documents.get(key, {}).get("chunks") or 0))
            touched.add(act["tenant_id"])
        elif act["action"] == "reingest":
            # A rewrite onto itself: a new generation, the same finalize event, the same worker. One path.
            blob = bucket.blob(act["name"])
            blob.rewrite(blob)
            print(json.dumps({"event": "reconcile_reingest", "gcs_uri": uri}))
    for tenant in sorted(touched):
        refresh_fingerprint(db, tenant, "reconcile")
    # THE DRIFT LINE: the number the night is measured by. alerts.tf reads it off this event.
    print(json.dumps({"event": "reconcile_done", "applied": a.apply, **summary, "drift": drift_of(summary)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
