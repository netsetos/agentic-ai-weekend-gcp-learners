#!/usr/bin/env python3
"""Build a tenant's knowledge graph from its current chunks - lesson 4.6, on the lane (13 September 2026).

    python graph.py --project P --tenant acme [--backend firestore|spanner] [--limit 200] [--source hr_policy_2026.md]
                    [--rebuild] [--dry-run] [--ask "Which Acts ..."]

4.6's pipeline, as a tool: every current text chunk of the tenant (the handbook's GEN- boilerplate skipped) through
gemini-3.1-flash-lite with the lesson's GraphExtraction schema and system prompt - entities and relations STATED in
the passage, never inferred; surface forms resolved to one canonical name by the lesson's two passes (normalise, then
text-embedding-005 cosine at 0.92 - a wrong merge is worse than a duplicate node); nodes and edges built the lesson's
way (a stable id from the canonical name, the chunk ids that make citations possible, the best confidence per edge);
written through shared/documind_graph.FirestoreGraph - the same class the notebook runs - into graph_nodes and
graph_edges beside the chunks. An extraction is cached in graph_extractions/{tenant}:{chunk_id} under the chunk's
hash, so a rerun pays for new or changed chunks only. --rebuild erases the tenant's graph first; --limit is the bill
(200 chunks is a few rupees; ACME's 1,600 are priced in 4.6's Cell 12). --source keeps one document's chunks (a
substring of source_uri, 16 September 2026): reading order sorts the Acts before the handbook, so a demonstration
that wants the handbook's entities names the handbook rather than guessing a limit. --ask walks the graph for one question and
prints the seeds, the nodes and the chunk ids the API would put in front of its dense pool (RETRIEVAL_GRAPH=on|auto).
--backend spanner (16 September 2026) writes the same graph to Spanner Graph (shared/documind_graph.SpannerGraph, the
DDL in spanner.tf; SPANNER_INSTANCE / SPANNER_DATABASE name it) with each canonical name's embedding beside the node,
and --ask then seeds BY MEANING - the question's embedding against the names' - so the question need not contain a
stored name; on Firestore --ask seeds by containment, as the lesson does.
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


def load_chunks(db, tenant: str, limit: int | None, source: str | None = None) -> list[dict]:
    """The tenant's current text chunks, in reading order per source; GEN- boilerplate and media skipped.
    `source` keeps the chunks of one document (a substring of source_uri) - the whole corpus otherwise."""
    rows = []
    for snap in db.collection("chunks").where("tenant_id", "==", tenant).stream():
        x = snap.to_dict() or {}
        if x.get("current") is False or x.get("kind", "text") != "text" or (x.get("section") or "").startswith("GEN-"):
            continue
        if source and source not in (x.get("source_uri") or ""):
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
    ap.add_argument("--source", help="only the chunks whose source_uri contains this - one document's graph")
    ap.add_argument("--backend", default=os.environ.get("GRAPH_BACKEND", "firestore"), choices=("firestore", "spanner"),
                    help="where the graph lives: Firestore (the lesson's default) or Spanner Graph, which also seeds by meaning")
    ap.add_argument("--rebuild", action="store_true", help="erase the tenant's graph first")
    ap.add_argument("--dry-run", action="store_true", help="count the chunks; extract nothing")
    ap.add_argument("--ask", help="walk the graph for one question and print what the API would fetch")
    ap.add_argument("--hops", type=int, default=1)
    ap.add_argument("--seed-k", type=int, default=5, help="--ask on spanner: how many nearest names to consider")
    ap.add_argument("--seed-distance", type=float, default=0.4,
                    help="--ask on spanner: the largest cosine distance a name may have and still seed the walk (the API's GRAPH_SEED_DISTANCE)")
    args = ap.parse_args()
    # --ask is consumed as one JSON document. SDK session/HTTP logs belong on
    # stderr; build mode retains stdout events for the runbook's build log gate.
    logging.basicConfig(level=logging.INFO, format="%(message)s",
                        stream=sys.stderr if args.ask else sys.stdout)
    from google.cloud import firestore
    from shared.documind_graph import FirestoreGraph, SpannerGraph, graph_chunk_ids, walk
    db = firestore.Client(project=args.project)
    def spanner_store():
        from google.cloud import spanner
        inst = os.environ.get("SPANNER_INSTANCE", "documind-graph"); dbname = os.environ.get("SPANNER_DATABASE", "documind")
        return SpannerGraph(spanner.Client(project=args.project).instance(inst).database(dbname))

    def embedder():
        from google import genai
        from google.genai import types
        emb = genai.Client(enterprise=True, project=args.project, location=os.environ.get("EMBED_LOCATION", "us-central1"))

        def embed(names: list) -> list:
            vecs = []
            for i in range(0, len(names), 250):
                r = emb.models.embed_content(model=EMBED_MODEL, contents=names[i:i + 250],
                                             config=types.EmbedContentConfig(task_type="SEMANTIC_SIMILARITY", output_dimensionality=768))
                vecs.extend(e.values for e in r.embeddings)
            return vecs
        return embed

    if args.ask:
        if args.backend == "spanner":
            # By meaning: the question's embedding against the names', the same model and task type the build used. The
            # k nearest are printed whatever their distance, with whether each passed the threshold - so the threshold
            # the API runs with (GRAPH_SEED_DISTANCE) is set from these numbers, not guessed.
            store, vec = spanner_store(), embedder()([args.ask])[0]
            nearest = store.seed_by_vector(vec, args.tenant, k=args.seed_k, max_distance=None)
            r = walk(store, args.ask, args.tenant, hops=args.hops, vec=vec, k=args.seed_k, max_distance=args.seed_distance)
            print(json.dumps({"question": args.ask, "backend": "spanner", "seeded_by": "meaning", "seed_distance": args.seed_distance,
                              "nearest": [{"name": x["name"], "kind": x["kind"], "distance": round(x["distance"], 3),
                                           "seeded": x["distance"] <= args.seed_distance} for x in nearest],
                              "seeds": [x["name"] for x in r["seeds"]],
                              "nodes": [n["name"] for n in r["nodes"]], "chunk_ids": r["chunk_ids"]}, indent=1))
            return 0
        r = graph_chunk_ids(db, args.ask, args.tenant, hops=args.hops)
        print(json.dumps({"question": args.ask, "backend": "firestore", "seeded_by": "containment",
                          "seeds": [x["name"] for x in r["seeds"]],
                          "nodes": [n["name"] for n in r["nodes"]], "chunk_ids": r["chunk_ids"]}, indent=1))
        return 0
    chunks = load_chunks(db, args.tenant, args.limit, args.source)
    print(f"{len(chunks)} current text chunks for tenant {args.tenant!r}" + (f" from {args.source!r}" if args.source else "")
          + (f" (first {args.limit})" if args.limit else ""))
    if args.dry_run:
        return 0
    from google import genai
    gen = genai.Client(enterprise=True, project=args.project, location="global")       # generation: global only
    embed = embedder()

    extractions = extract_all(db, args.tenant, chunks, gen)
    canon_of = resolve_entities([e.name for x in extractions for e in x["graph"].entities], embed)
    nodes, edges = build_graph(extractions, canon_of)
    graph = spanner_store() if args.backend == "spanner" else FirestoreGraph(db)
    if args.rebuild:
        graph.delete_tenant(args.tenant)
    if args.backend == "spanner":
        # One vector per canonical name, beside the node: what seed_by_vector ranks. A dozen names is one call.
        ids = list(nodes)
        vecs = embed([nodes[nid]["name"] for nid in ids]) if ids else []
        graph.load(nodes, edges, args.tenant, embeddings=dict(zip(ids, vecs)))
    else:
        graph.load(nodes, edges, args.tenant)
    log.info(json.dumps({"event": "graph_built", "tenant": args.tenant, "backend": args.backend, "chunks": len(chunks),
                         "surface_forms": len(canon_of), "nodes": len(nodes), "edges": len(edges)}))
    return 0


if __name__ == "__main__":
    sys.exit(main())
