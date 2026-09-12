"""The one PII definition DocuMind scans against.

Lifted out of services/admin/dlp.py so the ingest worker and the admin dashboard cannot
look for different things. Two info-type lists that drift is the failure mode here: the
scan misses a type, the dashboard reports zero findings, and both are working correctly.

Cost note: DLP bills per byte inspected. A 50-page document is a few hundred KB, so a scan
of the whole corpus is cents - but it is not free, and it is per ingest.

Quota note (the first live corpus load, 2026-09-06): DLP also meters REQUESTS per minute,
and one request per chunk - thousands in a few minutes, from ten workers - was refused
("Number of requests per minute"). inspect_many() scans a document's chunks in as few
requests as their bytes allow (a request takes half a megabyte) and maps each finding back
to its chunk by byte offset; a refusal is retried with backoff rather than failing the
document.
"""
import bisect
import os

from google.api_core import exceptions as gexc
from google.api_core import retry as gretry
from google.cloud import dlp_v2

import google.auth

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT") or google.auth.default()[1]

# India first, because that is where DocuMind's tenants and their obligations are.
INDIA_INFO_TYPES = [
    {"name": "INDIA_AADHAAR_INDIVIDUAL"},
    {"name": "INDIA_PAN_INDIVIDUAL"},
    {"name": "INDIA_GST_INDIVIDUAL"},
    {"name": "EMAIL_ADDRESS"},
    {"name": "PHONE_NUMBER"},
    {"name": "PERSON_NAME"},
    {"name": "DATE_OF_BIRTH"},
]
MIN_LIKELIHOOD = "LIKELY"

# DLP is regional and DocuMind's data is India-resident. This is not the global
# generation endpoint and must not be "helpfully" aligned with it.
LOCATION = os.environ.get("DLP_LOCATION", "asia-south1")

# content.inspect takes at most 0.5 MiB per request; stay well inside it.
REQUEST_BYTES = 400_000
SEPARATOR = b"\n\n"

# A rate-limit refusal is retried, up to about five minutes, doubling the wait each time.
RETRY = gretry.Retry(predicate=gretry.if_exception_type(gexc.ResourceExhausted,
                                                         gexc.ServiceUnavailable,
                                                         gexc.DeadlineExceeded),
                     initial=2.0, maximum=60.0, multiplier=2.0, deadline=300.0)

_client = None


def _dlp():
    global _client
    if _client is None:                      # lazy: importing must not need credentials
        _client = dlp_v2.DlpServiceClient()
    return _client


def _inspect_bytes(data: bytes) -> list:
    """One DLP request over raw bytes; the findings carry byte offsets into `data`."""
    resp = _dlp().inspect_content(request={
        "parent": f"projects/{PROJECT}/locations/{LOCATION}",
        "inspect_config": {
            "info_types": INDIA_INFO_TYPES,
            "min_likelihood": MIN_LIKELIHOOD,
            # include_quote is False on purpose: a findings record that quotes the PAN it
            # found has moved the PAN into your audit store, where it is hardest to delete.
            "include_quote": False,
            "limits": {"max_findings_per_request": 3000},
        },
        "item": {"byte_item": {"type_": "TEXT_UTF8", "data": data}},
    }, retry=RETRY)
    return list(resp.result.findings)


def inspect_many(texts: list[str]) -> list[list[dict]]:
    """Findings for each text, in order, scanned in as few requests as the bytes allow.

    Texts are joined with a blank line into requests of at most REQUEST_BYTES; a finding's
    byte offset locates the text it fell in, and the offset returned is relative to that
    text. Never returns the matched value. A text longer than a request is scanned alone
    (DLP truncates past 0.5 MiB; a chunk is never that long).
    """
    out: list[list[dict]] = [[] for _ in texts]
    encoded = [t.encode("utf-8") for t in texts]
    i = 0
    while i < len(encoded):
        starts, parts, size, j = [], [], 0, i
        while j < len(encoded) and (not parts or size + len(SEPARATOR) + len(encoded[j]) <= REQUEST_BYTES):
            if parts:
                size += len(SEPARATOR)
            starts.append(size)
            parts.append(encoded[j])
            size += len(encoded[j])
            j += 1
        data = SEPARATOR.join(parts)
        for f in _inspect_bytes(data):
            start = f.location.byte_range.start if f.location.byte_range else 0
            k = bisect.bisect_right(starts, start) - 1
            out[i + k].append({"info_type": f.info_type.name,
                               "likelihood": f.likelihood.name,
                               "offset": start - starts[k]})
        i = j
    return out


