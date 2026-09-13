"""Media Studio endpoints. Lesson 9.4's server half, adopted (gap G8); the upload door moved to the
uploads bucket when Module 9 joined the lane (9 September 2026).

Generation is global; storage is asia-south1. Two things this file does that the notebook
version did not: it checks the tenant ROSTER before spending on a tenant's behalf (the same
enforce_membership every query passes), and it writes a USAGE row - event=media,
modality=image, cost_usd - in the shape tenant_daily reads, so media spend per tenant is a
column and not an estimate. The audit row (who generated what, prompt hashed, never stored)
is written through the one shared audit writer, whose registry names media.generate.

The signed upload URL points at the UPLOADS bucket, under the tenant's prefix. It used to point
at the media bucket, which has no object.finalized notification (eventarc.tf watches uploads
only), so a video uploaded the way 9.4 teaches was stored, billed for thirty days and never
indexed. Under `{tenant}/` in the uploads bucket the same PUT is an ingest: the worker keys its
media branch on the content type the notification carries (12.5, MEDIA_TYPES), describes the
image or the video with Gemini, and the caption or the segments join the same `chunks`
collection every retrieve() reads. Media is a document.

Signing on Cloud Run: the service's credential is a token, so generate_signed_url is handed the
service's email and access token and signs through the IAM signBlob API (_signing_kwargs below),
which needs iam.serviceAccountTokenCreator on ITSELF (sa.tf: api_self_impersonate, the grant the
UI's account already had for its citation URLs). And the URL is authorised AS its signer, so the
API's account also holds objectCreator on the uploads bucket (storage.tf: api_uploads).
"""
import hashlib
import json
import logging
import os
import time
from datetime import timedelta

import google.auth
import google.auth.credentials
import google.auth.transport.requests
from fastapi import APIRouter, Depends, HTTPException
from google import genai
from google.genai import types
from google.cloud import storage
from pydantic import BaseModel, Field

from shared import audit_log
from auth import enforce_membership, verify_iap

router = APIRouter(prefix="/v1/media", tags=["media"])
log = logging.getLogger("documind-api")   # the logger main.py writes usage rows on

PROJECT = os.environ["GOOGLE_CLOUD_PROJECT"]
BUCKET = os.environ.get("MEDIA_BUCKET", f"{PROJECT}-media")            # storage.tf's media bucket: generated assets
UPLOAD_BUCKET = os.environ.get("UPLOAD_BUCKET", f"{PROJECT}-uploads")  # storage.tf's uploads bucket: the one with a notification
DEMO_MODE = os.getenv("DEMO_MODE") == "1"
# The audit row is the record (the media bucket is a cache), and shared/audit_log.py refuses to drop an
# event when AUDIT_BUCKET is unset. The first live generate (9 September 2026) found the API had never
# been given the bucket - only the worker and the admin had - and raised AFTER storing the image: a
# spend with no record. So the route checks before it spends, and 12.2's DEPLOY names the bucket.
AUDIT_BUCKET = os.environ.get("AUDIT_BUCKET", "")
IMAGE_MODEL = "gemini-3.1-flash-image"
IMAGE_USD = 0.039        # per image, verified 2026-09-04 (9.4) - re-verify on the pricing page
# What the ingest worker can turn into chunks (12.5: MEDIA_TYPES plus the document parser). A type
# outside this list would be stored and then fail in the worker, five deliveries later, in the DLQ.
UPLOAD_TYPES = {"image/png", "image/jpeg", "video/mp4", "audio/mpeg", "application/pdf", "text/markdown", "text/plain"}

_client = genai.Client(enterprise=True, project=PROJECT, location="global")
_gcs = storage.Client()

# SIGNING ON CLOUD RUN. The service's credential is a token from the metadata server, not a key, and
# generate_signed_url will not sign with it: "you need a private key to sign credentials" (the first
# live upload URL, 9 September 2026), with iam.serviceAccountTokenCreator granted and unused. Handed
# the service's email and access token instead, the library signs through the IAM signBlob API - the
# path that grant exists for. A credential that can sign itself (a key file, an impersonated
# credential in a notebook) needs nothing. services/frontend/citations.py carries the same twelve lines: the frontend image
# has no shared/ to import them from, and the gate holds the two copies to one shape.
def _signing_kwargs() -> dict:
    creds, _ = google.auth.default()
    if isinstance(creds, google.auth.credentials.Signing):
        return {}
    creds.refresh(google.auth.transport.requests.Request())
    return {"service_account_email": creds.service_account_email, "access_token": creds.token}


