"""Admin-side audit: the shared emitter, plus the Firestore index the dashboard reads.

emit() itself lives in shared/audit_log.py so the ingest worker writes the SAME event
shape into the SAME retention-locked bucket. What is admin-specific is the fast index:
the dashboard filters events by tenant and day, and listing a GCS prefix to do that
would be slow enough that nobody would use the page.
"""
import google.auth
import os

from google.cloud import firestore

from shared.audit_log import AUDIT_ACTIONS, emit as _emit

_fs = firestore.Client(project=(os.environ.get("GOOGLE_CLOUD_PROJECT")
                                or google.auth.default()[1]))


def emit(action: str, actor: dict, target: dict, meta: dict | None = None) -> str:
    """Write the immutable event, then index it for the dashboard (14-day TTL)."""
    event_id = _emit(action, actor, target, meta)
    _fs.collection("audit_index").document(event_id).set({
        "id": event_id, "action": action, "actor": actor,
        "target": target, "meta": meta or {},
        "ts": firestore.SERVER_TIMESTAMP,
    })
    return event_id
