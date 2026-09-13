"""The Cloud Run worker behind a Pub/Sub push subscription."""
import base64
import io
import json
import logging
import os
import re
import sys
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI, HTTPException, Request
from google import genai
from google.genai import types as gtypes
from google.api_core.exceptions import NotFound
from google.cloud import firestore, storage
from pydantic import BaseModel, ValidationError
from pypdf import PdfReader

# shared/ ships beside the service in the image (see the Dockerfile), the same
# way services/chat consumes documind_tools.
from shared.pii import inspect_image as pii_inspect_image, inspect_many as pii_inspect_many
from shared.audit_log import emit as audit_emit

from contracts import IngestMessage, DocumentContract, chunk_hash, effective_from_of, sha256_of
from idempotency import (claim, current_chunks, finish, reactivate, record_source, refresh_fingerprint, release,
                         retire_previous, source_id_for, stale_generation, status_of, swap_versions, withdrawn)
from indexer import (EMBEDDING_MODEL, EMBEDDING_VERSION, embed_with_carry_over, mirror_to_bigquery,
                     mirror_to_firestore, remove_datapoints, reupsert, to_datapoints, upsert)
from parser import parse

logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
log = logging.getLogger("documind.ingest")
app = FastAPI()
_db = firestore.Client()
_gcs = storage.Client()
INDEX_NAME = os.environ.get("VECTOR_INDEX_NAME", "")
PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
PROCESSOR_ID = os.environ.get("DOCAI_PROCESSOR_ID", "")    # docai.tf outputs it
REGION = os.environ.get("REGION", "us-central1")
# The batch lane's consumer (13 September 2026): the Cloud Run JOB batch.tf declares on this image, named here once
# make batch-job has declared it. Empty: nothing is started when a document is queued, and the queue waits for
# make batch (gcloud run jobs execute) - the lane says so on the ingest_queued_batch line.
BATCH_JOB = os.environ.get("BATCH_JOB", "")
GEN_MODEL = os.environ.get("GEN_MODEL", "gemini-3.6-flash")
# The SQL lane (5.5, gap G9): PROJECT.rag_data.chunk_source, declared by dataplex.tf. Unset
# means the lane is off; the worker never needs BigQuery to index a document.
BQ_CHUNK_TABLE = os.environ.get("BQ_CHUNK_TABLE", "")
# Retention (12 September 2026): a retired row is stamped expire_at = now + RETENTION_DAYS, and the Firestore TTL
# policy in firestore_indexes.tf deletes it after that - the only deleter on the lane. The number is variables.tf's
# retention_days, passed by make deploy-services; it is the audit window and the undo window at once.
RETENTION_DAYS = int(os.environ.get("RETENTION_DAYS", "30"))
STAGE_HOURS = 24            # a staged version nothing ever swapped expires by the same policy
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
# inline with room to spare. Bigger than this goes to the batch lane - a claim document the batch job consumes
# (batch.py, batch.tf: no request deadline, the same pipeline as below, 13 September 2026); the largest file in
# the kit's corpus is under two hundred pages, so the corpus never takes it.
MAX_INLINE_PAGES = 250

# 4.1's chunker, the shape every lesson's corpus has: ~500 tokens per chunk, an
# overlap so a sentence is never cut in half between two chunks.
CHUNK_CHARS, CHUNK_OVERLAP = 2000, 200
# A handbook's sections (12 September 2026): the same two rules shared/documind_corpus.py chunks with, so the
# lane and the notebooks mint the same chunk texts - and a chunk keeps its identity when a paragraph above it
# changes. Fixed windows over a whole document do not: one inserted line at the top moved every window of the
# handbook, and a re-issue that changed one clause reused none of its 92 windows. By section it reuses 281 of 283.
_SECTION = re.compile(r"^## +(.+?) *$", re.M)
_CODE = re.compile(r"^([A-Z][A-Z0-9]{0,7}(?:-[A-Z0-9]{1,6}){1,2})\b")   # NP-03, IT-SEC-04, MSA-04, GEN-014

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


def _expire_at(days: float) -> datetime:
    return datetime.now(timezone.utc) + timedelta(days=days)