class GenerateIn(BaseModel):
    prompt: str = Field(min_length=1, max_length=2000)
    tenant_id: str = Field(min_length=1)


def _usage(tenant_id: str, email: str, cost_usd: float, cached: bool, latency_ms: int) -> dict:
    """The media row. Same field names as usage_row() in main.py where they overlap, so
    tenant_daily's SUM(cost_usd) and GROUP BY modality need no special case."""
    return {"event": "media", "surface": "media", "tenant": tenant_id, "user": email,
            "modality": "image", "model": IMAGE_MODEL, "tokens_in": 0, "tokens_out": 0,
            "cached_tokens": 0, "cost_usd": 0.0 if cached else cost_usd, "cached": cached,
            "latency_ms": latency_ms, "answerable": True, "unanswerable_flag": 0,
            # The stage clocks the API row carries (main.py usage_row): an image has no retrieval and no pool, and
            # its whole latency is the generate stage - so tenant_daily's p95 per stage holds for modality=image too.
            "retrieve_ms": 0, "rerank_ms": 0, "generate_ms": latency_ms, "pool": 0, "rerank_fallback": 0,
            "confidence": "high", "model_backend": "vertex", "prompt_version": "media-v1",
            # no retrieval either (13 September 2026): no store served, no managed share, no policy fallback
            "retrieval_mode": "none", "retrieval_backend": "none", "managed_chunks": 0, "policy_fallback": 0, "brain": "ui"}


@router.post("/generate")
def generate(body: GenerateIn, user=Depends(verify_iap)):
    enforce_membership(user["email"], body.tenant_id)
    if not AUDIT_BUCKET:
        raise HTTPException(503, "AUDIT_BUCKET is not set on this service: refusing to generate what cannot be recorded")
    t0 = time.time()
    key = hashlib.sha256(body.prompt.encode()).hexdigest()[:32]
    blob = _gcs.bucket(BUCKET).blob(f"{body.tenant_id}/gen/{key}.png")

    # DEMO_MODE serves the cached asset instead of paying for it again. A live
    # demo re-running the same prompt eight times is eight bills and eight
    # chances for the network to embarrass you.
    if DEMO_MODE and blob.exists():
        log.info(json.dumps(_usage(body.tenant_id, user["email"], IMAGE_USD, True,
                                   int((time.time() - t0) * 1000))))
        return {"blob": blob.name, "bucket": BUCKET, "cached": True}

    resp = _client.models.generate_content(
        model=IMAGE_MODEL,
        contents=body.prompt,
        config=types.GenerateContentConfig(response_modalities=["TEXT", "IMAGE"]))

    png = next((p.inline_data.data for p in resp.candidates[0].content.parts
                if p.inline_data), None)
    if png is None:
        raise HTTPException(502, "model returned no image part")
    blob.upload_from_string(png, content_type="image/png")

    audit_log.emit(
        action="media.generate",
        actor={"tenant_id": body.tenant_id, "user_email": user["email"]},
        target={"bucket": BUCKET, "blob": blob.name},
        # prompt_sha, not prompt: the audit bucket is retention-LOCKED for five years.
        meta={"model": IMAGE_MODEL, "synthid": True, "prompt_sha": key})
    log.info(json.dumps(_usage(body.tenant_id, user["email"], IMAGE_USD, False,
                               int((time.time() - t0) * 1000))))
    return {"blob": blob.name, "bucket": BUCKET, "cached": False}


@router.post("/upload-url")
def upload_url(filename: str, content_type: str, tenant_id: str,
               user=Depends(verify_iap)):
    """Past 32 MB the browser must PUT straight to GCS - into the bucket the worker watches."""
    enforce_membership(user["email"], tenant_id)
    if "/" in filename or ".." in filename or not filename:
        raise HTTPException(400, "filename must be a bare name")
    if content_type not in UPLOAD_TYPES:
        raise HTTPException(400, f"content_type must be one of {sorted(UPLOAD_TYPES)}")
    blob = _gcs.bucket(UPLOAD_BUCKET).blob(f"{tenant_id}/{filename}")
    return {"url": blob.generate_signed_url(version="v4", method="PUT",
                                            expiration=timedelta(minutes=15),
                                            content_type=content_type, **_signing_kwargs()),
            "bucket": UPLOAD_BUCKET, "blob": blob.name,
            "note": "PUT the bytes with exactly this Content-Type; the object.finalized notification "
                    "hands it to documind-ingest, and list_documents (MCP) shows it indexed."}
