"""Admin-side DLP: the redaction path, and a thin wrapper over the shared scanner.

The info-type list is NOT defined here. It lives in shared/pii.py, because the ingest
worker scans with it too and two lists that drift are the worst kind of bug in a
compliance control: the scan misses a type, the dashboard reports zero findings, and
both look like they are working.
"""
import os

from google.cloud import dlp_v2, firestore

from shared.pii import INDIA_INFO_TYPES, LOCATION, PROJECT, inspect

_dlp = dlp_v2.DlpServiceClient()
_fs = firestore.Client(project=PROJECT)


def inspect_and_log(chunk_id: str, tenant_id: str, text: str) -> dict:
    """Scan one chunk and record the findings - never the matched values."""
    findings = inspect(text)
    if findings:
        _fs.collection("dlp_findings").add({
            "chunk_id": chunk_id, "tenant_id": tenant_id,
            "findings": findings, "count": len(findings),
            "scanned_at": firestore.SERVER_TIMESTAMP,
        })
    return {"has_pii": bool(findings),
            "types": sorted({f["info_type"] for f in findings})}


def redact(text: str) -> str:
    """Use BEFORE sending to an external provider. Replaces each finding with its type."""
    resp = _dlp.deidentify_content(request={
        "parent": f"projects/{PROJECT}/locations/{LOCATION}",
        "inspect_config": {"info_types": INDIA_INFO_TYPES,
                           "min_likelihood": "POSSIBLE"},
        "deidentify_config": {"info_type_transformations": {
            "transformations": [{
                "primitive_transformation": {
                    "replace_with_info_type_config": {}}}]}},
        "item": {"value": text},
    })
    return resp.item.value    # "Send to [EMAIL_ADDRESS] by [DATE_OF_BIRTH]"
