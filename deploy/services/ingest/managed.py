#!/usr/bin/env python3
"""The managed mirror (P9.2, 13 September 2026): the ledger's current versions, copied into the tenant's Vertex AI
RAG Engine corpus (lesson 4.3) and / or Vertex AI Search data store (lesson 4.4) - the stores the rag_engine and
vertex_search retrieval backends read (P9.4, P9.5).

    python managed.py --project P --status [--tenant acme]          # each tenant, each store, held against the ledger (make managed-status)
    python managed.py --project P --create-corpus --tenant acme     # the tenant's RAG Engine corpus, once, by name (make rag-corpus)
    python managed.py --project P --delete-corpus --tenant acme     # and gone, with its files (make managed-stores-down; it bills storage until then)

The rule (managed-retrieval-plan-2026-09-13.md, D3): the ledger is the source of truth and a store holds current
versions only. The worker calls after_swap() once swap_versions() has made a version current - with the text it
indexed, so the store carries what the kit's rows carry and a managed context can be matched back to a row - and
after_undo() when the undo made an older version current again (its text read off its own rows: no second parse, no
Document AI call); retired() when versions leave (a retirement by the walk, a withdrawal by hand). The managed
document id is the doc_key on both stores - a Vertex AI Search Document.id, a RagFile's display name - so every call
is idempotent and a listing compares with the ledger. A tenant with no store is mirror_no_store (once), a call that
fails is mirror_failed (with the error): one line each, never a failed ingest; the walk repairs the mirror (P9.3).
MANAGED_MIRROR is off | rag_engine | vertex_search | both: the deployment's capability list, which stores exist to
mirror into. Whether a TENANT's text may go there is the tenant's data_region policy (shared/tenancy.py; the plan's
section 7, 13 September 2026 evening): `in` keeps it on the kit's rows in asia-south1, `any` permits a store outside
India, absent is `in`. The mirror asks the policy per document (policy_for, the worker's cached reader), skips a store
it forbids (mirror_policy_skipped, once per tenant and store), writes a `doc.mirror` audit event for every copy that
went into or left a store, and stamps `mirrored` on the ledger row so GET /v1/sources says where a document is held.
A delete is never refused: it is the direction the policy wants. Figure and segment chunks stay on the kit's own
index (D4): a store receives text, and only text.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile

log = logging.getLogger("documind.ingest")

MODES = ("off", "rag_engine", "vertex_search", "both")
CHUNK_SIZE, CHUNK_OVERLAP = 512, 100          # what 4.3's ManagedRAG.ingest() asks of RAG Engine
STRUCT_FIELDS = ("tenant_id", "doc_key", "source_uri", "kind", "title", "doc_type", "effective_from", "generation", "name")


def check_mode(mode: str | None) -> str:
    """MANAGED_MIRROR at startup: an unknown value is refused with the list. Nothing else is judged here since
    13 September 2026 (evening): the mode says which stores this deployment CAN mirror into; whether a tenant's text
    MAY go there is its data_region, asked per document (Mirror.permitted) - a deployment variable cannot know that
    one tenant may leave India and another may not, and the old refusal ("needs RESIDENCY=us") made it a story about
    the whole deployment."""
    mode = (mode or "off").strip().lower()
    if mode not in MODES:
        raise ValueError(f"MANAGED_MIRROR={mode!r}: one of {'|'.join(MODES)}")
    return mode


def store_id(tenant_id: str) -> str:
    """documind-{tenant}: the corpus's display name and the data store's id (both take [a-z0-9-])."""
    return "documind-" + re.sub(r"[^a-z0-9-]+", "-", tenant_id.lower()).strip("-")


def version_rows(db, tenant_id: str, doc_key: str) -> tuple[str, dict]:
    """A version as the kit's rows hold it: every current text chunk of the doc_key in the order the chunker minted
    them (the index after `#` in a worker id, the locator in a notebook id), joined by blank lines - plus the
    doc_type and effective_from the rows carry. What after_undo() mirrors and what the walk re-mirrors."""
    rows, meta = [], {}
    for snap in db.collection("chunks").where("tenant_id", "==", tenant_id).where("doc_key", "==", doc_key).stream():
        d = snap.to_dict() or {}
        if d.get("current") is False or d.get("kind", "text") != "text":
            continue
        tail = snap.id.rsplit("#", 1)[-1]
        rows.append((int(tail) if tail.isdigit() else 10 ** 9, tail, d.get("text") or ""))
        meta.setdefault("doc_type", d.get("doc_type"))
        meta.setdefault("effective_from", d.get("effective_from"))
    rows.sort()
    return "\n\n".join(t for _, _, t in rows if t.strip()), {k: v for k, v in meta.items() if v is not None}


class NoStore(Exception):
    """The tenant has no store of this kind. Stores are declared - managed.tf, make rag-corpus - never created here."""


class RagEngineStore:
    """A RAG Engine corpus per tenant (4.3): the version's text uploaded as one RagFile named by its doc_key, chunked
    the lesson's way (512 / 100). `rag` is vertexai.rag, injectable for the gate."""
    name = "rag_engine"

    def __init__(self, project: str, location: str = "us-central1", embedding_model: str = "text-embedding-005", rag=None):
        self.project, self.location, self.embedding_model = project, location, embedding_model
        self._rag = rag
        self._corpora: dict[str, str | None] = {}

    @property
    def region(self) -> str:
        """Where the corpus holds the text - the region vertexai.init() is given (serverless corpora: us-central1). What
        a tenant's data_region is held against (shared/tenancy.permits) and what the audit event records."""
        return self.location

    def rag(self):
        if self._rag is None:
            import vertexai
            from vertexai import rag
            vertexai.init(project=self.project, location=self.location)   # corpora are regional; only generation is global
            self._rag = rag
        return self._rag

    def corpus(self, tenant_id: str) -> str:
        if tenant_id not in self._corpora:
            want = store_id(tenant_id)
            self._corpora[tenant_id] = next((c.name for c in self.rag().list_corpora() if c.display_name == want), None)
        name = self._corpora[tenant_id]
        if not name:
            raise NoStore(f"no RAG Engine corpus {store_id(tenant_id)!r} in {self.location}: make rag-corpus TENANT={tenant_id}")
        return name

    def create(self, tenant_id: str) -> str:
        """Once per tenant, by display name - create_corpus does not de-duplicate (4.3's rule), and a second corpus
        would bill for ever. The kit's one declared embedding model, so the store and the rows agree."""
        rag = self.rag()
        want = store_id(tenant_id)
        for c in rag.list_corpora():
            if c.display_name == want:
                self._corpora[tenant_id] = c.name
                return c.name
        emb = rag.RagEmbeddingModelConfig(vertex_prediction_endpoint=rag.VertexPredictionEndpoint(
            publisher_model=f"publishers/google/models/{self.embedding_model}"))
        c = rag.create_corpus(display_name=want, description=f"DocuMind managed mirror, tenant {tenant_id} (services/ingest/managed.py)",
                              backend_config=rag.RagVectorDbConfig(rag_embedding_model_config=emb))
        self._corpora[tenant_id] = c.name
        return c.name

    def delete_corpus(self, tenant_id: str) -> str | None:
        """The tenant's corpus and every file in it, by display name; None when there is none. The one call that
        stops a corpus billing storage (make managed-stores-down on the full profile, make down's note on lean)."""
        rag = self.rag()
        want = store_id(tenant_id)
        name = next((c.name for c in rag.list_corpora() if c.display_name == want), None)
        if name is None:
            return None
        rag.delete_corpus(name=name, force=True)
        self._corpora.pop(tenant_id, None)
        return name

    def _files(self, corpus_name: str, doc_key: str) -> list:
        return [f for f in self.rag().list_files(corpus_name) if f.display_name == doc_key]

    def upsert(self, tenant_id: str, doc_key: str, source_uri: str, text: str, meta: dict) -> str:
        rag = self.rag()
        corpus_name = self.corpus(tenant_id)
        for f in self._files(corpus_name, doc_key):            # the same version again: one file per doc_key
            rag.delete_file(name=f.name, corpus_name=corpus_name)
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, f"{doc_key}.txt")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(text)
            f = rag.upload_file(corpus_name=corpus_name, path=path, display_name=doc_key, description=source_uri,
                                transformation_config=rag.TransformationConfig(
                                    chunking_config=rag.ChunkingConfig(chunk_size=CHUNK_SIZE, chunk_overlap=CHUNK_OVERLAP)))
        return f.name

    def delete(self, tenant_id: str, doc_key: str) -> int:
        rag = self.rag()
        corpus_name = self.corpus(tenant_id)
        gone = 0
        for f in self._files(corpus_name, doc_key):
            rag.delete_file(name=f.name, corpus_name=corpus_name)
            gone += 1
        return gone

    def listing(self, tenant_id: str) -> dict:
        corpus_name = self.corpus(tenant_id)
        return {f.display_name: {"name": f.name, "source_uri": f.description} for f in self.rag().list_files(corpus_name)}


