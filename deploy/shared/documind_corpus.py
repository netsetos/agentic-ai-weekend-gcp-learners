"""One corpus, one chunker: how every lesson from 4.1 on loads DocuMind's demo documents.

The documents live in deploy/evals/corpus/<tenant>/ (evals/README.md): three synthetic tenants'
handbooks, contracts, an invoice and a report, plus thirteen REAL documents - twelve Acts and Codes of
Parliament and the ministry's compliance handbook - held unequally across the tenants, fetched from
the publishers' own sites by evals/fetch_real.py. Lessons 4.2 and
4.5 paste the block between the kit markers below verbatim - tools/check_contract.py holds them to
it - so a Colab notebook, the local lane and the offline gate chunk the same bytes the same way and
mint the same chunk ids:

    acme:hr_policy_2026#NP-03        a handbook section; the id is the clause code in its heading
    acme:code_on_wages_2019#p7-1     a fixed window: page 7, the second window on that page
    zeta:code_on_wages_2019#p7-1     the same bytes under another tenant are another chunk
    acme:hr_policy_2026@1f3a9c2b#NP-03   the same clause in a RE-ISSUED handbook: the version rides in the id

Sections are what a handbook actually has, and a section is the unit a policy question is answered
from, so it is never split unless it is longer than a window; a PDF mirror is fixed windows per page.
The ingest worker (services/ingest/main.py) chunks by the same two rules - 2000 characters, 200 overlap,
cut on a sentence when one is near, a section per `## ` heading - so the lane and a notebook mint the
same chunk texts, and a chunk keeps its identity (its chunk_hash and its locator) when a paragraph above
it changes. An anchor in evals/golden.jsonl - a clause code or a document slug - is a substring of the
id, which is what 4.7's recall_at_k and 12.7's run_eval.py match on.

The ledger (12 September 2026, deploy/INDEXING.md): seed() is version-aware. A document's version is the
hash of its bytes; a re-issue reuses every unchanged chunk's vector by chunk_hash, lands staged, and is
swapped current in one pass while the old rows are retired with an expire_at for the TTL policy - never
deleted; the same bytes again reactivate for free. The same rules, and the same fields, as the worker.
"""
import json
import os
import re
import subprocess

# --- kit: begin ---------------------------------------------------------------
import hashlib
KIT_REPO = "https://github.com/netsetos/agentic-ai-weekend-gcp-learners"   # the learner repo carries the kit under deploy/
KIT_BRANCH = "main"   # the learner repo (public): the notebooks and the kit, deploy/, on its main branch
CHUNK_CHARS, CHUNK_OVERLAP = 2000, 200                     # services/ingest/main.py
EMBEDDING_MODEL, EMBEDDING_VERSION = "text-embedding-005", "1"   # stamped on every row; the worker reads the same pair from its environment (variables.tf)
RETENTION_DAYS = 30                                        # a retired row expires this long after it is superseded (the TTL policy in firestore_indexes.tf)
SCHEMA_VERSION = 2                                         # the row shape: doc_key/current (1); chunk_hash, locator, the embedding stamp, expire_at (2)
_SECTION = re.compile(r"^## +(.+?) *$", re.M)
_CODE = re.compile(r"^([A-Z][A-Z0-9]{0,7}(?:-[A-Z0-9]{1,6}){1,2})\b")   # NP-03, IT-SEC-04, MSA-04, GEN-014
_EFFECTIVE = re.compile(r"effective[ _-]?(?:from|date)?\s*[:=]\s*(\d{4}-\d{2}-\d{2})", re.I)   # services/ingest/contracts.py