def _pdf_pages(content: bytes) -> int | None:
    """A PDF's page count off its page tree, before any OCR is paid for (12 September 2026). pypdf reads the tree
    and renders nothing - parser.py already opens the file this way to slice it - and the batch decision needs only
    the number. None when the bytes will not parse: Doc AI then gets its turn and says what is wrong with them."""
    try:
        return len(PdfReader(io.BytesIO(content)).pages)
    except Exception:  # noqa: BLE001 - a count is a hint for routing, never a verdict on the document
        return None


def _parse(content: bytes, content_type: str) -> tuple[str, int]:
    """(text, pages). Plain text needs no processor; everything else goes to Doc AI."""
    if content_type.startswith("text/"):
        text = content.decode("utf-8", "replace")
        return text, max(1, text.count("\f") + 1)
    if not PROCESSOR_ID:
        raise RuntimeError("DOCAI_PROCESSOR_ID is not set (deploy/terraform/docai.tf outputs it)")
    return parse(PROJECT, PROCESSOR_ID, content, content_type)


def _windows(text: str) -> list[tuple[int, str]]:
    """Fixed windows with overlap, ending on a sentence when there is one nearby; (start offset, text)."""
    text = text.strip()
    out, start = [], 0
    while start < len(text):
        end = min(len(text), start + CHUNK_CHARS)
        if end < len(text):
            cut = text.rfind(". ", start + CHUNK_CHARS // 2, end)
            if cut != -1:
                end = cut + 1
        piece = text[start:end].strip()
        if piece:
            out.append((start, piece))
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return out


def _chunk(text: str) -> list[dict]:
    """A document into chunks, each with a LOCATOR that survives an edit above it.

    A handbook - Markdown with `## ` headings - is one chunk per section, the clause code in the heading
    (NP-03, IT-SEC-04) or the section's ordinal as the locator, a long section windowed within itself.
    Anything else is fixed windows. A text upload marks its page breaks with a form feed (the kit's
    real-document mirrors, evals/fetch_real.py, do; so does parser.py between Doc AI pages), and the windows
    are cut PER PAGE (12 September 2026): a window never crosses a page break, a chunk that starts on page 7
    is cited as page 7, and its locator is the page and the window's ordinal on it (p7-1) - the same texts,
    hashes and locators shared/documind_corpus.py mints for the same bytes in a notebook, so a mirror seeded
    there and the same file uploaded here are one set of chunks, not two. Text without form feeds has no
    page to name, and None is more honest than 1. A mirror's provenance header - the leading <!-- ... -->
    fetch_real.py writes - is not content and is dropped the way the loader drops it. Every chunk carries
    the hash of its text: the carry-over in indexer.py matches on it, so a one-clause edit embeds one clause."""
    text = re.sub(r"\A\s*<!--.*?-->\s*", "", text, count=1, flags=re.S)
    heads = list(_SECTION.finditer(text.strip()))
    out = []
    if heads:
        text = text.strip()
        pre = text[:heads[0].start()].strip()
        for k, (_, piece) in enumerate(_windows(pre)):
            out.append({"text": piece, "kind": "text", "page_start": None,
                        "locator": "preamble" + (f"-{k}" if k else ""), "section": None})
        for n, h in enumerate(heads):
            title = h.group(1).strip()
            body = text[h.end(): heads[n + 1].start() if n + 1 < len(heads) else len(text)].strip()
            code = _CODE.match(title)
            key = code.group(1) if code else f"s{n + 1}"
            for k, (_, piece) in enumerate(_windows(f"{title}\n{body}")):
                out.append({"text": piece, "kind": "text", "page_start": None,
                            "locator": key + (f"-{k}" if k else ""), "section": title})
    else:
        paged = "\f" in text
        for p, page in enumerate(text.split("\f"), 1):
            for k, (_, piece) in enumerate(_windows(page)):
                out.append({"text": piece, "kind": "text", "page_start": p if paged else None,
                            "locator": f"p{p}-{k}" if paged else f"w{k}", "section": None})
    for c in out:
        c["chunk_hash"] = chunk_hash(c["text"])
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
        text = (r.text or "").strip()
        return [{"text": text, "kind": "figure", "media_url": gcs_uri, "locator": "figure",
                 "chunk_hash": chunk_hash(text)}]
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
             "start": s.start, "end": s.end, "locator": f"t{int(s.start)}-{int(s.end)}",
             "chunk_hash": chunk_hash(s.summary)} for s in (r.parsed or [])]


