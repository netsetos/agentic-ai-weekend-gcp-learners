"""Append-only audit events, shared by every service that has something to record.

Named audit_log rather than audit so it cannot shadow anything, and the bucket handle is
LAZY: services/admin/audit.py resolves os.environ["AUDIT_BUCKET"] at import time, which
turns a missing env var into an ImportError at container start rather than a clear failure
at the first write. The ingest worker must not inherit that.
"""
import json
import os
import uuid
from datetime import datetime, timezone

from google.cloud import storage

AUDIT_ACTIONS = {
    "user.login", "user.logout",
    "doc.upload", "doc.delete", "doc.download",
    "query.submit", "query.export",
    "admin.view_tenant", "admin.rotate_key",
    "tenant.create", "tenant.suspend",
    "dlp.finding", "consent.grant", "consent.revoke",
    # 9.4's Media Studio (gap G8): emit() refuses an unregistered action on purpose, so
    # adopting services/rag-api/media.py meant adding these names FIRST.
    "media.generate", "media.transcribe",
    # The managed mirror (services/ingest/managed.py, 13 September 2026 evening): a copy of a tenant's
    # text went into, or left, a store outside the kit - the store, its region, the doc_key, the op.
    "doc.mirror",
}

_bucket = None


def _b():
    global _bucket
    if _bucket is None:
        name = os.environ.get("AUDIT_BUCKET")
        if not name:
            raise RuntimeError("AUDIT_BUCKET is not set; refusing to drop an audit event")
        _bucket = storage.Client().bucket(name)
    return _bucket


def emit(action: str, actor: dict, target: dict, meta: dict | None = None) -> str:
    """Write one event to the retention-locked bucket. Returns the event id."""
    assert action in AUDIT_ACTIONS, f"unregistered audit action: {action}"
    event = {"id": str(uuid.uuid4()),
             "ts": datetime.now(timezone.utc).isoformat(),
             "action": action, "actor": actor, "target": target, "meta": meta or {}}
    d = datetime.now(timezone.utc)
    _b().blob(f"{d.year}/{d.month:02d}/{d.day:02d}/"
              f"{actor.get('tenant_id', '_')}/{action}-{event['id']}.json").upload_from_string(
        json.dumps(event, separators=(",", ":")), content_type="application/json")
    return event["id"]
