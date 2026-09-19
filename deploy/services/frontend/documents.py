"""Tenant-scoped uploads; the ingestion worker is the only indexing writer."""
import os
import requests
import streamlit as st
from auth import tenant_for
from chat import RAG_API_URL, _headers
from google.cloud import storage

_storage = storage.Client()
BUCKET = _storage.bucket(os.environ["UPLOAD_BUCKET"])
MIME_TYPES = {
    "pdf": "application/pdf",
    "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    "txt": "text/plain", "md": "text/plain",
    "png": "image/png", "jpg": "image/jpeg", "jpeg": "image/jpeg",
    "mp4": "video/mp4", "mp3": "audio/mpeg",
}


def upload_document(tenant_id: str, uploaded_file) -> dict:
    """Upload once to the watched prefix. This does not claim indexing has finished."""
    if not tenant_id or tenant_id in (".", "..") or any(c in tenant_id for c in "/\\"):
        raise ValueError("A valid, authorized tenant is required.")
    name = getattr(uploaded_file, "name", "")
    if (not isinstance(name, str) or not name.strip() or name in (".", "..")
            or any(c in name for c in "/\\") or any(ord(c) < 32 for c in name)
            or len(name.encode("utf-8")) > 255):
        raise ValueError("Use a plain filename, without a path or control characters.")
    extension = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if extension not in MIME_TYPES:
        raise ValueError("Unsupported document type.")
    mime = MIME_TYPES[extension]
    blob = BUCKET.blob(f"{tenant_id}/{name}")
    blob.chunk_size = 8 * 1024 * 1024
    blob.upload_from_file(uploaded_file, content_type=mime, timeout=300, rewind=True)
    return {"name": name, "gcs_uri": f"gs://{BUCKET.name}/{blob.name}",
            "generation": str(blob.generation or ""), "content_type": mime}


def _held_in(row: dict) -> str:
    """Where a version is held: the managed stores that confirmed it, with the region each holds it in (`mirrored`, the
    worker's stamp on the ledger row) - or the kit's own rows alone, which is every version of an `in` tenant."""
    held = row.get("mirrored") or {}
    return ", ".join(f"{store} ({region})" for store, region in sorted(held.items())) or "kit rows"


def versions_section(tenant_id: str) -> None:
    """The versions view (12 September 2026): GET /v1/sources - the tenant's ledger, as the API serves it. Which
    version of every document is current, what the last reindex cost (chunks reused by hash, embedded, retired),
    the date a document declares, when it landed - and, since 13 September 2026 (evening), where each version is HELD:
    the managed stores that confirmed it (the worker's mirror stamps `mirrored` on the ledger row) beside the tenant's
    data_region, the policy those copies were judged under. Rendered, never queried here: the page holds no Firestore
    credential for the ledger, the API checks the roster, and the same rows are `make sources TENANT=`."""
    st.subheader("Versions")
    try:
        r = requests.get(f"{RAG_API_URL}/v1/sources", params={"tenant_id": tenant_id}, headers=_headers(), timeout=30)
    except Exception as e:  # noqa: BLE001 - the ledger is a view; the upload path above it must not break
        st.info(f"The ledger is not reachable right now ({type(e).__name__}).")
        return
    if r.status_code == 404:
        st.info("This API revision predates the ledger's versions view (GET /v1/sources).")
        return
    if r.status_code != 200:
        st.info(f"The ledger answered {r.status_code}.")
        return
    body = r.json()
    rows = body.get("sources") or []
    if not rows:
        st.info("No documents indexed for this tenant yet.")
        return
    region = body.get("data_region") or "in"
    st.caption(f"{body.get('versions') or len(rows)} current versions - corpus fingerprint {body.get('fingerprint') or 'none yet'}"
               f" (last change: {body.get('last_event') or '-'}) - data_region {region}: "
               + ("the managed stores may hold copies" if region == "any" else "the kit's own rows only"))
    st.dataframe([{"document": r_["name"], "status": r_["status"], "chunks": r_["chunks"], "reused": r_["reused"],
                   "embedded": r_["embedded"], "retired": r_["retired"], "effective from": r_["effective_from"] or "",
                   "embedding": r_["embedding"], "indexed": (r_["indexed_at"] or "")[:19].replace("T", " "),
                   "held in": _held_in(r_)}
                  for r_ in rows], use_container_width=True)
    st.caption("A re-issued document keeps its name: the worker retires the previous version (never deletes it), reuses "
               "every chunk whose text did not change, and the retired rows expire by policy after the retention window.")


def documents_page(user):
    st.title("📄 Documents")
    tenant_id = tenant_for(user["email"])
    if not tenant_id:
        st.error("Your account is not a member of any DocuMind tenant. "
                 "Ask an administrator to add you.")
        st.stop()
    versions_section(tenant_id)
    if st.button("Refresh indexing status"):
        st.rerun()
    files = st.file_uploader("Upload documents for indexing",
                             type=list(MIME_TYPES), accept_multiple_files=True,
                             max_upload_size=200)
    if files and st.button("Index documents"):
        failures = 0
        with st.status("Uploading documents...", expanded=True) as status:
            for uploaded_file in files:
                try:
                    result = upload_document(tenant_id, uploaded_file)
                except Exception as exc:
                    failures += 1
                    st.error(f"Upload failed for {uploaded_file.name}: {type(exc).__name__}: {exc}")
                    continue
                st.write(f"Uploaded {result['name']} to {result['gcs_uri']} "
                         f"(generation {result['generation'] or 'not returned'}).")
            status.update(label=("Some uploads failed; inspect each result." if failures else
                                 "Upload complete; indexing is still in progress."),
                          state="error" if failures else "complete", expanded=True)
        st.info("documind-ingest performs parsing, screening, chunking and indexing. "
                "Use Refresh indexing status and verify the current document in Versions "
                "before asking a question. An upload confirmation is not an indexed answer.")