def find_kit(start: str = ".") -> str:
    """The deploy/evals directory: beside the notebook, above it, or a clone under /content."""
    here = os.path.abspath(start)
    for _ in range(6):
        for cand in (os.path.join(here, "deploy", "evals"), os.path.join(here, "evals"), here):
            if os.path.isfile(os.path.join(cand, "manifest.json")) and os.path.isdir(os.path.join(cand, "corpus")):
                return cand
        here = os.path.dirname(here)
    clone = "/content/agentic-ai-weekend-gcp-learners"
    if not os.path.isdir(clone):
        subprocess.run(["git", "clone", "--depth", "1", "-b", KIT_BRANCH, KIT_REPO, clone], check=True)
    return os.path.join(clone, "deploy", "evals")


def load_documents(tenant: str, evals_dir: str, project_id: str) -> list:
    """Every document of one tenant that has text on disk: the synthetic .md files and the real
    Acts' pypdf mirrors. A scanned PDF with no mirror (posh_act_2013) is skipped - that one is
    lesson 4.1's, and only Document AI can read it. Each carries its VERSION - the sha256 of the
    mirror's bytes, the worker's doc_key for the same bytes - and the date it declares, if any."""
    docs = []
    for m in json.load(open(os.path.join(evals_dir, "manifest.json"), encoding="utf-8")):
        if m["tenant_id"] != tenant or not m.get("chars"):
            continue
        mirror = os.path.join(evals_dir, m["file"].rsplit(".", 1)[0] + ".md")
        if not os.path.isfile(mirror):
            continue
        raw = open(mirror, "rb").read()
        text = raw.decode("utf-8")
        dated = _EFFECTIVE.search(text[:3000])
        docs.append({"slug": m["slug"], "doc_type": m["doc_type"],
                     "source_uri": m["gcs_uri"].replace("${PROJECT_ID}", project_id),
                     "text": text, "sha256": hashlib.sha256(raw).hexdigest(),
                     "effective_from": dated.group(1) if dated else None})
    return docs


def windows(text: str) -> list:
    """The worker's chunker: fixed windows with overlap, ending on a sentence when one is nearby."""
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
            out.append(piece)
        if end >= len(text):
            break
        start = max(end - CHUNK_OVERLAP, start + 1)
    return out


def chunk_hash(text: str) -> str:
    """The chunk's identity across versions (services/ingest/contracts.py): the hash of its text with the
    whitespace collapsed. A re-wrapped paragraph is the same paragraph; a changed figure is a new chunk."""
    return hashlib.sha256(re.sub(r"\s+", " ", text).strip().encode("utf-8")).hexdigest()


def chunk_document(doc: dict, tenant: str, version: str = "") -> list:
    """Canonical chunk documents - the fields services/ingest/indexer.py writes - minus the embedding. Each carries
    its LOCATOR (the clause code or the section's ordinal; a page and its window for a PDF mirror; `preamble` for
    the text above a handbook's first heading) and its chunk_hash. The first version of a document keeps the classic
    id (acme:hr_policy_2026#NP-03); a re-issue carries its version in the id (acme:hr_policy_2026@1f3a9c2b#NP-03),
    so the retired rows stay beside the current ones and nothing is overwritten. An anchor matches either."""
    text = re.sub(r"\A\s*<!--.*?-->\s*", "", doc["text"], count=1, flags=re.S)   # a mirror's provenance header
    base = {"tenant_id": tenant, "source_uri": doc["source_uri"], "doc_type": doc["doc_type"], "kind": "text"}
    at = f"@{version}" if version else ""
    out = []
    heads = list(_SECTION.finditer(text))
    if heads:                                                  # a handbook: one chunk per section
        for k, piece in enumerate(windows(text[:heads[0].start()])):
            loc = "preamble" + (f"-{k}" if k else "")
            out.append({**base, "chunk_id": f"{tenant}:{doc['slug']}{at}#{loc}", "text": piece, "page_start": 1,
                        "section": None, "locator": loc, "chunk_hash": chunk_hash(piece)})
        for n, h in enumerate(heads):
            body = text[h.end(): heads[n + 1].start() if n + 1 < len(heads) else len(text)].strip()
            title = h.group(1).strip()
            code = _CODE.match(title)
            key = code.group(1) if code else f"s{n + 1}"
            for k, piece in enumerate(windows(f"{title}\n{body}")):
                loc = key + (f"-{k}" if k else "")
                out.append({**base, "chunk_id": f"{tenant}:{doc['slug']}{at}#{loc}", "text": piece, "page_start": 1,
                            "section": title, "locator": loc, "chunk_hash": chunk_hash(piece)})
    else:                                                      # a PDF mirror: pages split by \f
        for p, page in enumerate(text.split("\f"), 1):
            for k, piece in enumerate(windows(page)):
                loc = f"p{p}-{k}"
                out.append({**base, "chunk_id": f"{tenant}:{doc['slug']}{at}#{loc}", "text": piece, "page_start": p,
                            "locator": loc, "chunk_hash": chunk_hash(piece)})
    return out