class IngestFailed(Exception):
    """index_document() gave the claim back and logged ingest_failed; the caller decides what a failure is on its
    lane - a 500 on the push path (Pub/Sub retries, then the DLQ), a failed record on the batch path."""


def _run_batch_job() -> str:
    """Start the batch job (batch.tf: documind-ingest-batch) once, through the Cloud Run Jobs API, as this worker's
    own identity - batch.tf grants it run.invoker on the job. A failure here is logged and swallowed by the caller:
    the hourly schedule and make batch drain the same queue, and a document that is queued is not a failed ingest."""
    import google.auth
    from google.auth.transport.requests import AuthorizedSession
    creds, _ = google.auth.default(scopes=["https://www.googleapis.com/auth/cloud-platform"])
    url = f"https://run.googleapis.com/v2/projects/{PROJECT}/locations/{REGION}/jobs/{BATCH_JOB}:run"
    r = AuthorizedSession(creds).post(url, json={}, timeout=30)
    r.raise_for_status()
    return (r.json().get("metadata") or {}).get("name", "")


def _enqueue_batch(doc: DocumentContract, msg: IngestMessage) -> None:
    """The batch lane. A claim document the batch job consumes; no second queue to provision.

    The document WAITS, and its claim says so - `queued`, not `processing` - which is what keeps a redelivery from
    being acked as a duplicate and the nightly reconcile from rewriting the object onto itself: the worker answers
    queued_batch, reconcile's plan reports `queued` for that generation and counts it apart from the drift. The
    record carries everything the consumer needs to fetch the same bytes (the object, its generation, its type),
    and when the job is declared (BATCH_JOB, batch.tf) it is started here, so a queued document is indexed minutes
    later and not at the next hour. The log line says which (13 September 2026)."""
    _db.collection("ingest_batch").document(doc.doc_key).set({
        "tenant_id": doc.tenant_id, "gcs_uri": doc.gcs_uri, "bucket": msg.bucket, "name": msg.name,
        "content_type": msg.content_type, "size": msg.size, "pages": doc.pages, "generation": str(msg.generation),
        "status": "queued", "queued_at": firestore.SERVER_TIMESTAMP})
    _db.collection("documents").document(doc.doc_key).set(
        {"status": "queued", "pages": doc.pages, "generation": str(msg.generation),
         "queued_at": firestore.SERVER_TIMESTAMP}, merge=True)
    consumer = "no job declared (BATCH_JOB unset): make batch runs the queue now, make batch-job declares and schedules it"
    if BATCH_JOB:
        try:
            op = _run_batch_job()
            consumer = f"{BATCH_JOB} started ({op or 'run requested'}); the hourly schedule backstops it"
        except Exception as e:  # noqa: BLE001 - the queue is durable; the schedule and make batch drain it
            consumer = f"{BATCH_JOB} could not be started ({type(e).__name__}); the hourly schedule drains the queue"
            log.warning(json.dumps({"event": "batch_job_start_failed", "tenant": doc.tenant_id, "doc_key": doc.doc_key,
                                    "job": BATCH_JOB, "error": f"{type(e).__name__}: {e}"[:300]}))
    log.warning(json.dumps({"event": "ingest_queued_batch", "tenant": doc.tenant_id, "doc_key": doc.doc_key,
                            "gcs_uri": doc.gcs_uri, "pages": doc.pages, "generation": str(msg.generation),
                            "consumer": consumer}))


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

    # THE GENERATION GUARD (12 September 2026). Push delivery is at-least-once and not in order: the event for an
    # older generation of this object can arrive after the ledger has indexed a newer one. Acting on it would make
    # the old version current again. So the ledger's generation is read first, and an older event is acked as
    # stale - one line, nothing downloaded, nothing changed. The bytes are fetched BY GENERATION, never "whatever
    # the object holds now": an event and its bytes are one version. A generation that is gone (overwritten,
    # unversioned bucket) is the same case: the newer generation's own event indexes it.
    older = stale_generation(_db, msg.tenant_id, msg.name, msg.generation)
    if older:
        log.info(json.dumps({"event": "ingest_stale_event", "tenant": msg.tenant_id, "name": msg.name,
                             "generation": msg.generation, "ledger_generation": older, "reason": "older than the ledger"}))
        return {"status": "stale", "generation": msg.generation, "ledger_generation": older}
    blob = _gcs.bucket(msg.bucket).blob(msg.name, generation=int(msg.generation))
    try:
        content = blob.download_as_bytes()
    except NotFound:
        log.info(json.dumps({"event": "ingest_stale_event", "tenant": msg.tenant_id, "name": msg.name,
                             "generation": msg.generation, "reason": "generation gone: the object was overwritten"}))
        return {"status": "stale", "generation": msg.generation}
    doc = DocumentContract(tenant_id=msg.tenant_id, sha256=sha256_of(content),
                           gcs_uri=msg.gcs_uri, pages=0)

    if not claim(_db, doc.doc_key, doc.gcs_uri, doc.tenant_id):
        if status_of(_db, doc.doc_key) == "superseded":
            # THE UNDO (the ledger, 11 September 2026). The same bytes again, after a newer version
            # retired them: the chunks are still here, flagged. Flip them back, retire the newer
            # version in turn, and nothing is re-embedded - because nothing was ever deleted.
            # Unless a person withdrew the source (12 September 2026): the tombstone holds against a
            # redelivery and against the same bytes uploaded again; make restore SOURCE= is the way back.
            if withdrawn(_db, doc.tenant_id, doc.gcs_uri):
                log.info(json.dumps({"event": "ingest_withdrawn", "tenant": doc.tenant_id, "doc_key": doc.doc_key,
                                     "gcs_uri": doc.gcs_uri, "generation": msg.generation,
                                     "hint": "make restore SOURCE= clears the tombstone"}))
                return {"status": "withdrawn", "doc_key": doc.doc_key}
            back = reactivate(_db, doc.tenant_id, doc.gcs_uri, doc.doc_key)
            if back is not None:
                # The full profile first (12 September 2026): the ids remove_datapoints() took out go back up from
                # the rows' own vectors BEFORE the newer version leaves the tier, so a reader there never finds none.
                if INDEX_NAME:
                    reupsert(INDEX_NAME, _db, doc.tenant_id, doc.gcs_uri, doc.doc_key)
                gone = retire_previous(_db, doc.tenant_id, doc.gcs_uri, doc.doc_key, expire_at=_expire_at(RETENTION_DAYS))
                if INDEX_NAME and gone["retired_ids"]:
                    remove_datapoints(INDEX_NAME, gone["retired_ids"])
                record_source(_db, doc.tenant_id, msg.name, doc.gcs_uri, doc.doc_key, msg.generation,
                              doc.sha256, back, effective_from_of(msg.name, None),
                              reused=back, embedded=0, retired=gone["retired_chunks"],
                              embedding_model=EMBEDDING_MODEL, embedding_version=EMBEDDING_VERSION)
                fingerprint = refresh_fingerprint(_db, doc.tenant_id, "ingest_reactivated")
                log.info(json.dumps({"event": "ingest_reactivated", "tenant": doc.tenant_id,
                                     "doc_key": doc.doc_key, "chunks": back, "reused": back, "embedded": 0,
                                     "retired": gone["retired_chunks"], "retired_doc_keys": gone["retired_doc_keys"],
                                     "generation": msg.generation, "fingerprint": fingerprint}))
                return {"status": "reactivated", "doc_key": doc.doc_key, "chunks": back}
            # THE UNDO REFUSED (12 September 2026): reactivate_incomplete said why - fewer rows than the claim
            # counted, or a retire stamp past RETENTION_DAYS; the TTL policy has been at the rows. Nothing was
            # flipped and the newer version is still current. The bytes are here, so take the claim back from its
            # superseded record and ingest them as a fresh version below: the carry-over reuses every vector the
            # newer version still holds, and the swap retires it only once the new rows are whole.
            if not claim(_db, doc.doc_key, doc.gcs_uri, doc.tenant_id, retake=True):
                return {"status": "duplicate", "doc_key": doc.doc_key}
        elif status_of(_db, doc.doc_key) == "queued":
            # Handed to the batch lane by an earlier delivery and waiting for the batch job (batch.py) to take it:
            # acked as queued, so the wait is a fact in the log and not a "duplicate" that hides it.
            return {"status": "queued_batch", "doc_key": doc.doc_key}
        else:
            # Already done by an earlier delivery, or by an earlier upload of the
            # same bytes. Returning 200 ACKS the message: this is a success, not a
            # failure, and retrying it would achieve nothing.
            return {"status": "duplicate", "doc_key": doc.doc_key}

    # THE OTHER LANE'S VERSION (13 September 2026). The Module 4 notebooks seed this collection through
    # shared/documind_corpus.py - the same doc_key for the same bytes (a real Act's is its PDF's sha, not its
    # mirror's), the same ledger row - and never the claim, so the claim above is won for a version that is
    # already current. Nothing is parsed or embedded: the claim becomes its record with the rows it holds, the
    # ledger learns this generation (so the nightly walk stops planning a re-ingest), the fingerprint is
    # refreshed, and the line says what was found. The other order needs nothing: seed() skips a version the
    # lane already holds current.
    already = current_chunks(_db, doc.tenant_id, doc.gcs_uri, doc.doc_key, EMBEDDING_MODEL, EMBEDDING_VERSION)
    if already:
        prior = _db.collection("sources").document(source_id_for(doc.tenant_id, msg.name)).get()
        prior = (prior.to_dict() or {}) if prior.exists else {}
        finish(_db, doc.doc_key, already, {"reused": already, "embedded": 0}, msg.generation)
        record_source(_db, doc.tenant_id, msg.name, doc.gcs_uri, doc.doc_key, msg.generation, doc.sha256, already,
                      prior.get("effective_from"), reused=already, embedded=0, retired=0,
                      embedding_model=EMBEDDING_MODEL, embedding_version=EMBEDDING_VERSION)
        fingerprint = refresh_fingerprint(_db, doc.tenant_id, "ingest_already_current")
        log.info(json.dumps({"event": "ingest_already_current", "tenant": doc.tenant_id, "doc_key": doc.doc_key,
                             "gcs_uri": doc.gcs_uri, "generation": msg.generation, "chunks": already,
                             "reused": already, "embedded": 0, "retired": 0, "fingerprint": fingerprint,
                             "seeded_by": prior.get("generation") or "unknown"}))
        return {"status": "already_current", "doc_key": doc.doc_key, "chunks": already}

    try:
        return index_document(doc, msg, content)
    except IngestFailed:
        raise HTTPException(500, "ingest failed")