class VertexSearchStore:
    """A Vertex AI Search data store per tenant (4.4, managed.tf): the version's text as an object in the audit bucket
    (which has no notification, so nothing ingests it; Document.content.raw_bytes has a 1 MB ceiling, an object has
    none) imported inline as one Document whose id is the doc_key and whose structData is the schema's fields.
    `clients` (de, docs, ds, gcs) is injectable for the gate."""
    name = "vertex_search"

    def __init__(self, project: str, location: str = "global", bucket: str = "", clients=None):
        self.project, self.location, self.bucket = project, location, bucket
        self._c = clients
        self._exists: dict[str, bool] = {}

    @property
    def region(self) -> str:
        """Where the data store holds the text: `global` (managed.tf) - outside India by definition."""
        return self.location

    def clients(self):
        if self._c is None:
            from types import SimpleNamespace
            from google.cloud import discoveryengine_v1 as de
            from google.cloud import storage
            self._c = SimpleNamespace(de=de, docs=de.DocumentServiceClient(), ds=de.DataStoreServiceClient(),
                                      gcs=storage.Client(project=self.project))
        return self._c

    def data_store(self, tenant_id: str) -> str:
        return (f"projects/{self.project}/locations/{self.location}/collections/default_collection"
                f"/dataStores/{store_id(tenant_id)}")

    def branch(self, tenant_id: str) -> str:
        return f"{self.data_store(tenant_id)}/branches/default_branch"

    def ensure(self, tenant_id: str) -> None:
        c = self.clients()
        if tenant_id not in self._exists:
            from google.api_core.exceptions import NotFound
            try:
                c.ds.get_data_store(name=self.data_store(tenant_id))
                self._exists[tenant_id] = True
            except NotFound:
                self._exists[tenant_id] = False
        if not self._exists[tenant_id]:
            raise NoStore(f"no Vertex AI Search data store {store_id(tenant_id)!r} in {self.location}: "
                          f"MANAGED_SEARCH=true make plan / make up declares one per tenant (managed.tf)")

    def object_name(self, tenant_id: str, doc_key: str) -> str:
        return f"search/{tenant_id}/{doc_key}.txt"

    def upsert(self, tenant_id: str, doc_key: str, source_uri: str, text: str, meta: dict) -> str:
        c = self.clients()
        self.ensure(tenant_id)
        if not self.bucket:
            raise RuntimeError("AUDIT_BUCKET is not set: the mirror keeps the version's text there")
        obj = self.object_name(tenant_id, doc_key)
        c.gcs.bucket(self.bucket).blob(obj).upload_from_string(text, content_type="text/plain; charset=utf-8")
        struct = {"tenant_id": tenant_id, "doc_key": doc_key, "source_uri": source_uri, "kind": "text",
                  "title": source_uri.rsplit("/", 1)[-1], **{k: v for k, v in meta.items() if k in STRUCT_FIELDS and v is not None}}
        doc = c.de.Document(id=doc_key, struct_data=struct,
                            content=c.de.Document.Content(uri=f"gs://{self.bucket}/{obj}", mime_type="text/plain"))
        op = c.docs.import_documents(request=c.de.ImportDocumentsRequest(
            parent=self.branch(tenant_id), inline_source=c.de.ImportDocumentsRequest.InlineSource(documents=[doc]),
            reconciliation_mode=c.de.ImportDocumentsRequest.ReconciliationMode.INCREMENTAL))
        return getattr(getattr(op, "operation", None), "name", "") or "import requested"   # an LRO: minutes; the walk verifies

    def delete(self, tenant_id: str, doc_key: str) -> int:
        c = self.clients()
        self.ensure(tenant_id)
        from google.api_core.exceptions import NotFound
        gone = 0
        try:
            c.docs.delete_document(name=f"{self.branch(tenant_id)}/documents/{doc_key}")
            gone = 1
        except NotFound:
            pass
        if self.bucket:
            try:
                c.gcs.bucket(self.bucket).blob(self.object_name(tenant_id, doc_key)).delete()
            except NotFound:
                pass
        return gone

    def listing(self, tenant_id: str) -> dict:
        c = self.clients()
        self.ensure(tenant_id)
        out = {}
        for doc in c.docs.list_documents(parent=self.branch(tenant_id)):
            sd = (c.de.Document.to_dict(doc).get("struct_data") or {}) if hasattr(c.de.Document, "to_dict") else dict(doc.struct_data or {})
            out[doc.id] = {"name": doc.name, "source_uri": sd.get("source_uri")}
        return out