EMBED_BATCH, EMBED_TOKENS, CHARS_PER_TOKEN = 250, 15_000, 3


def embed_batches(texts: list) -> list:
    """Batches of at most EMBED_BATCH texts AND about EMBED_TOKENS tokens. text-embedding-005 takes
    250 texts per request and 20,000 tokens across them, and a request over either limit fails
    whole; a two-thousand-character chunk is ~500 tokens, so 250 of them are ~125,000. The first
    live corpus load (6 Sept 2026) failed every long Act exactly here - the same rule now lives in
    services/ingest/indexer.py."""
    out, cur, cur_tokens = [], [], 0
    for t in texts:
        tokens = max(1, len(t) // CHARS_PER_TOKEN)
        if cur and (len(cur) >= EMBED_BATCH or cur_tokens + tokens > EMBED_TOKENS):
            out.append(cur); cur, cur_tokens = [], 0
        cur.append(t); cur_tokens += tokens
    if cur:
        out.append(cur)
    return out


def rows_of(db, tenant: str, source_uri: str, collection: str = "chunks") -> list:
    """Every row the tenant holds for one source - its version, its flags, its hash, its vector and its embedding
    stamp. One query, two equality filters, no composite index."""
    from google.cloud.firestore_v1.base_query import FieldFilter
    out = []
    for d in (db.collection(collection).where(filter=FieldFilter("tenant_id", "==", tenant))
                .where(filter=FieldFilter("source_uri", "==", source_uri)).stream()):
        x = d.to_dict() or {}
        out.append({"id": d.id, "ref": d.reference, "doc_key": x.get("doc_key"), "current": x.get("current"),
                    "staged": x.get("staged"), "chunk_hash": x.get("chunk_hash"), "embedding": x.get("embedding"),
                    "embedding_model": x.get("embedding_model"), "embedding_version": x.get("embedding_version")})
    return out


def swap_versions(db, rows: list, new_doc_key: str, retention_days: int = RETENTION_DAYS, effective_to: str = None) -> dict:
    """Visibility is a swap (services/ingest/idempotency.py, the same rule). Every row of new_doc_key becomes current -
    a staged re-issue, or the retired rows of a version uploaded again (the undo) - and then every OTHER current row
    of the source is retired: a flag, superseded_by, and expire_at = now + retention_days for the TTL policy. Never a
    delete. Batches of 400; a reader between two batches sees the new version only (the newest-per-source guard)."""
    import datetime
    from google.cloud import firestore
    expire = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=retention_days)
    activated, retired, n = 0, 0, 0
    batch = db.batch()
    for r in rows:
        if r["doc_key"] == new_doc_key and r["current"] is not True:
            fields = {"current": True, "staged": firestore.DELETE_FIELD, "expire_at": firestore.DELETE_FIELD,
                      "superseded_by": firestore.DELETE_FIELD, "superseded_at": firestore.DELETE_FIELD,
                      "effective_to": firestore.DELETE_FIELD}
            if not r.get("staged"):
                fields["reactivated_at"] = firestore.SERVER_TIMESTAMP     # the undo: a retired row, current again
            batch.update(r["ref"], fields)
            activated += 1; n += 1
        elif r["doc_key"] != new_doc_key and r["current"] is not False:
            fields = {"current": False, "superseded_by": new_doc_key, "superseded_at": firestore.SERVER_TIMESTAMP,
                      "expire_at": expire}
            if effective_to:
                fields["effective_to"] = effective_to
            batch.update(r["ref"], fields)
            retired += 1; n += 1
        if n and n % 400 == 0:                                   # a Firestore batch holds 500 writes
            batch.commit(); batch = db.batch()
    if n % 400:
        batch.commit()
    return {"activated": activated, "retired": retired}