def index_document(doc: DocumentContract, msg: IngestMessage, content: bytes, lane: str = "push") -> dict:
    """The pipeline, from a claimed document to its record: media described or text parsed, chunked, DLP-scanned,
    the carry-over, the staged write, the swap, the claim, the ledger row, the fingerprint, one ingest_ok line.
    Shared by the two lanes (13 September 2026): the push handler above, inside a request's 600 s, and batch.py,
    a job with no deadline, which passes lane="batch" so the page ceiling that queued the document is not asked
    again. A failure gives the claim back (release), logs ingest_failed and raises IngestFailed; each lane decides
    what that means for it."""
    gone = {"activated": 0, "retired_doc_keys": [], "retired_ids": [], "retired_chunks": 0}
    counts = {"reused": 0, "embedded": 0}
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
            # The batch decision BEFORE Doc AI (12 September 2026): a PDF's page count comes off its page tree
            # for nothing, and a document the push lane cannot finish must not pay for OCR it will not use -
            # the first version parsed the whole file and then looked at the count. Anything that is not a
            # PDF is counted by the parser, as before.
            pages = _pdf_pages(content) if msg.content_type == "application/pdf" else None
            if lane == "push" and pages is not None and pages > MAX_INLINE_PAGES:
                doc = doc.model_copy(update={"pages": pages, "effective_from": effective_from_of(msg.name, None)})
                _enqueue_batch(doc, msg)
                return {"status": "queued_batch", "pages": pages}
            text, pages = _parse(content, msg.content_type)
            doc = doc.model_copy(update={"pages": pages,
                                        "effective_from": effective_from_of(msg.name, text)})
            if lane == "push" and pages > MAX_INLINE_PAGES:
                # A 400-page contract will not finish inside a push request's
                # timeout. Hand it to the batch lane and ack.
                _enqueue_batch(doc, msg)
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

        # THE CARRY-OVER (12 September 2026): the previous version's current chunks, matched by chunk_hash; their
        # vectors are copied and only the changed chunks are embedded. The counts go on every line below.
        vectors, counts = embed_with_carry_over(_db, doc, chunks)
        # THE SWAP. The new version is written STAGED - current=false, invisible to every reader - and then one
        # pass flips it current and retires the predecessor's rows (a flag, never a delete, expire_at set so the
        # TTL policy purges them after RETENTION_DAYS). A reader between the two steps still finds exactly one
        # version. The full profile's ANN tier follows: the new ids go up after the swap, the retired ids come out.
        mirror_to_firestore(_db, doc, chunks, vectors, staged=True, stage_expire_at=_expire_at(STAGE_HOURS / 24))
        gone = swap_versions(_db, doc.tenant_id, doc.gcs_uri, doc.doc_key,
                             expire_at=_expire_at(RETENTION_DAYS), effective_to=doc.effective_from)
        if INDEX_NAME:                       # the full profile; the lean one has no index
            upsert(INDEX_NAME, to_datapoints(doc, chunks, vectors))
            if gone["retired_ids"]:
                remove_datapoints(INDEX_NAME, gone["retired_ids"])
        # The SQL lane reads the REAL chunks (5.5, gap G9): the same rows, with the verdict
        # the scan above just produced, so pii_flag in BigQuery is this worker's - never a
        # second scanner's that could disagree.
        mirror_to_bigquery(_bigquery() if BQ_CHUNK_TABLE else None, BQ_CHUNK_TABLE, doc, chunks,
                           {f["chunk_id"] for f in findings})
        finish(_db, doc.doc_key, len(chunks), counts, msg.generation)
        record_source(_db, doc.tenant_id, msg.name, doc.gcs_uri, doc.doc_key, msg.generation,
                      doc.sha256, len(chunks), doc.effective_from,
                      reused=counts["reused"], embedded=counts["embedded"], retired=gone["retired_chunks"],
                      embedding_model=EMBEDDING_MODEL, embedding_version=EMBEDDING_VERSION)
        # The cache follows the ledger: the tenant's corpus fingerprint changes, the API sees its cache record no
        # longer matches and answers uncached, make cache rebuilds the pack from the corpus that changed (12.6, 10.2).
        fingerprint = refresh_fingerprint(_db, doc.tenant_id, "ingest_ok")
        if gone["retired_chunks"]:
            log.info(json.dumps({"event": "ingest_superseded", "tenant": doc.tenant_id,
                                 "doc_key": doc.doc_key, "gcs_uri": doc.gcs_uri,
                                 "retired_doc_keys": gone["retired_doc_keys"],
                                 "retired_chunks": gone["retired_chunks"],
                                 "expire_days": RETENTION_DAYS, "effective_to": doc.effective_from}))

        # The document is now retrievable. Record that, with who and what - the
        # upload event the audit trail is missing without it.
        audit_emit("doc.upload",
                   actor={"tenant_id": doc.tenant_id, "email": "system:ingest"},
                   target={"type": "document", "id": doc.doc_key,
                           "tenant_id": doc.tenant_id},
                   meta={"gcs_uri": doc.gcs_uri, "pages": doc.pages,
                         "chunks": len(chunks), "pii": bool(findings),
                         "kinds": sorted({c["kind"] for c in chunks}),
                         "reused": counts["reused"], "embedded": counts["embedded"],
                         "retired": gone["retired_chunks"]})
    except Exception as e:
        # Give the claim back before failing, or the retry finds the document
        # already claimed and does nothing - for ever. And SAY what failed, on the log
        # line an operator reads first: the claim document carries the same text, but
        # the first live load was diagnosed from request logs that only said 500.
        release(_db, doc.doc_key, f"{type(e).__name__}: {e}")
        log.error(json.dumps({"event": "ingest_failed", "tenant": doc.tenant_id, "lane": lane,
                              "doc_key": doc.doc_key, "gcs_uri": doc.gcs_uri,
                              "error": f"{type(e).__name__}: {e}"[:600]}))
        raise IngestFailed(f"{type(e).__name__}: {e}") from e

    log.info(json.dumps({"event": "ingest_ok", "tenant": doc.tenant_id, "lane": lane,
                         "doc_key": doc.doc_key, "chunks": len(chunks),
                         "pages": pages, "kinds": sorted({c["kind"] for c in chunks}),
                         "reused": counts["reused"], "embedded": counts["embedded"],
                         "retired": gone["retired_chunks"], "generation": msg.generation,
                         "effective_from": doc.effective_from, "fingerprint": fingerprint}))
    return {"status": "indexed", "chunks": len(chunks), "reused": counts["reused"], "embedded": counts["embedded"]}