class Mirror:
    """The worker's, the batch job's and the walk's one door to the stores: every call is one log line per store
    and raises nothing - an ingest is never failed by its mirror. `policy_for(tenant_id)` answers the tenant's
    data_region - the worker's cached reader over shared/tenancy.policy_for, a dict's get in the gate; without one
    nothing is permitted, the way an absent policy permits nothing. `audit` is shared/audit_log.emit unless
    injected."""

    def __init__(self, db, stores: list, mode: str = "off", policy_for=None, audit=None):
        self.db, self.stores, self.mode = db, list(stores), mode
        self.policy_for = policy_for or (lambda tenant_id: "in")
        self._audit = audit
        self._said: set[tuple] = set()

    @classmethod
    def from_env(cls, db, mode: str | None = None, policy_for=None, audit=None) -> "Mirror":
        mode = check_mode(os.environ.get("MANAGED_MIRROR", "off") if mode is None else mode)
        stores: list = []
        if mode in ("rag_engine", "both"):
            stores.append(RagEngineStore(os.environ.get("GOOGLE_CLOUD_PROJECT", ""), os.environ.get("RAG_LOCATION", "us-central1"),
                                         os.environ.get("EMBEDDING_MODEL", "text-embedding-005")))
        if mode in ("vertex_search", "both"):
            stores.append(VertexSearchStore(os.environ.get("GOOGLE_CLOUD_PROJECT", ""), os.environ.get("SEARCH_LOCATION", "global"),
                                            os.environ.get("AUDIT_BUCKET", "")))
        return cls(db, stores, mode, policy_for=policy_for, audit=audit)

    @property
    def active(self) -> bool:
        return bool(self.stores)

    def policy(self, tenant_id: str) -> str:
        """The tenant's data_region, never raised: a reader that fails answers `in` (nothing leaves), logged once."""
        try:
            return self.policy_for(tenant_id)
        except Exception as e:  # noqa: BLE001 - an unreadable policy is the strict one
            if ("policy_unreadable", tenant_id) not in self._said:
                self._said.add(("policy_unreadable", tenant_id))
                log.warning(json.dumps({"event": "mirror_policy_unreadable", "tenant": tenant_id, "error": f"{type(e).__name__}: {e}"[:200]}))
            return "in"

    def permitted(self, tenant_id: str, doc_key: str, op: str) -> list:
        """The stores the tenant's policy lets this text into: every one under `any`, only a store inside India under
        `in` (shared/tenancy.permits - none of the managed stores is there today). A forbidden store is one
        mirror_policy_skipped line per tenant and store, then silence: the skip is the policy working, not an error."""
        from shared.tenancy import permits       # shared/ ships beside the service (the Dockerfile); PYTHONPATH=. from deploy/
        policy = self.policy(tenant_id)
        out = []
        for store in self.stores:
            region = getattr(store, "region", "")
            if permits(policy, region):
                out.append(store)
            elif ("policy", store.name, tenant_id) not in self._said:
                self._said.add(("policy", store.name, tenant_id))
                log.info(json.dumps({"event": "mirror_policy_skipped", "store": store.name, "region": region, "tenant": tenant_id,
                                     "doc_key": doc_key, "op": op, "data_region": policy,
                                     "why": "the tenant's data_region keeps its text on the kit's own rows (make tenant-policy)"}))
        return out

    def _each(self, op: str, tenant_id: str, doc_key: str, fn, stores: list | None = None) -> list:
        """One call per store, one line each; the stores that confirmed come back, and each confirmation is audited."""
        done = []
        for store in (self.stores if stores is None else stores):
            try:
                result = fn(store)
            except NoStore as e:
                if (store.name, tenant_id) not in self._said:
                    self._said.add((store.name, tenant_id))
                    log.warning(json.dumps({"event": "mirror_no_store", "store": store.name, "tenant": tenant_id,
                                            "doc_key": doc_key, "op": op, "hint": str(e)}))
                continue
            except Exception as e:  # noqa: BLE001 - the mirror never fails the ingest; the walk repairs it
                log.warning(json.dumps({"event": "mirror_failed", "store": store.name, "tenant": tenant_id, "doc_key": doc_key,
                                        "op": op, "error": f"{type(e).__name__}: {e}"[:300]}))
                continue
            log.info(json.dumps({"event": "mirror_ok", "store": store.name, "tenant": tenant_id, "doc_key": doc_key,
                                 "op": op, "result": str(result)[:200]}))
            done.append(store)
            self._record(op, store, tenant_id, doc_key, result)
        return done

    def _record(self, op: str, store, tenant_id: str, doc_key: str, result) -> None:
        """doc.mirror: the audit event that a copy of a tenant's text went into, or left, a store outside the kit - the
        store, its region, the doc_key, the op - beside doc.upload in the retention bucket (shared/audit_log.py). The
        record that a border was crossed, which is what a residency question turns into once it is a policy and not
        a story. Best-effort like every line here: a failed emit is one mirror_audit_failed line, never a failed ingest."""
        try:
            if self._audit is None:
                from shared.audit_log import emit
                self._audit = emit
            self._audit("doc.mirror", actor={"tenant_id": tenant_id, "email": "system:ingest"},
                        target={"type": "document", "id": doc_key, "tenant_id": tenant_id},
                        meta={"store": store.name, "region": getattr(store, "region", ""), "op": op, "result": str(result)[:200]})
        except Exception as e:  # noqa: BLE001
            log.warning(json.dumps({"event": "mirror_audit_failed", "store": store.name, "tenant": tenant_id, "doc_key": doc_key,
                                    "op": op, "error": f"{type(e).__name__}: {e}"[:200]}))

    def upsert(self, tenant_id: str, doc_key: str, source_uri: str, text: str, meta: dict | None = None) -> dict:
        """The version's text into every store the policy permits. Returns {store: region} for the stores that
        confirmed - what after_swap() and after_undo() stamp on the ledger row; empty is a real answer."""
        if not self.stores:
            return {}
        if not text or not text.strip():
            log.info(json.dumps({"event": "mirror_skipped", "tenant": tenant_id, "doc_key": doc_key,
                                 "why": "no text: a media version stays on the kit's own index"}))
            return {}
        meta = {k: v for k, v in (meta or {}).items() if v is not None}
        stores = self.permitted(tenant_id, doc_key, "upsert")
        done = self._each("upsert", tenant_id, doc_key, lambda s: s.upsert(tenant_id, doc_key, source_uri, text, meta), stores)
        return {s.name: getattr(s, "region", "") for s in done}

    def retired(self, tenant_id: str, doc_keys, why: str = "retired") -> None:
        """Every store, whatever the policy: a delete is the direction the policy wants, and a store that never held the
        version answers NotFound (0) rather than refusing."""
        for doc_key in doc_keys:
            self._each(f"delete:{why}", tenant_id, doc_key, lambda s, k=doc_key: s.delete(tenant_id, k))

    def stamp(self, tenant_id: str, name: str | None, doc_key: str, held: dict) -> None:
        """`mirrored` on the ledger row - sources/{tenant~name}, the id idempotency.source_id_for mints - the stores that
        confirmed this version and the region each holds it in, the policy it was judged under, and when; so GET
        /v1/sources and the UI's Documents page say where a document is held. An empty dict is a real answer (a
        policy of `in`, or no store confirming): the row reads "the kit's rows only". Merged, so record_source() and
        this land in either order; best-effort like every line here."""
        if not name:
            return
        try:
            from google.cloud import firestore
            self.db.collection("sources").document(name.replace("/", "~")).set(
                {"mirrored": dict(held), "data_region": self.policy(tenant_id), "mirrored_at": firestore.SERVER_TIMESTAMP}, merge=True)
        except Exception as e:  # noqa: BLE001
            log.warning(json.dumps({"event": "mirror_stamp_failed", "tenant": tenant_id, "doc_key": doc_key,
                                    "error": f"{type(e).__name__}: {e}"[:200]}))

    def after_swap(self, doc, text: str, gone: dict, meta: dict | None = None) -> None:
        """swap_versions() just made `doc` current: the permitted stores get its text, the versions the swap retired
        leave every store, and the ledger row says which stores hold it."""
        if not self.stores:
            return
        meta = meta or {}
        held = self.upsert(doc.tenant_id, doc.doc_key, doc.gcs_uri, text,
                           {"doc_type": doc.doc_type, "effective_from": doc.effective_from, **meta})
        self.retired(doc.tenant_id, [k for k in gone.get("retired_doc_keys", []) if k != doc.doc_key], "superseded")
        self.stamp(doc.tenant_id, meta.get("name"), doc.doc_key, held)

    def after_undo(self, tenant_id: str, gcs_uri: str, doc_key: str, gone: dict, meta: dict | None = None) -> None:
        """The undo made `doc_key` current again: its text comes off its own rows; the newer version leaves."""
        if not self.stores:
            return
        meta = meta or {}
        text, rows_meta = version_rows(self.db, tenant_id, doc_key)
        held = self.upsert(tenant_id, doc_key, gcs_uri, text, {**rows_meta, **meta})
        self.retired(tenant_id, [k for k in gone.get("retired_doc_keys", []) if k != doc_key], "superseded")
        self.stamp(tenant_id, meta.get("name"), doc_key, held)


