import os
import requests
import streamlit as st
from auth import tenant_for
from chat import RAG_API_URL, _headers
from google.cloud import storage, documentai_v1 as docai
from google import genai
from google.genai import types
from google.cloud import aiplatform

_storage = storage.Client()
BUCKET = _storage.bucket(os.environ["UPLOAD_BUCKET"])
_docai = docai.DocumentProcessorServiceClient(
    client_options={"api_endpoint": "us-documentai.googleapis.com"})
_genai = genai.Client(enterprise=True, project=os.environ.get("GOOGLE_CLOUD_PROJECT"), location=os.environ.get("GOOGLE_CLOUD_LOCATION", "us-central1"))
LAYOUT_PROCESSOR = os.environ["LAYOUT_PROCESSOR"]

def extract_chunks(doc, source_uri):
    chunks = []
    for i, c in enumerate(doc.chunked_document.chunks):
        chunks.append({
            "id": f"{source_uri}#{i}",
            "text": c.content,
            "page_start": c.page_span.page_start if c.page_span else None,
            "source_uri": source_uri,
        })
    return chunks

def parse_layout(gcs_uri):
    req = docai.ProcessRequest(
        name=f"{LAYOUT_PROCESSOR}/processorVersions/pretrained-layout-parser-v1.5-2025-08-25",
        gcs_document=docai.GcsDocument(gcs_uri=gcs_uri, mime_type="application/pdf"),
        process_options=docai.ProcessOptions(
            layout_config=docai.ProcessOptions.LayoutConfig(
                chunking_config=docai.ProcessOptions.LayoutConfig.ChunkingConfig(
                    chunk_size=500, include_ancestor_headings=True))))
    return _docai.process_document(request=req).document

def embed_batch(chunks):
    for i in range(0, len(chunks), 5):
        batch = chunks[i:i+5]
        resp = _genai.models.embed_content(
            model="text-embedding-005",
            contents=[c["text"] for c in batch],
            config=types.EmbedContentConfig(
                task_type="RETRIEVAL_DOCUMENT", output_dimensionality=768))
        for c, e in zip(batch, resp.embeddings):
            c["embedding"] = e.values
    return chunks

def versions_section(tenant_id: str) -> None:
    """The versions view (12 September 2026): GET /v1/sources - the tenant's ledger, as the API serves it. Which
    version of every document is current, what the last reindex cost (chunks reused by hash, embedded, retired),
    the date a document declares, when it landed. Rendered, never queried here: the page holds no Firestore
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
    st.caption(f"{body.get('versions') or len(rows)} current versions - corpus fingerprint {body.get('fingerprint') or 'none yet'}"
               f" (last change: {body.get('last_event') or '-'})")
    st.dataframe([{"document": r_["name"], "status": r_["status"], "chunks": r_["chunks"], "reused": r_["reused"],
                   "embedded": r_["embedded"], "retired": r_["retired"], "effective from": r_["effective_from"] or "",
                   "embedding": r_["embedding"], "indexed": (r_["indexed_at"] or "")[:19].replace("T", " ")}
                  for r_ in rows], use_container_width=True)
    st.caption("A re-issued document keeps its name: the worker retires the previous version (never deletes it), reuses "
               "every chunk whose text did not change, and the retired rows expire by policy after the retention window.")


def documents_page(user):
    st.title("📄 Documents")
    # The tenant ONCE, before anything is rendered or stored, and a stop when there is none - the
    # gate chat.py and studio.py already have. Until 12 September 2026 this page asked tenant_for()
    # twice and never looked at the answer: a signed-in person on no roster filed every document
    # under "None/<file>", and the ingest worker indexed a tenant called None (12.5, contracts.py).
    # IAP says who you are; only the roster says where your documents go.
    tenant_id = tenant_for(user["email"])
    if not tenant_id:
        st.error("Your account is not a member of any DocuMind tenant. "
                 "Ask an administrator to add you.")
        st.stop()
    versions_section(tenant_id)
    # Documents AND media (9.4 / 9.6): an image, a video or a recording is a document to the
    # ingest worker - it is described, not parsed, and its caption or segments join the same
    # chunks the text does. The list is what the worker's MEDIA_TYPES and parser accept.
    files = st.file_uploader("Upload documents for indexing",
                             type=["pdf", "docx", "txt", "md", "png", "jpg", "jpeg", "mp4", "mp3"],
                             accept_multiple_files=True,
                             max_upload_size=200)

    if files and st.button("Index documents"):
        with st.status("Processing...", expanded=True) as status:
            for f in files:
                st.write(f"📤 Uploading {f.name}")
                # Tenant, not user. Documents belong to the company that owns them,
                # and keying on `sub` filed every colleague's copy separately -
                # which is why nothing uploaded here was ever findable by anyone else.
                #
                # `{tenant}/{name}`: the ingest worker reads the tenant from the FIRST path
                # segment of the object it is told about (12.5, contracts.py), exactly as
                # evals/upload.sh names the corpus. The earlier `tenants/{tenant}/{id}/`
                # shape filed every upload from this page under a tenant called "tenants".
                blob = BUCKET.blob(f"{tenant_id}/{f.name}")
                blob.chunk_size = 8 * 1024 * 1024
                # content_type is what the worker keys its media branch on - the notification
                # carries it; the extension is never consulted.
                blob.upload_from_file(f, content_type=f.type, timeout=300)
                gcs_uri = f"gs://{BUCKET.name}/{blob.name}"
                if (f.type or "").split("/")[0] in ("image", "video", "audio"):
                    st.write(f"🎞️ {f.name} ({f.type}) handed to documind-ingest: described by Gemini, "
                             f"scanned, indexed as a figure or segments. list_documents (MCP) shows it.")
                    continue

                st.write(f"🔍 Parsing layout ({f.name})")
                doc = parse_layout(gcs_uri)
                chunks = extract_chunks(doc, gcs_uri)

                st.write(f"📈 Embedding {len(chunks)} chunks")
                chunks = embed_batch(chunks)

                st.write(f"💾 Upserting to Vector Search")
                # upsert_datapoints(chunks, user["sub"])  # Module 11 pattern

            status.update(label="Done!", state="complete")
