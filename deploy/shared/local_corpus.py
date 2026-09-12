"""DocuMind's corpus for the local lane (Rs 0): the same documents, the same chunks and the same
chunk ids that 2.3 and 4.2 put in Firestore - deploy/evals/corpus/ read through
shared/documind_corpus.py - written into the Chroma directory shared/profile.py opens when
DOCUMIND_PROFILE=local. Run once, from deploy/:

    DOCUMIND_PROFILE=local python -m shared.local_corpus [tenant]     # or: make chat-local

Idempotent - Chroma upserts by id. A second tenant is one more call with "zeta" (its own
handbook and the Code on Wages) or "globex" (an MSA and nothing else), which is how the isolation
rows in evals/golden.jsonl can be exercised for Rs 0.

Until 2026-09-05 this file typed six passages ("the same six as 6.4's CORPUS"). Modules 6-8's
notebooks still carry those six inline, and the four facts they ask about - a 60-day notice
period, 45 days of leave encashed, 90 days to terminate the MSA, an invoice for Rs 1,84,500 -
are the handbook's, the MSA's and the invoice's figures here too, so the lane answers the same
questions from the real chunks. What the six could not answer, the corpus can: thirteen real documents
(evals/real_sources.json) sit under acme, the Labour Codes under zeta, the DPDP and IT Acts under globex.
"""
from __future__ import annotations

import os
import sys

from shared import documind_corpus as dc

# The local lane has no project. The placeholder keeps source_uri in the canonical shape
# (gs://<project>-uploads/<tenant>/<file>) so a citation looks the way production's does.
PROJECT_ID = os.environ.get("GOOGLE_CLOUD_PROJECT", "documind-ai-YOUR-ID")
BATCH = 200


def chunks_for(tenant_id: str, evals_dir: str | None = None) -> list[dict]:
    """The tenant's chunks, exactly as shared/documind_corpus.py mints them for Firestore."""
    evals_dir = evals_dir or dc.find_kit(os.path.dirname(os.path.abspath(__file__)))
    out: list[dict] = []
    for doc in dc.load_documents(tenant_id, evals_dir, PROJECT_ID):
        out += dc.chunk_document(doc, tenant_id)
    return out


def seed(store=None, tenant_id: str = "acme", evals_dir: str | None = None) -> int:
    """Write the corpus for ONE tenant into the local store. Returns how many chunks."""
    if store is None:
        from shared.profile import build_store
        store = build_store()
    chunks = chunks_for(tenant_id, evals_dir)
    for i in range(0, len(chunks), BATCH):
        batch = chunks[i:i + BATCH]
        # Chroma metadata takes str/int/float/bool only: a statute window has no section, so the
        # key is absent rather than None. `page` is what the answer contract's Citation reads.
        store.add_texts(
            texts=[c["text"] for c in batch],
            metadatas=[{"tenant_id": tenant_id, "doc_type": c["doc_type"], "page": c["page_start"],
                        "source_uri": c["source_uri"], "chunk_id": c["chunk_id"],
                        **({"section": c["section"]} if c.get("section") else {})} for c in batch],
            ids=[c["chunk_id"] for c in batch],
        )
    return len(chunks)


if __name__ == "__main__":
    tenant = sys.argv[1] if len(sys.argv) > 1 else "acme"
    print(f"seeded {seed(tenant_id=tenant)} chunks for tenant {tenant!r} into the local store")
