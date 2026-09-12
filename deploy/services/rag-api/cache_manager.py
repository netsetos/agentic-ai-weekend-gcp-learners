"""Per-tenant explicit Gemini caches for rag-api (lesson 4.5).

State lives in Firestore `tenant_caches/{tenant_id}`:
  cache_name, location ('global' | regional), model, tokens, expire_time, created_at,
  version (the corpus manifest's hash), corpus_fingerprint (the ledger's, 12 September 2026).
Explicit caches have no maximum TTL, so every cache is created with one and a Cloud
Scheduler job (12.6) calls `refresh()` only while the tenant is active.

The cache follows the ledger. The ingest worker re-computes the tenant's corpus fingerprint
(ledger/{tenant}) on every reindex, retirement and reactivation; a record packed from another
fingerprint is not attached to a request - the answer runs uncached, the log says cache_stale,
and `make cache` packs the corpus that changed. Nothing deletes the record for that.
"""
from __future__ import annotations

import datetime as dt
import json
import logging
import os

from google import genai
from google.genai import types
from google.cloud import firestore

MODEL = os.environ.get("GENERATOR_MODEL", "gemini-3.6-flash")
MIN_CACHE_TOKENS = 4_096          # Gemini 3 family minimum for explicit caches
DEFAULT_TTL_S = int(os.environ.get("CACHE_TTL_S", "3600"))
log = logging.getLogger("documind-api")


class TenantCacheManager:
    def __init__(self, project_id: str, region: str = "us-central1", db: firestore.Client | None = None):
        self.global_client = genai.Client(enterprise=True, project=project_id, location="global")
        self.regional_client = genai.Client(enterprise=True, project=project_id, location=region)
        self.db = db or firestore.Client(project=project_id)

    def _client(self, location: str) -> genai.Client:
        return self.global_client if location == "global" else self.regional_client

    def _doc(self, tenant_id: str):
        return self.db.collection("tenant_caches").document(tenant_id)

    def ledger_fingerprint(self, tenant_id: str) -> str | None:
        """The ledger's corpus fingerprint for the tenant (ledger/{tenant}, written by the ingest worker), or None on
        a lane that has never reindexed."""
        snap = self.db.collection("ledger").document(tenant_id).get()
        return (snap.to_dict() or {}).get("fingerprint") if snap.exists else None

    def create(self, tenant_id: str, system_instruction: str, pack: str, ttl_s: int = DEFAULT_TTL_S,
               version: str = "", fingerprint: str = "") -> dict:
        """`version` is the corpus manifest's hash (cache_admin.py) and `fingerprint` the ledger's corpus
        fingerprint at packing time: a changed corpus is a new cache, never a stale one."""
        cfg = types.CreateCachedContentConfig(
            display_name=f"documind-{tenant_id}", system_instruction=system_instruction,
            contents=[types.Content(role="user", parts=[types.Part.from_text(text=pack)])], ttl=f"{ttl_s}s")
        # Caches are documented on `global` since 2026-09-01; keep the regional path until the
        # Phase-0 live test is recorded in CLAUDE.md.
        try:
            cache, where = self.global_client.caches.create(model=MODEL, config=cfg), "global"
        except Exception:
            cache, where = self.regional_client.caches.create(model=MODEL, config=cfg), self.regional_client._api_client.location
        rec = {"tenant_id": tenant_id, "cache_name": cache.name, "location": where, "model": MODEL,
               "tokens": cache.usage_metadata.total_token_count, "expire_time": cache.expire_time,
               "version": version, "corpus_fingerprint": fingerprint or self.ledger_fingerprint(tenant_id) or "",
               "created_at": firestore.SERVER_TIMESTAMP}
        self._doc(tenant_id).set(rec)
        return rec

    def get(self, tenant_id: str, min_remaining_s: int = 120) -> dict | None:
        snap = self._doc(tenant_id).get()
        if not snap.exists:
            return None
        rec = snap.to_dict()
        if rec["expire_time"] <= dt.datetime.now(dt.timezone.utc) + dt.timedelta(seconds=min_remaining_s):
            return None
        return rec

    def stale_against(self, rec: dict, tenant_id: str) -> str | None:
        """The ledger's fingerprint when the record was packed from another one - the cache is stale, the corpus
        moved. None when they agree, when the record predates the fingerprint, or when the tenant has no ledger
        row yet (a lane before its first reindex): a record nobody can prove stale is trusted, as before."""
        have = rec.get("corpus_fingerprint")
        if not have:
            return None
        now = self.ledger_fingerprint(tenant_id)
        return now if now and now != have else None

    def refresh(self, tenant_id: str, ttl_s: int = DEFAULT_TTL_S) -> dict | None:
        rec = self.get(tenant_id, min_remaining_s=0)
        if not rec:
            return None
        c = self._client(rec["location"]).caches.update(
            name=rec["cache_name"], config=types.UpdateCachedContentConfig(ttl=f"{ttl_s}s"))
        self._doc(tenant_id).update({"expire_time": c.expire_time})
        rec["expire_time"] = c.expire_time
        return rec

    def delete(self, tenant_id: str) -> None:
        snap = self._doc(tenant_id).get()
        if snap.exists:
            rec = snap.to_dict()
            try:
                self._client(rec["location"]).caches.delete(name=rec["cache_name"])
            except Exception:
                pass                      # already expired
            self._doc(tenant_id).delete()

    def generate_config_kwargs(self, tenant_id: str, model: str | None = None) -> dict:
        """Kwargs to splat into GenerateContentConfig; empty when no live cache exists - or when the
        request's model is not the cache's (a cache belongs to one model; a tuned endpoint or a routed
        tier must not be handed gemini-3.6-flash's cache, which the API would refuse) - or when the corpus
        moved past the fingerprint the cache was packed from (12 September 2026): one Firestore read, and
        a stale pack is never the context an answer is grounded in."""
        rec = self.get(tenant_id)
        if not rec or (model and rec.get("model") != model):
            return {}
        moved = self.stale_against(rec, tenant_id)
        if moved:
            log.info(json.dumps({"event": "cache_stale", "tenant": tenant_id, "cache_fingerprint": rec.get("corpus_fingerprint"),
                                 "ledger_fingerprint": moved, "action": "answering uncached; make cache TENANT= rebuilds the pack"}))
            return {}
        return {"cached_content": rec["cache_name"]}
