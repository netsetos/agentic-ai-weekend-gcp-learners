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

Fixed windows are the ingest worker's (services/ingest/main.py: 2000 characters, 200 overlap, cut
on a sentence when one is near). Sections are what a handbook actually has, and a section is the
unit a policy question is answered from, so it is never split unless it is longer than a window.
An anchor in evals/golden.jsonl - a clause code or a document slug - is a substring of the id,
which is what 4.7's recall_at_k and 12.7's run_eval.py match on.
"""
import json
import os
import re
import subprocess

# --- kit: begin ---------------------------------------------------------------
KIT_REPO = "https://github.com/netsetos/agentic-ai-weekend-gcp-learners"   # the learner repo carries the kit under deploy/
KIT_BRANCH = "main"   # the learner repo (public): the notebooks and the kit, deploy/, on its main branch
CHUNK_CHARS, CHUNK_OVERLAP = 2000, 200                     # services/ingest/main.py
_SECTION = re.compile(r"^## +(.+?) *$", re.M)
_CODE = re.compile(r"^([A-Z][A-Z0-9]{0,7}(?:-[A-Z0-9]{1,6}){1,2})\b")   # NP-03, IT-SEC-04, MSA-04, GEN-014


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
    lesson 4.1's, and only Document AI can read it."""
    docs = []
    for m in json.load(open(os.path.join(evals_dir, "manifest.json"), encoding="utf-8")):
        if m["tenant_id"] != tenant or not m.get("chars"):
            continue
        mirror = os.path.join(evals_dir, m["file"].rsplit(".", 1)[0] + ".md")
        if not os.path.isfile(mirror):
            continue
        docs.append({"slug": m["slug"], "doc_type": m["doc_type"],
                     "source_uri": m["gcs_uri"].replace("${PROJECT_ID}", project_id),
                     "text": open(mirror, encoding="utf-8").read()})
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


def chunk_document(doc: dict, tenant: str) -> list:
    """Canonical chunk documents - the fields services/ingest/indexer.py writes - minus the embedding."""
    text = re.sub(r"\A\s*<!--.*?-->\s*", "", doc["text"], count=1, flags=re.S)   # a mirror's provenance header
    base = {"tenant_id": tenant, "source_uri": doc["source_uri"], "doc_type": doc["doc_type"], "kind": "text"}
    out = []
    heads = list(_SECTION.finditer(text))
    if heads:                                                  # a handbook: one chunk per section
        for n, h in enumerate(heads):
            body = text[h.end(): heads[n + 1].start() if n + 1 < len(heads) else len(text)].strip()
            title = h.group(1).strip()
            code = _CODE.match(title)
            key = code.group(1) if code else f"s{n}"
            for k, piece in enumerate(windows(f"{title}\n{body}")):
                cid = f"{tenant}:{doc['slug']}#{key}" + (f"-{k}" if k else "")
                out.append({**base, "chunk_id": cid, "text": piece, "page_start": 1, "section": title})
    else:                                                      # a PDF mirror: pages split by \f
        for p, page in enumerate(text.split("\f"), 1):
            for k, piece in enumerate(windows(page)):
                out.append({**base, "chunk_id": f"{tenant}:{doc['slug']}#p{p}-{k}",
                            "text": piece, "page_start": p})
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


def seed(db, embed, tenant: str, project_id: str, evals_dir: str = None, collection: str = "chunks") -> dict:
    """Write one tenant's corpus into Firestore, idempotently. A document that already has a chunk
    under this tenant - written by this loader or by 4.1's Document AI path - is skipped, so two
    lessons never hold two copies of one document. `embed(texts) -> vectors` is the notebook's
    batched text-embedding-005 call. Returns {slug: chunks written}."""
    from google.cloud import firestore
    from google.cloud.firestore_v1.base_query import FieldFilter
    from google.cloud.firestore_v1.vector import Vector
    evals_dir = evals_dir or find_kit()
    counts = {}
    for doc in load_documents(tenant, evals_dir, project_id):
        chunks = chunk_document(doc, tenant)
        present = (db.collection(collection).where(filter=FieldFilter("tenant_id", "==", tenant))
                   .where(filter=FieldFilter("source_uri", "==", doc["source_uri"])).limit(1).get())
        if not chunks or present:
            counts[doc["slug"]] = 0
            continue
        vectors = []
        for batch in embed_batches([c["text"] for c in chunks]):   # 250 texts AND 20,000 tokens per request
            vectors += embed(batch)
        batch, n = db.batch(), 0
        for c, v in zip(chunks, vectors):
            batch.set(db.collection(collection).document(c["chunk_id"]),
                      {**c, "embedding": Vector(v), "processed_at": firestore.SERVER_TIMESTAMP})
            n += 1
            if n % 400 == 0:                                     # a Firestore batch holds 500 writes
                batch.commit()
                batch = db.batch()
        batch.commit()
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
              f"first id {ch[0]['chunk_id'] if ch else '-'}")
    print(f"{len(docs)} documents, {total} chunks for tenant {tenant!r}")