def record_version(db, tenant: str, source_uri: str, doc_key: str, sha256: str, chunks: int, reused: int,
                   embedded: int, retired: int, effective_from: str = None) -> str:
    """The ledger row - sources/{tenant~name}: which version is current and what the reindex cost - and the tenant's
    corpus fingerprint, ledger/{tenant}: the hash of its current doc_keys, the key rag-api's cache follows. What the
    worker writes on every ingest (services/ingest/idempotency.py: record_source, refresh_fingerprint), from a notebook."""
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter
    name = source_uri.split("/", 3)[-1]                        # gs://bucket/<tenant>/<file> -> <tenant>/<file>
    db.collection("sources").document(name.replace("/", "~")).set(
        {"tenant_id": tenant, "name": name, "gcs_uri": source_uri, "doc_key": doc_key, "generation": "notebook",
         "sha256": sha256, "chunks": chunks, "reused": reused, "embedded": embedded, "retired": retired,
         "effective_from": effective_from, "status": "indexed", "embedding_model": EMBEDDING_MODEL,
         "embedding_version": EMBEDDING_VERSION, "indexed_at": firestore.SERVER_TIMESTAMP}, merge=True)
    keys = sorted((s.to_dict() or {}).get("doc_key") or "" for s in
                  db.collection("sources").where(filter=FieldFilter("tenant_id", "==", tenant))
                    .where(filter=FieldFilter("status", "==", "indexed")).stream())
    fp = hashlib.sha256("\n".join(keys).encode("utf-8")).hexdigest()[:16]
    db.collection("ledger").document(tenant).set(
        {"tenant_id": tenant, "fingerprint": fp, "versions": len(keys), "last_event": "notebook_seed",
         "updated_at": firestore.SERVER_TIMESTAMP}, merge=True)
    return fp