def inspect(text: str) -> list[dict]:
    """Findings for one piece of text. Never returns the matched value."""
    return inspect_many([text])[0]


# ------------------------------------------------------------------------------ images (9.6)
# DLP reads the text in the pixels: a scanned invoice, a photographed form, a page render. The
# worker scans a figure's BYTES before it indexes the caption, because the caption is Gemini's
# description of the picture and a description of an invoice can carry the invoice's PAN in
# plain text. Same info-types, same likelihood floor, same no-quote rule as the text scan; an
# image finding locates by bounding box, so there is no byte offset to hand back.
IMAGE_TYPES = {"image/png": "IMAGE_PNG", "image/jpeg": "IMAGE_JPEG",
               "image/bmp": "IMAGE_BMP", "image/svg+xml": "IMAGE_SVG"}

# Image inspection is offered in a SHORT list of locations - global, asia, asia-southeast1, europe,
# europe-north1, us, us-central1, us-east4, us-west1 (the locations page, read 9 September 2026) - and
# asia-south1 is not on it: the first figure the lane ingested came back "400 Image inspection is not
# supported in this location" and went to the DLQ. So the pixels are scanned in the nearest location
# that reads them, Singapore, while the text scan stays in Mumbai. That is a residency compromise the
# same shape as 9.3's Chirp table, and it is a decision, not a default: DLP_IMAGE_LOCATION overrides it.
IMAGE_LOCATION = os.environ.get("DLP_IMAGE_LOCATION", "asia-southeast1")


def _shrink(data: bytes, content_type: str) -> tuple:
    """content.inspect takes at most 0.5 MiB, and a page render is bigger. A 1600 px JPEG carries
    the same PAN a 2400 px PNG does, so the bytes are re-encoded down to the limit. Pillow is in
    the worker's image (12.5 requirements); a caller without it gets the bytes back unchanged and
    DLP refuses anything over the limit - loudly, which is the right failure."""
    if len(data) <= REQUEST_BYTES:
        return data, IMAGE_TYPES[content_type]
    try:
        from PIL import Image
    except ImportError:
        return data, IMAGE_TYPES[content_type]
    import io
    im = Image.open(io.BytesIO(data)).convert("RGB")
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    for quality in (80, 60, 40, 25):
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=quality)
        if buf.tell() <= REQUEST_BYTES:
            break
    return buf.getvalue(), "IMAGE_JPEG"


def inspect_image(data: bytes, content_type: str) -> list[dict]:
    """Findings in the pixels of one image. Never returns the matched value.

    Anything that is not an image DLP can read (IMAGE_TYPES) yields no findings rather than an
    error: the worker calls this for every media kind and a video is scanned by other means.
    """
    if content_type not in IMAGE_TYPES:
        return []
    payload, kind = _shrink(data, content_type)
    resp = _dlp().inspect_content(request={
        "parent": f"projects/{PROJECT}/locations/{IMAGE_LOCATION}",     # not LOCATION: see IMAGE_LOCATION
        "inspect_config": {
            "info_types": INDIA_INFO_TYPES,
            "min_likelihood": MIN_LIKELIHOOD,
            "include_quote": False,
            "limits": {"max_findings_per_request": 3000},
        },
        "item": {"byte_item": {"type_": kind, "data": payload}},
    }, retry=RETRY)
    return [{"info_type": f.info_type.name, "likelihood": f.likelihood.name,
             "offset": 0, "where": "image", "scanned_in": IMAGE_LOCATION} for f in resp.result.findings]
