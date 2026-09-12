"""The Cloud Run worker behind a Pub/Sub push subscription."""
import base64
import json
import logging
import os
import sys

from fastapi import FastAPI, HTTPException, Request
from google import genai
from google.genai import types as gtypes
from google.cloud import firestore, storage
from pydantic import BaseModel, ValidationError

# shared/ ships beside the service in the image (see the Dockerfile), the same
# way services/chat consumes documind_tools.
from shared.pii import inspect_image as pii_inspect_image, inspect_many as pii_inspect_many
from shared.audit_log import emit as audit_emit

from contracts import IngestMessage, DocumentContract, effective_from_of, sha256_of
from idempotency import (claim, drop_tenant_cache, finish, reactivate, record_source, release,
                         retire_previous, status_of)
from indexer import (embed_all, mirror_to_bigquery, mirror_to_firestore, remove_datapoints,
                     to_datapoints, upsert)
from parser import parse

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("documind.ingest")
app = FastAPI()
_db = firestore.Client()
_gcs = storage.Client()
INDEX_NAME = os.environ.get("VECTOR_INDEX_NAME", "")
PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
PROCESSOR_ID = os.environ.get("DOCAI_PROCESSOR_ID", "")    # docai.tf outputs it
GEN_MODEL = os.environ.get("GEN_MODEL", "gemini-3.6-flash")
# The SQL lane (5.5, gap G9): PROJECT.rag_data.chunk_source, declared by dataplex.tf. Unset
# means the lane is off; the worker never needs BigQuery to index a document.
BQ_CHUNK_TABLE = os.environ.get("BQ_CHUNK_TABLE", "")
_bq = None


def _bigquery():
    global _bq
    if _bq is None:
        from google.cloud import bigquery
        _bq = bigquery.Client(project=PROJECT)
    return _bq

# Fail at STARTUP, not on the first document. audit_log.emit refuses to drop an
# event, so an unset AUDIT_BUCKET would surface as a 500 halfway through an
# ingest, release the claim and retry into the DLQ - a configuration mistake
# wearing the costume of a data problem.
if not os.environ.get("AUDIT_BUCKET"):
    raise RuntimeError(
        "AUDIT_BUCKET is not set. The ingest worker records doc.upload and "
        "dlp.finding events; refusing to start without somewhere to put them.")
# parser.py sends a PDF to Document AI in 15-page slices at roughly a second a page, and the
# push subscription allows 600 seconds before it redelivers, so a document this size finishes
# inline with room to spare. Bigger than this goes to the batch lane - a claim document that
# nothing polls yet: the largest file in the kit's corpus is under two hundred pages.
MAX_INLINE_PAGES = 250

# 4.1's chunker, the shape every lesson's corpus has: ~500 tokens per chunk, an
# overlap so a sentence is never cut in half between two chunks.
CHUNK_CHARS, CHUNK_OVERLAP = 2000, 200

# The corpus has four modalities (Module 9) and ONE contract. An uploaded image, video or
# audio file is not parsed for text - it is DESCRIBED, and the description is what gets
# embedded and quoted; the asset rides alongside as media_url (9.6). kind names are
# the shared contract's: figure, table, segment - never a second vocabulary. The key is the
# content type the object.finalized record carries, which `gcloud storage cp`, the UI's
# uploader and 9.4's signed PUT all set from the file - not the extension.
MEDIA_TYPES = {"image/png": "figure", "image/jpeg": "figure",
               "video/mp4": "segment", "audio/mpeg": "segment"}
_gen = None


def _genai() -> genai.Client:
    # Generation is global-only (Gemini 3.x); lazy, so the worker starts without it.
    global _gen
    if _gen is None:
        _gen = genai.Client(enterprise=True, project=PROJECT, location="global")
    return _gen


def _parse(content: bytes, content_type: str) -> tuple[str, int]:
    """(text, pages). Plain text needs no processor; everything else goes to Doc AI."""
    if content_type.startswith("text/"):
        text = content.decode("utf-8", "replace")
        return text, max(1, text.count("\f") + 1)
    if not PROCESSOR_ID:
        raise RuntimeError("DOCAI_PROCESSOR_ID is not set (deploy/terraform/docai.tf outputs it)")
    return parse(PROJECT, PROCESSOR_ID, content, content_type)