def seed(db, embed, tenant: str, project_id: str, evals_dir: str = None, collection: str = "chunks",
         retention_days: int = RETENTION_DAYS) -> dict:
    """Write one tenant's corpus into Firestore, version-aware and idempotent (12 September 2026).

    A document's version is the hash of its bytes - the worker's doc_key, tenant_sha256, for the same bytes.
    The tenant holds this version, current: nothing to do. Holds it retired (a later version replaced it):
    the UNDO - its rows come back current, the later version is retired, nothing is embedded. Holds another
    version: the RE-ISSUE - the new chunks are written staged, every unchanged chunk's vector reused by
    chunk_hash and only the changed ones sent to `embed`, then one swap makes them current and retires the old
    rows with expire_at. Holds rows with no version at all (4.1's Document AI chunks on a lane older than the
    ledger): left alone, never written twice. Holds nothing: the first version, written current.
    `embed(texts) -> vectors` is the notebook's batched text-embedding-005 call. Returns {slug: chunks written}
    and prints one line per version event."""
    from google.cloud import firestore
    from google.cloud.firestore_v1.vector import Vector
    evals_dir = evals_dir or find_kit()
    counts = {}
    for doc in load_documents(tenant, evals_dir, project_id):
        key = f"{tenant}_{doc['sha256']}"
        held = rows_of(db, tenant, doc["source_uri"], collection)
        live = [r for r in held if r["current"] is not False and not r.get("staged")]
        counts[doc["slug"]] = 0
        if any(r["doc_key"] == key for r in live) or (held and not any(r["doc_key"] for r in held)):
            continue                                             # this version is current, or the rows predate versions
        if any(r["doc_key"] == key and r["current"] is False for r in held):
            n = swap_versions(db, held, key, retention_days, doc.get("effective_from"))
            record_version(db, tenant, doc["source_uri"], key, doc["sha256"], n["activated"], n["activated"], 0,
                           n["retired"], doc.get("effective_from"))
            print(f"  {doc['slug']}: reactivated {n['activated']} chunks, retired {n['retired']}, embedded 0 (the undo)")
            continue
        chunks = chunk_document(doc, tenant, doc["sha256"][:8] if live else "")
        if not chunks:
            continue
        by_hash = {r["chunk_hash"]: r["embedding"] for r in live
                   if r["chunk_hash"] and r["embedding"] is not None
                   and r["embedding_model"] == EMBEDDING_MODEL and str(r["embedding_version"]) == EMBEDDING_VERSION}
        vectors = [by_hash.get(c["chunk_hash"]) for c in chunks]           # the carry-over: reused by hash
        misses = [c["text"] for c, v in zip(chunks, vectors) if v is None]
        fresh = []
        for texts in embed_batches(misses):                                  # 250 texts AND 20,000 tokens per request
            fresh += embed(texts)
        it = iter(fresh)
        vectors = [list(v) if v is not None else next(it) for v in vectors]
        batch, n = db.batch(), 0
        for c, v in zip(chunks, vectors):
            row = {**c, "doc_key": key, "current": not live, "embedding": Vector(v),
                   "embedding_model": EMBEDDING_MODEL, "embedding_version": EMBEDDING_VERSION,
                   "schema_version": SCHEMA_VERSION, "indexed_at": firestore.SERVER_TIMESTAMP,
                   "processed_at": firestore.SERVER_TIMESTAMP}
            if doc.get("effective_from"):
                row["effective_from"] = doc["effective_from"]
            if live:
                row["staged"] = True                                 # a re-issue lands invisible; the swap makes it current
            batch.set(db.collection(collection).document(c["chunk_id"]), row)
            n += 1
            if n % 400 == 0:                                         # a Firestore batch holds 500 writes
                batch.commit()
                batch = db.batch()
        batch.commit()
        retired = 0
        if live:
            retired = swap_versions(db, rows_of(db, tenant, doc["source_uri"], collection), key, retention_days,
                                    doc.get("effective_from"))["retired"]
            print(f"  {doc['slug']}: revision {doc['sha256'][:8]}: {len(chunks) - len(misses)} chunks reused by hash, "
                  f"{len(misses)} embedded, {retired} retired")
        record_version(db, tenant, doc["source_uri"], key, doc["sha256"], len(chunks), len(chunks) - len(misses),
                       len(misses), retired, doc.get("effective_from"))
        counts[doc["slug"]] = len(chunks)
    return counts
# --- kit: end -----------------------------------------------------------------


def kit_block() -> str:
    """The text the notebooks must carry verbatim (tools/check_contract.py compares against this)."""
    s = open(__file__, encoding="utf-8").read()
    i, j = s.index("# --- kit: begin"), s.index("# --- kit: end")
    return s[i:j]


if __name__ == "__main__":
    import sys
    tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
    evals = find_kit(os.path.dirname(os.path.abspath(__file__)))
    docs = load_documents(tenant, evals, "documind-ai-YOUR-ID")
    total = 0
    for d in docs:
        ch = chunk_document(d, tenant)
        total += len(ch)
        print(f"{d['slug']:38} {d['doc_type']:9} {len(d['text']):8,d} chars -> {len(ch):4d} chunks  "
              f"first id {ch[0]['chunk_id'] if ch else '-'}  version {d['sha256'][:8]}")
    print(f"{len(docs)} documents, {total} chunks for tenant {tenant!r}")
