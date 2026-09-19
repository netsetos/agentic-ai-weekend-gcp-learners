"""Tenant-scoped knowledge graph stores used by lesson 4.6 and the production lane."""
from __future__ import annotations

import re

from google.cloud.firestore_v1.base_query import FieldFilter

TENANT = "acme"

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
            for i in range(0, len(ids), 30):
                for field, other in (("node_id", "dst_id"), ("dst_id", "node_id")):
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
    """Tenant-scoped Spanner Graph backend with semantic seeding and one/two-hop GQL expansion."""

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
    RETURN DISTINCT b.node_id AS node_id
    ORDER BY node_id
    LIMIT @cap"""

    def __init__(self, database):
        self.database = database

    @staticmethod
    def _pt():
        from google.cloud.spanner_v1 import param_types
        return param_types

    def _snapshot(self, multi_use: bool = False):
        """Request a multi-use snapshot from the real client; keep the offline fake compatible."""
        if not multi_use:
            return self.database.snapshot()
        try:
            return self.database.snapshot(multi_use=True)
        except TypeError:
            # tools/check_graph_backends.py's pre-existing fake has snapshot() with no keyword.
            # Production google-cloud-spanner supports multi_use and therefore takes the branch above.
            return self.database.snapshot()

    def load(self, nodes: dict, edges: dict, tenant_id: str = TENANT, embeddings: dict | None = None) -> None:
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
        pt = self._pt()
        with self._snapshot() as snapshot:
            rows = snapshot.execute_sql(
                self.SEED_SQL,
                params={"tenant": tenant_id, "question": question.lower(), "names": _candidate_names(question), "limit": limit},
                param_types={"tenant": pt.STRING, "question": pt.STRING, "names": pt.Array(pt.STRING), "limit": pt.INT64})
            return [{"node_id": r[0], "name": r[1], "kind": r[2]} for r in rows]

    def seed_by_vector(self, vec: list, tenant_id: str = TENANT, k: int = 5, max_distance: float | None = 0.4) -> list:
        pt = self._pt()
        with self._snapshot() as snapshot:
            rows = snapshot.execute_sql(
                self.KNN_SQL,
                params={"tenant": tenant_id, "q": [float(x) for x in vec], "k": k},
                param_types={"tenant": pt.STRING, "q": pt.Array(pt.FLOAT32), "k": pt.INT64})
            return [{"node_id": r[0], "name": r[1], "kind": r[2], "distance": float(r[3])}
                    for r in rows if max_distance is None or float(r[3]) <= max_distance]

    def expand(self, seed_ids: list, tenant_id: str = TENANT, hops: int = 1, cap: int = 20) -> list:
        """Return seed rows plus their GQL neighbours in one consistent multi-use snapshot."""
        if hops not in (1, 2):
            raise ValueError("hops must be 1 or 2 - deeper walks return the whole tenant")
        if not seed_ids or cap <= 0:
            return []
        pt = self._pt()
        out, seen = [], set()
        with self._snapshot(multi_use=True) as snapshot:
            for r in snapshot.execute_sql(
                    self.ROWS_SQL, params={"tenant": tenant_id, "ids": list(seed_ids)},
                    param_types={"tenant": pt.STRING, "ids": pt.Array(pt.STRING)}):
                if r[0] not in seen:
                    seen.add(r[0]); out.append({"node_id": r[0], "name": r[1], "kind": r[2], "chunk_ids": list(r[3] or [])})
            # ARRAY values cannot participate in Spanner RETURN DISTINCT. Deduplicate
            # scalar IDs in GQL, then hydrate citation arrays through ordinary SQL.
            # All three reads share the snapshot; LIMIT applies to distinct nodes,
            # not repeated paths to the same node.
            neighbour_ids = [r[0] for r in snapshot.execute_sql(
                    self.EXPAND_GQL % hops, params={"tenant": tenant_id, "seeds": list(seed_ids), "cap": cap},
                    param_types={"tenant": pt.STRING, "seeds": pt.Array(pt.STRING), "cap": pt.INT64})
                if r[0] not in seen]
            if neighbour_ids:
                rows = {r[0]: r for r in snapshot.execute_sql(
                    self.ROWS_SQL, params={"tenant": tenant_id, "ids": neighbour_ids},
                    param_types={"tenant": pt.STRING, "ids": pt.Array(pt.STRING)})}
                for nid in neighbour_ids:
                    if nid in rows and nid not in seen:
                        r = rows[nid]
                        seen.add(nid)
                        out.append({"node_id": r[0], "name": r[1], "kind": r[2], "chunk_ids": list(r[3] or [])})
        return out[:cap]

    def delete_tenant(self, tenant_id: str) -> None:
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
    if vec is not None and hasattr(store, "seed_by_vector"):
        seeds = store.seed_by_vector(vec, tenant_id, k=k, max_distance=max_distance)
    else:
        seeds = store.seed(question, tenant_id)
    if not seeds:
        return {"seeds": [], "nodes": [], "chunk_ids": []}
    nodes = store.expand([s["node_id"] for s in seeds], tenant_id, hops=hops, cap=cap)
    return {"seeds": seeds, "nodes": nodes, "chunk_ids": sorted({cid for n in nodes for cid in n["chunk_ids"]})}


def graph_chunk_ids(db, question: str, tenant_id: str, hops: int = 1, cap: int = 20) -> dict:
    g = FirestoreGraph(db)
    seeds = g.seed(question, tenant_id)
    if not seeds:
        return {"seeds": [], "nodes": [], "chunk_ids": []}
    nodes = g.expand([s["node_id"] for s in seeds], tenant_id, hops=hops, cap=cap)
    return {"seeds": seeds, "nodes": nodes, "chunk_ids": sorted({cid for n in nodes for cid in n["chunk_ids"]})}
