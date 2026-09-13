#!/usr/bin/env python3
"""Build a tenant's knowledge graph from its current chunks - lesson 4.6, on the lane (13 September 2026).

    python graph.py --project P --tenant acme [--limit 200] [--rebuild] [--dry-run] [--ask "Which Acts ..."]

4.6's pipeline, as a tool: every current text chunk of the tenant (the handbook's GEN- boilerplate skipped) through
gemini-3.1-flash-lite with the lesson's GraphExtraction schema and system prompt - entities and relations STATED in
the passage, never inferred; surface forms resolved to one canonical name by the lesson's two passes (normalise, then
text-embedding-005 cosine at 0.92 - a wrong merge is worse than a duplicate node); nodes and edges built the lesson's
way (a stable id from the canonical name, the chunk ids that make citations possible, the best confidence per edge);
written through shared/documind_graph.FirestoreGraph - the same class the notebook runs - into graph_nodes and
graph_edges beside the chunks. An extraction is cached in graph_extractions/{tenant}:{chunk_id} under the chunk's
hash, so a rerun pays for new or changed chunks only. --rebuild erases the tenant's graph first; --limit is the bill
(200 chunks is a few rupees; ACME's 1,600 are priced in 4.6's Cell 12). --ask walks the graph for one question and
prints the seeds, the nodes and the chunk ids the API would put in front of its dense pool (RETRIEVAL_GRAPH=on|auto).
Needs, on Cloud Shell: pip install --user google-genai==2.22.0 google-cloud-firestore==2.30.0 numpy.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time

from pydantic import BaseModel, Field
from typing import List, Literal

log = logging.getLogger("documind.ingest")

EXTRACT_MODEL = "gemini-3.1-flash-lite"   # bulk extraction: cheapest per token (4.6)
EMBED_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-005")
RESOLVE_THRESHOLD = 0.92                  # deliberately high: a wrong merge is worse than a duplicate node (4.6)


class Entity(BaseModel):
    name: str = Field(description="Surface form exactly as written in the text")
    type: Literal["person", "org", "product", "policy", "system", "location", "date"]


class Relation(BaseModel):
    source: str = Field(description="name of the source entity, exactly as in entities")
    target: str = Field(description="name of the target entity, exactly as in entities")
    rel: str = Field(description="UPPER_SNAKE verb phrase, e.g. OWNS, REPORTS_TO, SUPERSEDES")
    confidence: float = Field(ge=0, le=1)


class GraphExtraction(BaseModel):
    entities: List[Entity]
    relations: List[Relation]


EXTRACT_SYSTEM = """You build a knowledge graph from enterprise documents.
Extract only entities and relations STATED in the passage. Never infer, never add
world knowledge. Every relation's source and target must appear in entities.
If the passage states no relation, return an empty relations list."""


def normalise(name: str) -> str:
    """Cheap first pass: case, punctuation and the corporate suffixes that create duplicates (4.6)."""
    n = name.lower().strip()
    n = re.sub(r"[\.,]", "", n)
    n = re.sub(r"\b(private|pvt|limited|ltd|inc|llc|corp|corporation|co)\b", "", n)
    return re.sub(r"\s+", " ", n).strip()


def node_id(canonical_name: str) -> str:
    """Stable id from the canonical name, so re-ingesting a document updates rather than duplicates (4.6)."""
    return hashlib.sha1(canonical_name.encode("utf-8")).hexdigest()[:32]


def build_graph(extractions: list, canon_of: dict) -> tuple:
    """Nodes and edges from the extractions - pure Python, no store (4.6's build_graph)."""
    nodes, edges = {}, {}
    for x in extractions:
        cid, g = x["chunk_id"], x["graph"]
        for e in g.entities:
            canonical = canon_of.get(e.name, e.name)
            nid = node_id(canonical)
            n = nodes.setdefault(nid, {"name": canonical, "kind": e.type, "chunks": set()})
            n["chunks"].add(cid)                     # this is what makes citations possible
        for r in g.relations:
            src, dst = canon_of.get(r.source), canon_of.get(r.target)
            if not src or not dst or src == dst:     # drop dangling and self edges
                continue
            key = (node_id(src), node_id(dst), r.rel)
            prev = edges.get(key)
            if prev is None or r.confidence > prev["confidence"]:
                edges[key] = {"chunk_id": cid, "confidence": float(r.confidence)}
    return nodes, edges


def resolve_entities(names: list, embed) -> dict:
    """Map every surface form to one canonical name: exact match after normalise(), then cosine over embeddings
    for the survivors (4.6's resolve_entities). `embed(names) -> unit vectors` is the caller's."""
    import numpy as np
    canon_of, buckets = {}, {}
    for n in names:
        buckets.setdefault(normalise(n), []).append(n)
    reps = sorted(buckets)
    if not reps:
        return canon_of
    vecs = np.array(embed([buckets[k][0] for k in reps]), dtype=np.float32)
    vecs = vecs / np.linalg.norm(vecs, axis=1, keepdims=True)
    merged_into = {}
    for i in range(len(reps)):
        if reps[i] in merged_into:
            continue
        for j in range(i + 1, len(reps)):
            if reps[j] in merged_into:
                continue
            if float(vecs[i] @ vecs[j]) >= RESOLVE_THRESHOLD:
                merged_into[reps[j]] = reps[i]
    for key, surfaces in buckets.items():
        target = merged_into.get(key, key)
        canonical = buckets[target][0]
        for s in surfaces:
            canon_of[s] = canonical
    return canon_of


def load_chunks(db, tenant: str, limit: int | None) -> list[dict]:
    """The tenant's current text chunks, in reading order per source; GEN- boilerplate and media skipped."""
    rows = []
    for snap in db.collection("chunks").where("tenant_id", "==", tenant).stream():
        x = snap.to_dict() or {}
        if x.get("current") is False or x.get("kind", "text") != "text" or (x.get("section") or "").startswith("GEN-"):
            continue
        rows.append({"chunk_id": snap.id, "text": x.get("text", ""), "source_uri": x.get("source_uri", ""),
                     "chunk_hash": x.get("chunk_hash") or hashlib.sha256(re.sub(r"\s+", " ", x.get("text", "")).strip().encode()).hexdigest(),
                     "locator": x.get("locator") or ""})
    rows.sort(key=lambda r: (r["source_uri"], r["locator"], r["chunk_id"]))
    return rows[:limit] if limit else rows


def extract_all(db, tenant: str, chunks: list[dict], gen, pause: float = 0.1) -> list[dict]:
    """One typed subgraph per chunk, cached under the chunk's hash; one bad chunk never stops the build."""
    from google.genai import types
    out, fresh = [], 0
    for i, c in enumerate(chunks, 1):
        ref = db.collection("graph_extractions").document(f"{tenant}:{c['chunk_id']}".replace("/", "~"))
        snap = ref.get()
        cached = (snap.to_dict() or {}) if snap.exists else {}
        if cached.get("chunk_hash") == c["chunk_hash"]:
            out.append({"chunk_id": c["chunk_id"], "graph": GraphExtraction.model_validate(cached["graph"])})
            continue
        try:
            r = gen.models.generate_content(
                model=EXTRACT_MODEL, contents=f"Passage:\n{c['text']}",
                config=types.GenerateContentConfig(
                    system_instruction=EXTRACT_SYSTEM, response_mime_type="application/json",
                    response_schema=GraphExtraction,
                    thinking_config=types.ThinkingConfig(thinking_level="LOW")))
            g = r.parsed if isinstance(r.parsed, GraphExtraction) else GraphExtraction.model_validate_json(r.text or "{}")
        except Exception as e:  # noqa: BLE001 - one bad chunk must not stop the ingest
            log.warning(json.dumps({"event": "graph_extract_failed", "tenant": tenant, "chunk_id": c["chunk_id"],
                                    "error": f"{type(e).__name__}: {e}"[:200]}))
            continue
        ref.set({"tenant_id": tenant, "chunk_id": c["chunk_id"], "chunk_hash": c["chunk_hash"],
                 "graph": g.model_dump(), "model": EXTRACT_MODEL})
        out.append({"chunk_id": c["chunk_id"], "graph": g})
        fresh += 1
        if i % 25 == 0:
            log.info(json.dumps({"event": "graph_extract_progress", "tenant": tenant, "done": i, "of": len(chunks), "fresh": fresh}))
        time.sleep(pause)                            # stay well inside the per-minute quota
    log.info(json.dumps({"event": "graph_extracted", "tenant": tenant, "chunks": len(chunks), "extracted": len(out), "fresh": fresh}))
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.environ.get("GOOGLE_CLOUD_PROJECT"))
    ap.add_argument("--tenant", default="acme")
    ap.add_argument("--limit", type=int, help="chunks to extract, in reading order (the bill)")
    ap.add_argument("--rebuild", action="store_true", help="erase the tenant's graph first")
    ap.add_argument("--dry-run", action="store_true", help="count the chunks; extract nothing")
    ap.add_argument("--ask", help="walk the graph for one question and print what the API would fetch")
    ap.add_argument("--hops", type=int, default=1)
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout)
    from google.cloud import firestore
    from shared.documind_graph import FirestoreGraph, graph_chunk_ids
    db = firestore.Client(project=args.project)
    if args.ask:
        r = graph_chunk_ids(db, args.ask, args.tenant, hops=args.hops)
        print(json.dumps({"question": args.ask, "seeds": [s["name"] for s in r["seeds"]],
                          "nodes": [n["name"] for n in r["nodes"]], "chunk_ids": r["chunk_ids"]}, indent=1))
        return 0
    chunks = load_chunks(db, args.tenant, args.limit)
    print(f"{len(chunks)} current text chunks for tenant {args.tenant!r}" + (f" (first {args.limit})" if args.limit else ""))
    if args.dry_run:
        return 0
    from google import genai
    from google.genai import types
    gen = genai.Client(enterprise=True, project=args.project, location="global")       # generation: global only
    emb = genai.Client(enterprise=True, project=args.project, location=os.environ.get("EMBED_LOCATION", "us-central1"))

    def embed(names: list) -> list:
        vecs = []
        for i in range(0, len(names), 250):
            r = emb.models.embed_content(model=EMBED_MODEL, contents=names[i:i + 250],
                                         config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY", output_dimensionality=768))
            vecs.extend(e.values for e in r.embeddings)
        return vecs

    extractions = extract_all(db, args.tenant, chunks, gen)
    canon_of = resolve_entities([e.name for x in extractions for e in x["graph"].entities], embed)
    nodes, edges = build_graph(extractions, canon_of)
    graph = FirestoreGraph(db)
    if args.rebuild:
        graph.delete_tenant(args.tenant)
    graph.load(nodes, edges, args.tenant)
    log.info(json.dumps({"event": "graph_built", "tenant": args.tenant, "chunks": len(chunks), "surface_forms": len(canon_of),
                         "nodes": len(nodes), "edges": len(edges)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
