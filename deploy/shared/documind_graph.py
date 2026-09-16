"""The tenant-scoped knowledge graph on Firestore - lesson 4.6's store, on the lane (13 September 2026).

Three calls and a tenant delete, over `graph_nodes` and `graph_edges` in the database the chunks already live in:
load(nodes, edges), seed(question), expand(seed_ids, hops, cap), delete_tenant(). The text between the markers is
4.6's own cells, carried verbatim - tools/check_graph_backends.py holds the notebook and this file to one text and
walks both against the same fake - so a walk that is right in the lesson is right on the lane. Built by
services/ingest/graph.py (make graph TENANT=); read by rag-api/retriever.py when RETRIEVAL_GRAPH is on or auto.
"""
from __future__ import annotations

import re

from google.cloud.firestore_v1.base_query import FieldFilter

TENANT = "acme"   # the notebook's default tenant; the lane names the tenant on every call

# --- graph: begin ---------------------------------------------------------------
_STOP = {"what", "which", "who", "whose", "how", "when", "where", "why", "is", "are", "does", "do",
         "can", "the", "if", "in", "under", "after", "before", "which acts", "what did"}

def _candidate_names(question: str) -> list:
    """Capitalised runs in the question, lower-cased - a cheap proper-noun guess, no model call.
    A run may stop short of the entity's full name ("Code on Wages" for "Code on Wages, 2019"),
    so every backend's seed() matches by CONTAINMENT: the node's name inside the question, or a
    candidate inside the node's name. An exact match on the full lower-cased name found nothing
    on the real corpus - the extracted names carry the year and the brackets."""
    runs = re.findall(r"\b[A-Z][\w&-]*(?:\s+(?:[A-Z][\w&-]*|of|on|and|for))*", question)
    return [r.lower() for r in runs if r.lower() not in _STOP and len(r) >= 5] or [question.lower()]


def _seed_match(name_lower: str, question_lower: str, candidates: list) -> bool:
    return bool(name_lower) and (name_lower in question_lower or any(c in name_lower for c in candidates))
# --- graph: end -----------------------------------------------------------------


