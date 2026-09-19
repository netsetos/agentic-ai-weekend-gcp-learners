#!/usr/bin/env python3
"""Read-only combined-filter probe using a verified source's actual stored metadata.

Run from deploy with the runbook venv, PROJECT and GOOGLE_CLOUD_PROJECT restored:
    python commands/check-firestore-fallback.py

GCS-ingested text may have doc_type=unknown while notebook seeds use the manifest type.
This checks the Firestore fallback and its indexes, not document classification or
natural-language answer quality. It never changes cloud metadata or API settings.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import sys


def require(condition, message):
    if not condition:
        raise ValueError(message)


def select_seed(rows, *, tenant, source_uri, doc_key, embedding_model, embedding_version):
    current = sorted(((key, row) for key, row in rows if row.get("current") is True), key=lambda x: x[0])
    require(current, f"No current chunks for {source_uri}; check the project, collection and source ledger.")
    doc_types = set()
    for key, row in current:
        require(row.get("tenant_id") == tenant and row.get("source_uri") == source_uri,
                f"Unexpected tenant/source on {key}")
        require(row.get("doc_key") == doc_key, f"Current version disagrees with verified source on {key}")
        require(not row.get("staged"), f"Current chunk is still staged: {key}")
        require(row.get("kind") == "text", f"Expected a text chunk: {key}")
        doc_type = row.get("doc_type")
        require(isinstance(doc_type, str) and bool(doc_type.strip()), f"Missing document type on {key}")
        doc_types.add(doc_type)
        require(row.get("embedding_model") == embedding_model
                and str(row.get("embedding_version")) == str(embedding_version)
                and row.get("embedding_task_type") == "RETRIEVAL_DOCUMENT",
                f"Embedding stamp mismatch on {key}; use the runbook's document-embedding checks.")
        raw = row.get("embedding")
        require(raw is not None, f"Embedding missing on {key}")
        try:
            vector = [float(value) for value in raw]
        except (TypeError, ValueError) as error:
            raise ValueError(f"Invalid embedding on {key}") from error
        require(len(vector) == 768 and all(math.isfinite(value) for value in vector)
                and any(value != 0 for value in vector), f"Invalid 768-dimensional embedding on {key}")
    require(len(doc_types) == 1, f"Mixed document types in the current source: {sorted(doc_types)}")
    key, row = current[0]
    return key, row, [float(value) for value in row["embedding"]], {"doc_type": row["doc_type"], "kind": "text"}


def verify_hits(hits, *, tenant, source_uri, filters, current_only):
    require(bool(hits), "No combined-filter results; inspect the Firestore vector index and stored metadata.")
    for row in hits:
        key = row.get("id", "<missing id>")
        require(row.get("tenant_id") == tenant, f"Tenant filter failed: {key}")
        require(all(row.get(field) == value for field, value in filters.items()),
                f"Combined document-type/kind filter failed: {key}")
        require(row.get("found_by") == "firestore", f"Result did not come from Firestore: {key}")
        require(not row.get("staged"), f"Staged candidate returned: {key}")
        if current_only:
            require(row.get("current") is True, f"Current-only filter failed: {key}")
    require(any(row.get("source_uri") == source_uri for row in hits), "Control HR source was not returned.")


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--evidence", default="operator-evidence/firestore-combined-filters.json")
    args = ap.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    project = os.environ.get("PROJECT", "")
    require(project, "Restore PROJECT before running this checkpoint.")
    require(os.environ.get("GOOGLE_CLOUD_PROJECT") == project, "PROJECT and GOOGLE_CLOUD_PROJECT must agree.")
    sys.path[:0] = [str(root), str(root / "services" / "rag-api")]
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter
    from config import settings
    from retriever import _firestore_fallback

    require(settings.project_id == project, "API settings point to another project.")
    tenant = "acme"
    name = "acme/hr_policy_2026.md"
    uri = f"gs://{project}-uploads/{name}"
    local_file = root / "evals" / "corpus" / name
    require(local_file.is_file(), f"Fetch the frozen HR source first: {local_file}")
    digest = hashlib.sha256(local_file.read_bytes()).hexdigest()
    doc_key = f"{tenant}_{digest}"
    db = firestore.Client(project=project, database="(default)")
    source = db.collection("sources").document(name.replace("/", "~")).get().to_dict() or {}
    print(json.dumps({"project": project, "collection": settings.chunks_collection, "source_uri": uri,
                      "source_status": source.get("status"), "source_doc_key": source.get("doc_key")}), flush=True)
    require(source.get("status") == "indexed", "HR source ledger is not indexed; inspect source ingestion in this project.")
    require(source.get("tenant_id") == tenant and source.get("gcs_uri") == uri
            and source.get("doc_key") == doc_key and source.get("sha256") == digest,
            "HR source ledger does not match the selected local HR file; review the source version.")
    query = db.collection(settings.chunks_collection).where(filter=FieldFilter("source_uri", "==", uri))
    rows = [(snap.id, snap.to_dict() or {}) for snap in query.stream()]
    current_count = sum(row.get("current") is True for _, row in rows)
    require(current_count == int(source.get("chunks") or 0) and current_count > 0,
            f"Source ledger/current chunk counts disagree: {source.get('chunks')} vs {current_count}")
    seed_id, seed, vector, filters = select_seed(rows, tenant=tenant, source_uri=uri, doc_key=doc_key,
                                                embedding_model=settings.embed_model,
                                                embedding_version=settings.embedding_version)
    manifest = json.loads((root / "evals" / "manifest.json").read_text())
    expected = next((row.get("doc_type") for row in manifest if row.get("file") == f"corpus/{name}"), None)
    require(expected is not None, "HR fixture is missing from the corpus manifest.")
    metadata_matches = filters["doc_type"] == expected
    print(f"Verified HR source: {current_count} current chunks; stored doc_type={filters['doc_type']!r}; "
          f"manifest doc_type={expected!r}", flush=True)
    if not metadata_matches:
        print("NOTE: stored metadata differs from the manifest. This probe tests the stored equality filters; "
              "it does not certify policy classification or repair metadata.", flush=True)
    report = {"scope": "Firestore combined-filter plumbing, not classification or answer quality",
              "project": project, "collection": settings.chunks_collection, "source_uri": uri,
              "seed_id": seed_id, "doc_key": doc_key, "current_chunks": current_count,
              "filters": filters, "manifest_doc_type": expected, "metadata_matches_manifest": metadata_matches,
              "results": {}}
    previous = settings.retrieval_current_only
    try:
        for mode in ("off", "on"):
            settings.retrieval_current_only = mode
            hits = _firestore_fallback(vector, tenant, 5, filters)
            verify_hits(hits, tenant=tenant, source_uri=uri, filters=filters, current_only=mode == "on")
            report["results"][mode] = {"ids": [row["id"] for row in hits], "control_source_found": True}
            print(f"PASS: combined doc_type={filters['doc_type']!r}+kind='text', current={mode}, rows={len(hits)}", flush=True)
    finally:
        settings.retrieval_current_only = previous
    path = Path(args.evidence)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(report, indent=2) + "\n")
    print(f"PASS: both Firestore filter modes verified. Evidence: {path}")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except ValueError as error:
        raise SystemExit(f"STOP: {error}") from error