def status(db, stores: list, tenant_only: str | None = None) -> list[dict]:
    """Each tenant the ledger knows, each store: what the store holds against the ledger's current versions."""
    current: dict[str, set] = {}
    for snap in db.collection("sources").stream():
        row = snap.to_dict() or {}
        t = row.get("tenant_id")
        if not t or (tenant_only and t != tenant_only):
            continue
        current.setdefault(t, set())
        if row.get("status") == "indexed" and row.get("doc_key"):
            current[t].add(row["doc_key"])
    if tenant_only and tenant_only not in current:
        current[tenant_only] = set()
    out = []
    for tenant in sorted(current):
        for store in stores:
            line = {"tenant": tenant, "store": store.name, "ledger_current": len(current[tenant])}
            try:
                held = store.listing(tenant)
            except NoStore as e:
                out.append({**line, "status": "no store", "hint": str(e)})
                continue
            except Exception as e:  # noqa: BLE001
                out.append({**line, "status": "error", "error": f"{type(e).__name__}: {e}"[:200]})
                continue
            missing = sorted(current[tenant] - set(held))
            orphans = sorted(set(held) - current[tenant])
            out.append({**line, "held": len(held), "missing": len(missing), "orphans": len(orphans),
                        "status": "in sync" if not missing and not orphans else "drift",
                        "missing_doc_keys": missing[:5], "orphan_doc_keys": orphans[:5]})
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    ap.add_argument("--tenant")
    ap.add_argument("--status", action="store_true", help="each tenant, each store, against the ledger")
    ap.add_argument("--create-corpus", action="store_true", help="the tenant's RAG Engine corpus, once (needs --tenant)")
    ap.add_argument("--delete-corpus", action="store_true", help="the tenant's RAG Engine corpus and its files, gone (needs --tenant)")
    ap.add_argument("--mode", default=os.environ.get("MANAGED_MIRROR") or "both",
                    help="which stores --status reads: rag_engine | vertex_search | both")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    if not args.project:
        ap.error("--project is required")
    if args.create_corpus:
        if not args.tenant:
            ap.error("--create-corpus needs --tenant")
        name = RagEngineStore(args.project, os.environ.get("RAG_LOCATION", "us-central1"),
                              os.environ.get("EMBEDDING_MODEL", "text-embedding-005")).create(args.tenant)
        print(json.dumps({"event": "rag_corpus_ready", "tenant": args.tenant, "corpus": name,
                          "note": "one corpus per tenant, found by name; RAG Engine storage bills while it exists - make down names it"}))
        return 0
    if args.delete_corpus:
        if not args.tenant:
            ap.error("--delete-corpus needs --tenant")
        name = RagEngineStore(args.project, os.environ.get("RAG_LOCATION", "us-central1")).delete_corpus(args.tenant)
        print(json.dumps({"event": "rag_corpus_deleted" if name else "rag_corpus_absent", "tenant": args.tenant, "corpus": name}))
        return 0
    if args.status:
        from google.cloud import firestore
        db = firestore.Client(project=args.project)
        mode = check_mode(args.mode)
        os.environ.setdefault("GOOGLE_CLOUD_PROJECT", args.project)
        mirror = Mirror.from_env(db, mode=mode)
        for line in status(db, mirror.stores, args.tenant):
            print(json.dumps(line))
        return 0
    ap.error("one of --status, --create-corpus, --delete-corpus")
    return 2


if __name__ == "__main__":
    sys.exit(main())