# --- graph store: begin ---------------------------------------------------------
class FirestoreGraph:
    """Nodes and edges as documents in graph_nodes / graph_edges, keyed tenant:node_id, in the
    database the chunks already live in. A hop is one `in` query per direction over at most 30
    ids (Firestore's limit on `in`); two hops are two rounds. Nothing to provision, nothing that
    expires, the tenant predicate in every read, and a tenant delete is a batch over the
    tenant's documents - the DPDP erasure path, as a query rather than a schema property."""

    def __init__(self, db):
        self.db = db

    def load(self, nodes: dict, edges: dict, tenant_id: str = TENANT) -> None:
        batch, n = self.db.batch(), 0
        for nid, node in nodes.items():
            batch.set(self.db.collection("graph_nodes").document(f"{tenant_id}:{nid}"),
                      {"tenant_id": tenant_id, "node_id": nid, "kind": node["kind"], "name": node["name"],
                       "name_lower": node["name"].lower(), "chunk_ids": sorted(node["chunks"])})
            n += 1
            if n % 400 == 0:
                batch.commit(); batch = self.db.batch()
        for (s, d, rel), v in edges.items():
            batch.set(self.db.collection("graph_edges").document(f"{tenant_id}:{s}:{d}:{rel}"),
                      {"tenant_id": tenant_id, "node_id": s, "dst_id": d, "rel": rel,
                       "chunk_id": v["chunk_id"], "confidence": v["confidence"]})
            n += 1
            if n % 400 == 0:
                batch.commit(); batch = self.db.batch()
        batch.commit()
        print(f"{len(nodes)} nodes, {len(edges)} edges written for tenant {tenant_id} (Firestore)")

    def seed(self, question: str, tenant_id: str = TENANT, limit: int = 5) -> list:
        """One pass over the tenant's nodes (a few thousand documents at most), matched by
        containment; the most specific name first. Production would keep a name-token index."""
        cands, ql = _candidate_names(question), question.lower()
        hits = []
        for d in (self.db.collection("graph_nodes").where(filter=FieldFilter("tenant_id", "==", tenant_id))
                  .select(["node_id", "name", "kind", "name_lower"]).stream()):
            if _seed_match(d.get("name_lower") or "", ql, cands):
                hits.append({"node_id": d.get("node_id"), "name": d.get("name"), "kind": d.get("kind")})
        hits.sort(key=lambda n: -len(n["name"] or ""))
        return hits[:limit]

    def expand(self, seed_ids: list, tenant_id: str = TENANT, hops: int = 1, cap: int = 20) -> list:
        if hops not in (1, 2):
            raise ValueError("hops must be 1 or 2 - deeper walks return the whole tenant")
        frontier, seen = set(seed_ids), set(seed_ids)
        for _ in range(hops):
            nxt = set()
            ids = sorted(frontier)
            for i in range(0, len(ids), 30):                       # `in` takes at most 30 values
                for field, other in (("node_id", "dst_id"), ("dst_id", "node_id")):   # undirected, like GQL's -[e]-
                    q = (self.db.collection("graph_edges")
                         .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                         .where(filter=FieldFilter(field, "in", ids[i:i + 30])))
                    nxt |= {e.get(other) for e in q.stream()}
            frontier = nxt - seen
            seen |= nxt
        out = []
        ids = sorted(seen)
        for i in range(0, len(ids), 30):
            q = (self.db.collection("graph_nodes")
                 .where(filter=FieldFilter("tenant_id", "==", tenant_id))
                 .where(filter=FieldFilter("node_id", "in", ids[i:i + 30])))
            out += [{"node_id": d.get("node_id"), "name": d.get("name"), "kind": d.get("kind"),
                     "chunk_ids": list(d.get("chunk_ids") or [])} for d in q.stream()]
        return sorted(out, key=lambda n: n["name"])[:cap]

    def delete_tenant(self, tenant_id: str) -> None:
        for coll in ("graph_edges", "graph_nodes"):
            docs = list(self.db.collection(coll).where(filter=FieldFilter("tenant_id", "==", tenant_id)).stream())
            batch, n = self.db.batch(), 0
            for d in docs:
                batch.delete(d.reference); n += 1
                if n % 400 == 0:
                    batch.commit(); batch = self.db.batch()
            batch.commit()
            print(f"tenant {tenant_id}: {len(docs)} {coll} deleted")
# --- graph store: end -----------------------------------------------------------