def _chunk(text: str) -> list[dict]:
    """Fixed windows with overlap, ending on a sentence when there is one nearby.

    A text upload marks its page breaks with a form feed (the kit's real-document mirrors,
    evals/fetch_real.py, do; so does _parse's page count), and a chunk that starts on page 7
    is cited as page 7 - the same page_start shared/documind_corpus.py mints for the same
    bytes in a notebook. Text without form feeds has no page to name, and None is more honest
    than 1."""
    text = text.strip()
    paged = "\f" in text
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            cut = text.rfind(". ", start + CHUNK_CHARS // 2, end)
            if cut != -1:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            out.append({"text": piece, "kind": "text",
                        "page_start": text.count("\f", 0, start) + 1 if paged else None})
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return out


class Segment(BaseModel):
    start: float
    end: float
    summary: str


def _describe_media(gcs_uri: str, content_type: str) -> list[dict]:
    """9.6: a figure cannot be retrieved as pixels, so the caption IS the retrievable
    body; a video becomes segments with start/end in seconds. from_uri: the asset stays
    in GCS and never passes through this process."""
    kind = MEDIA_TYPES[content_type]
    part = gtypes.Part.from_uri(file_uri=gcs_uri, mime_type=content_type)
    if kind == "figure":
        r = _genai().models.generate_content(
            model=GEN_MODEL,
            contents=[part, "Describe this figure for retrieval: one caption sentence, then the "
                            "key facts it shows, then any table it contains as Markdown."],
            config=gtypes.GenerateContentConfig(
                thinking_config=gtypes.ThinkingConfig(thinking_level="LOW")))
        return [{"text": (r.text or "").strip(), "kind": "figure", "media_url": gcs_uri}]
    audio = content_type.startswith("audio/")
    what = "recording" if audio else "video"
    shown = "what is said" if audio else "what is said and shown"
    config = dict(response_mime_type="application/json", response_schema=list[Segment],
                  thinking_config=gtypes.ThinkingConfig(thinking_level="LOW"))
    if not audio:
        # LOW: 'what was said and roughly when' does not need to read text off slides (9.4).
        # A resolution is a frame-sampling dial; an audio file has no frames to sample.
        config["media_resolution"] = gtypes.MediaResolution.MEDIA_RESOLUTION_LOW
    # "quoting every number": a summary paraphrases, and the first live town hall came back as "a slight
    # contraction" where the speaker said "fell 5.2 per cent" - the segment was found, the figure was
    # gone. The caption is the quote (9.6): what is not in the segment's text cannot be retrieved by it.
    r = _genai().models.generate_content(
        model=GEN_MODEL,
        contents=[part, f"Split this {what} into segments of at most 60 seconds. For each, give start "
                        f"and end in seconds and a two-sentence summary of {shown}, quoting every number, "
                        f"percentage, amount and name that is spoken exactly as it is said."],
        config=gtypes.GenerateContentConfig(**config))
    return [{"text": s.summary, "kind": "segment", "media_url": gcs_uri,
             "start": s.start, "end": s.end} for s in (r.parsed or [])]


def _enqueue_batch(doc: DocumentContract) -> None:
    """The batch lane. A claim document the batch worker polls; no second queue to provision."""
    _db.collection("ingest_batch").document(doc.doc_key).set({
        "tenant_id": doc.tenant_id, "gcs_uri": doc.gcs_uri, "pages": doc.pages,
        "status": "queued", "queued_at": firestore.SERVER_TIMESTAMP})


@app.post("/")
async def push(request: Request):
    envelope = await request.json()
    try:
        raw = base64.b64decode(envelope["message"]["data"])
        msg = IngestMessage.model_validate_json(raw)
    except (KeyError, ValueError, ValidationError) as e:
        # 400, NOT 500. A message this worker can never parse must not be
        # retried five times before reaching the DLQ - it will fail the same
        # way every time. Ack the poison and let the DLQ hold it.
        log.warning(json.dumps({"event": "ingest_poison", "error": str(e)[:200]}))
        raise HTTPException(400, "unparseable message")

    blob = _gcs.bucket(msg.bucket).blob(msg.name)
    content = blob.download_as_bytes()
    doc = DocumentContract(tenant_id=msg.tenant_id, sha256=sha256_of(content),
                           gcs_uri=msg.gcs_uri, pages=0)

    if not claim(_db, doc.doc_key, doc.gcs_uri):
        if status_of(_db, doc.doc_key) == "superseded":
            # THE UNDO (the ledger, 11 September 2026). The same bytes again, after a newer version
            # retired them: the chunks are still here, flagged. Flip them back, retire the newer
            # version in turn, and nothing is re-embedded - because nothing was ever deleted.
            back = reactivate(_db, doc.tenant_id, doc.gcs_uri, doc.doc_key)
            gone = retire_previous(_db, doc.tenant_id, doc.gcs_uri, doc.doc_key)
            if INDEX_NAME and gone["retired_ids"]:
                remove_datapoints(INDEX_NAME, gone["retired_ids"])
            record_source(_db, doc.tenant_id, msg.name, doc.gcs_uri, doc.doc_key, msg.generation,
                          doc.sha256, back, effective_from_of(msg.name, None))
            drop_tenant_cache(_db, doc.tenant_id)
            log.info(json.dumps({"event": "ingest_reactivated", "tenant": doc.tenant_id,
                                 "doc_key": doc.doc_key, "chunks": back,
                                 "retired": gone["retired_doc_keys"]}))
            return {"status": "reactivated", "doc_key": doc.doc_key, "chunks": back}
        # Already done by an earlier delivery, or by an earlier upload of the
        # same bytes. Returning 200 ACKS the message: this is a success, not a
        # failure, and retrying it would achieve nothing.
        return {"status": "duplicate", "doc_key": doc.doc_key}

    gone = {"retired_doc_keys": [], "retired_ids": [], "retired_chunks": 0}
    try:
        image_findings = []
        if msg.content_type in MEDIA_TYPES:
            chunks = _describe_media(doc.gcs_uri, msg.content_type)
            pages = 1
            doc = doc.model_copy(update={"pages": 1, "doc_type": MEDIA_TYPES[msg.content_type],
                                        "effective_from": effective_from_of(msg.name, None)})
            # 9.6: the PIXELS are scanned, not only the caption. The caption is Gemini's
            # description of the picture, and a description of an invoice can carry the
            # invoice's PAN in plain text - so the scan below would find it there too, but a
            # picture of a form with a PAN the caption did not mention would sail into the
            # index. Same info-types, same no-quote rule (shared/pii.py); a video is not an
            # image DLP can read and yields nothing here.
            image_findings = pii_inspect_image(content, msg.content_type)
        else:
            text, pages = _parse(content, msg.content_type)
            doc = doc.model_copy(update={"pages": pages,
                                        "effective_from": effective_from_of(msg.name, text)})
            if pages > MAX_INLINE_PAGES:
                # A 400-page contract will not finish inside a push request's
                # timeout. Hand it to the batch lane and ack.
                _enqueue_batch(doc)
                return {"status": "queued_batch", "pages": pages}
            chunks = _chunk(text)

        # Scan BEFORE indexing. After the upsert the PII is in the index, and
        # "we scanned it afterwards" is a description of a breach, not a control.
        # A DLP failure fails the whole message: it is nacked, retried, and ends
        # in the DLQ where a human decides - because indexing an unscanned
        # document is the exact thing this control exists to prevent.
        # One scan per document, not per chunk: DLP meters requests per minute, and a
        # corpus load from ten workers at a chunk a request was refused (first live load).
        findings = [{**f, "chunk_id": doc.chunk_id(0)} for f in image_findings]
        for i, chunk_findings in enumerate(pii_inspect_many([c["text"] for c in chunks])):
            for f in chunk_findings:
                findings.append({**f, "chunk_id": doc.chunk_id(i)})
        if findings:
            # No quotes, only types and offsets - see shared/pii.py.
            _db.collection("dlp_findings").add({
                "doc_key": doc.doc_key, "tenant_id": doc.tenant_id,
                "findings": findings, "count": len(findings),
                "scanned_at": firestore.SERVER_TIMESTAMP,
            })
            audit_emit("dlp.finding",
                       actor={"tenant_id": doc.tenant_id, "email": "system:ingest"},
                       target={"type": "document", "id": doc.doc_key,
                               "tenant_id": doc.tenant_id},
                       meta={"types": sorted({f["info_type"] for f in findings}),
                             "count": len(findings)})

        vectors = embed_all([c["text"] for c in chunks])
        if INDEX_NAME:                       # the full profile; the lean one has no index
            upsert(INDEX_NAME, to_datapoints(doc, chunks, vectors))
        mirror_to_firestore(_db, doc, chunks, vectors)
        # THE LEDGER (11 September 2026). The new version is current from the line above; every
        # other version of this object path is now retired - flagged, never deleted - so the index
        # holds exactly one current reading of a document. A re-issued handbook replaces its
        # predecessor instead of standing beside it, and a citation opens the page it quotes.
        # After the write, never before: a reader between the two steps still finds a document.
        gone = retire_previous(_db, doc.tenant_id, doc.gcs_uri, doc.doc_key)
        if INDEX_NAME and gone["retired_ids"]:
            remove_datapoints(INDEX_NAME, gone["retired_ids"])
        # The SQL lane reads the REAL chunks (5.5, gap G9): the same rows, with the verdict
        # the scan above just produced, so pii_flag in BigQuery is this worker's - never a
        # second scanner's that could disagree.
        mirror_to_bigquery(_bigquery() if BQ_CHUNK_TABLE else None, BQ_CHUNK_TABLE, doc, chunks,
                           {f["chunk_id"] for f in findings})
        finish(_db, doc.doc_key, len(chunks))
        record_source(_db, doc.tenant_id, msg.name, doc.gcs_uri, doc.doc_key, msg.generation,
                      doc.sha256, len(chunks), doc.effective_from)
        # The cache follows the index: the tenant's pack record goes, the next answer runs uncached,
        # make cache rebuilds the pack from the corpus that changed (12.6, 10.2).
        drop_tenant_cache(_db, doc.tenant_id)
        if gone["retired_chunks"]:
            log.info(json.dumps({"event": "ingest_superseded", "tenant": doc.tenant_id,
                                 "doc_key": doc.doc_key, "gcs_uri": doc.gcs_uri,
                                 "retired_doc_keys": gone["retired_doc_keys"],
                                 "retired_chunks": gone["retired_chunks"]}))

        # The document is now retrievable. Record that, with who and what - the
        # upload event the audit trail is missing without it.
        audit_emit("doc.upload",
                   actor={"tenant_id": doc.tenant_id, "email": "system:ingest"},
                   target={"type": "document", "id": doc.doc_key,
                           "tenant_id": doc.tenant_id},
                   meta={"gcs_uri": doc.gcs_uri, "pages": doc.pages,
                         "chunks": len(chunks), "pii": bool(findings),
                         "kinds": sorted({c["kind"] for c in chunks})})
    except Exception as e:
        # Give the claim back before failing, or the retry finds the document
        # already claimed and does nothing - for ever. And SAY what failed, on the log
        # line an operator reads first: the claim document carries the same text, but
        # the first live load was diagnosed from request logs that only said 500.
        release(_db, doc.doc_key, f"{type(e).__name__}: {e}")
        log.error(json.dumps({"event": "ingest_failed", "tenant": doc.tenant_id,
                              "doc_key": doc.doc_key, "gcs_uri": doc.gcs_uri,
                              "error": f"{type(e).__name__}: {e}"[:600]}))
        raise HTTPException(500, "ingest failed")

    log.info(json.dumps({"event": "ingest_ok", "tenant": doc.tenant_id,
                         "doc_key": doc.doc_key, "chunks": len(chunks),
                         "pages": pages, "kinds": sorted({c["kind"] for c in chunks}),
                         "retired": gone["retired_chunks"], "effective_from": doc.effective_from}))
    return {"status": "indexed", "chunks": len(chunks)}