# --- spanner graph: begin -------------------------------------------------------
class SpannerGraph:
    """4.6's Spanner Graph backend, on the lane (16 September 2026). The lesson's load, seed-by-containment, GQL walk
    and key-range delete (cells 19, 21, 23, 37) with the database injected instead of a notebook global, over the
    DDL spanner.tf declares: GraphNode keyed (tenant_id, node_id) with an `embedding` column, GraphEdge interleaved
    in its source node ON DELETE CASCADE, the property graph DocuMindGraph with the edge label RELATES_TO.

    And the one thing the Firestore rung cannot do: SEED BY MEANING. seed_by_vector() ranks the tenant's nodes by
    COSINE_DISTANCE between their names' embeddings and the question's, so the question no longer has to contain
    a stored name - "who signs off on a big purchase?" reaches the CFO. Exact distance over the tenant's rows: right
    for a graph of thousands of nodes; the ANN index (CREATE VECTOR INDEX, APPROX_COSINE_DISTANCE) is the step for
    millions and is deliberately not taken here. The tenant predicate is in every read. expand() returns the seeds
    too, as FirestoreGraph.expand does - their chunks are the evidence most questions need. A tenant delete is one
    key range on GraphNode; the interleave takes the edges with it in the same transaction."""

    SEED_SQL = """SELECT node_id, name, kind FROM GraphNode
               WHERE tenant_id = @tenant
                 AND (STRPOS(@question, LOWER(name)) > 0
                      OR EXISTS (SELECT 1 FROM UNNEST(@names) AS n WHERE STRPOS(LOWER(name), n) > 0))
               ORDER BY LENGTH(name) DESC
               LIMIT @limit"""
    KNN_SQL = """SELECT node_id, name, kind, COSINE_DISTANCE(embedding, @q) AS d FROM GraphNode
              WHERE tenant_id = @tenant AND embedding IS NOT NULL
              ORDER BY d
              LIMIT @k"""
    ROWS_SQL = """SELECT node_id, name, kind, chunk_ids FROM GraphNode
               WHERE tenant_id = @tenant AND node_id IN UNNEST(@ids)"""
    EXPAND_GQL = """GRAPH DocuMindGraph
    MATCH (a:GraphNode WHERE a.tenant_id = @tenant AND a.node_id IN UNNEST(@seeds))
          -[e:RELATES_TO]-{1,%d}
          (b:GraphNode WHERE b.tenant_id = @tenant)
    RETURN DISTINCT b.node_id AS node_id, b.name AS name, b.kind AS kind, b.chunk_ids AS chunk_ids
    LIMIT @cap"""

    def __init__(self, database):
        self.database = database

    @staticmethod
    def _pt():
        from google.cloud.spanner_v1 import param_types
        return param_types

    def load(self, nodes: dict, edges: dict, tenant_id: str = TENANT, embeddings: dict | None = None) -> None:
        """Idempotent: insert_or_update on the stable ids. `embeddings` maps node_id -> 768 floats (make graph
        GRAPH_BACKEND=spanner embeds the canonical names); a node without one is stored and never seeded by meaning."""
        from google.cloud.spanner_v1 import COMMIT_TIMESTAMP
        embeddings = embeddings or {}
        with self.database.batch() as batch:
            batch.insert_or_update(
                table="GraphNode",
                columns=("tenant_id", "node_id", "kind", "name", "chunk_ids", "embedding", "updated_at"),
                values=[(tenant_id, nid, n["kind"], n["name"], sorted(n["chunks"]),
                         (list(embeddings[nid]) if nid in embeddings else None), COMMIT_TIMESTAMP)
                        for nid, n in nodes.items()])
            if edges:
                batch.insert_or_update(
                    table="GraphEdge",
                    columns=("tenant_id", "node_id", "dst_id", "rel", "chunk_id", "confidence"),
                    values=[(tenant_id, s, d, rel, v["chunk_id"], float(v["confidence"]))
                            for (s, d, rel), v in edges.items()])

    def seed(self, question: str, tenant_id: str = TENANT, limit: int = 5) -> list:
        """Cell 21: the node's name inside the question, or a capitalised run of the question inside the name."""
        pt = self._pt()
        with self.database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                self.SEED_SQL,
                params={"tenant": tenant_id, "question": question.lower(), "names": _candidate_names(question), "limit": limit},
                param_types={"tenant": pt.STRING, "question": pt.STRING, "names": pt.Array(pt.STRING), "limit": pt.INT64})
            return [{"node_id": r[0], "name": r[1], "kind": r[2]} for r in rows]

    def seed_by_vector(self, vec: list, tenant_id: str = TENANT, k: int = 5, max_distance: float | None = 0.4) -> list:
        """The nodes whose names mean what the question means: COSINE_DISTANCE (0 identical, 2 opposite) over the
        tenant's stored vectors, the k nearest, and none farther than max_distance - so a question about nothing in
        the graph seeds nothing, and `auto` stays on the dense path. max_distance=None returns the k nearest whatever
        their distance: what graph.py --ask prints, so the threshold is set from numbers rather than guessed."""
        pt = self._pt()
        with self.database.snapshot() as snapshot:
            rows = snapshot.execute_sql(
                self.KNN_SQL,
                params={"tenant": tenant_id, "q": [float(x) for x in vec], "k": k},
                param_types={"tenant": pt.STRING, "q": pt.Array(pt.FLOAT32), "k": pt.INT64})
            return [{"node_id": r[0], "name": r[1], "kind": r[2], "distance": float(r[3])}
                    for r in rows if max_distance is None or float(r[3]) <= max_distance]

    def expand(self, seed_ids: list, tenant_id: str = TENANT, hops: int = 1, cap: int = 20) -> list:
        """Cell 23's GQL: one statement for one or two hops, undirected, inside the tenant, capped - plus the seeds'
        own rows first. hops is 1 or 2: deeper walks return the whole tenant, which blows the budget 4.5 defends."""
        if hops not in (1, 2):
            raise ValueError("hops must be 1 or 2 - deeper walks return the whole tenant")
        if not seed_ids:
            return []
        pt = self._pt()
        out, seen = [], set()
        with self.database.snapshot() as snapshot:
            for r in snapshot.execute_sql(
                    self.ROWS_SQL, params={"tenant": tenant_id, "ids": list(seed_ids)},
                    param_types={"tenant": pt.STRING, "ids": pt.Array(pt.STRING)}):
                if r[0] not in seen:
                    seen.add(r[0]); out.append({"node_id": r[0], "name": r[1], "kind": r[2], "chunk_ids": list(r[3] or [])})
            for r in snapshot.execute_sql(
                    self.EXPAND_GQL % hops, params={"tenant": tenant_id, "seeds": list(seed_ids), "cap": cap},
                    param_types={"tenant": pt.STRING, "seeds": pt.Array(pt.STRING), "cap": pt.INT64}):
                if r[0] not in seen:
                    seen.add(r[0]); out.append({"node_id": r[0], "name": r[1], "kind": r[2], "chunk_ids": list(r[3] or [])})
        return out[:cap]

    def delete_tenant(self, tenant_id: str) -> None:
        """Cell 37: one key range on GraphNode; the interleave cascades to GraphEdge in the same transaction."""
        from google.cloud import spanner
        from google.cloud.spanner_v1 import KeySet
        with self.database.batch() as batch:
            batch.delete("GraphNode", KeySet(ranges=[spanner.KeyRange(start_closed=[tenant_id], end_closed=[tenant_id])]))
# --- spanner graph: end ---------------------------------------------------------


# --- graph route: begin ---------------------------------------------------------
def choose_mode(question: str, seeds: list) -> str:
    """auto: use the graph only when the question is relational AND we found a seed entity.

    No model call - a classifier here would cost more than the retrieval it is choosing.
    """
    relational = re.search(
        r"\b(who|which|whose|related|relationship|connect|between|depend|owns?|reports? to|"
        r"supersed|replac|affect|impact|downstream|upstream)\b", question, re.I)
    return "graph" if (relational and seeds) else "vector"
# --- graph route: end -----------------------------------------------------------


def walk(store, question: str, tenant_id: str, hops: int = 1, cap: int = 20, vec: list | None = None,
         k: int = 5, max_distance: float | None = 0.4) -> dict:
    """One question in, on any store (16 September 2026): seeds by meaning when a question vector is given and the
    store can (SpannerGraph.seed_by_vector), by containment otherwise; then the walk; then the chunk ids."""
    if vec is not None and hasattr(store, "seed_by_vector"):
        seeds = store.seed_by_vector(vec, tenant_id, k=k, max_distance=max_distance)
    else:
        seeds = store.seed(question, tenant_id)
    if not seeds:
        return {"seeds": [], "nodes": [], "chunk_ids": []}
    nodes = store.expand([s["node_id"] for s in seeds], tenant_id, hops=hops, cap=cap)
    return {"seeds": seeds, "nodes": nodes, "chunk_ids": sorted({cid for n in nodes for cid in n["chunk_ids"]})}


def graph_chunk_ids(db, question: str, tenant_id: str, hops: int = 1, cap: int = 20) -> dict:
    """One question in: the seeds, the nodes the walk reached and the chunk ids they point at - what the API's
    retriever fetches and checks (retriever.graph_candidates), and what make graph prints as its own smoke."""
    g = FirestoreGraph(db)
    seeds = g.seed(question, tenant_id)
    if not seeds:
        return {"seeds": [], "nodes": [], "chunk_ids": []}
    nodes = g.expand([s["node_id"] for s in seeds], tenant_id, hops=hops, cap=cap)
    return {"seeds": seeds, "nodes": nodes, "chunk_ids": sorted({cid for n in nodes for cid in n["chunk_ids"]})}
