"""Offline regression checks for Spanner traversal and citation hydration.

No cloud calls: the strict fake rejects DISTINCT arrays like the live service.
Live GQL execution remains a runbook acceptance gate.
"""
import ast
from pathlib import Path
import re
from types import SimpleNamespace
import unittest
from unittest.mock import patch


SOURCE = Path(__file__).resolve().parents[2] / "shared" / "documind_graph.py"
tree = ast.parse(SOURCE.read_text())
cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "SpannerGraph")
namespace = {"TENANT": "acme"}
exec(compile(ast.Module(body=[cls], type_ignores=[]), str(SOURCE), "exec"), namespace)
SpannerGraph = namespace["SpannerGraph"]
TYPES = SimpleNamespace(STRING="STRING", INT64="INT64", Array=lambda t: ("ARRAY", t))


class Database:
    def __init__(self):
        self.rows = {
            ("acme", "a"): ("a", "Purchasing", "policy", ["acme:doc#0", "acme:doc#1"]),
            ("acme", "b"): ("b", "Finance", "org", ["acme:doc#2", "acme:doc#3"]),
            ("acme", "c"): ("c", "Director", "person", ["acme:doc#4"]),
            ("acme", "isolated"): ("isolated", "Standalone", "policy", None),
            ("zeta", "a"): ("a", "Zeta Purchasing", "policy", ["zeta:secret#0"]),
            ("zeta", "b"): ("b", "Zeta Finance", "org", ["zeta:secret#1"]),
        }
        # Parallel edges and cycles must not consume the distinct-node limit.
        self.edges = [("acme", "a", "b"), ("acme", "a", "b"),
                      ("acme", "b", "c"), ("zeta", "a", "b")]
        self.calls, self.snapshots = [], []

    def snapshot(self, multi_use=False):
        snap = self.Snapshot(self, multi_use)
        self.snapshots.append(snap)
        return snap

    class Snapshot:
        def __init__(self, db, multi_use):
            self.db, self.multi_use, self.active = db, multi_use, False

        def __enter__(self):
            self.active = True
            return self

        def __exit__(self, *_):
            self.active = False

        def execute_sql(self, sql, params, param_types):
            assert self.active and self.multi_use, "use one active multi-use snapshot"
            assert set(params) == set(param_types), "every parameter must be typed"
            self.db.calls.append((sql, params, id(self)))
            tenant = params["tenant"]
            if sql.startswith("GRAPH"):
                projection = sql.split("RETURN DISTINCT", 1)[1].split("LIMIT", 1)[0]
                if "chunk_ids" in projection:
                    raise ValueError("Column chunk_ids of type ARRAY cannot be used in RETURN DISTINCT")
                assert "a.tenant_id = @tenant" in sql and "b.tenant_id = @tenant" in sql
                hops = int(re.search(r"\{1,(\d)\}", sql)[1])
                frontier = {n for n in params["seeds"] if (tenant, n) in self.db.rows}
                reached = set()
                for _ in range(hops):
                    nxt = set()
                    for t, a, b in self.db.edges:
                        if t == tenant:
                            if a in frontier:
                                nxt.add(b)
                            if b in frontier:
                                nxt.add(a)
                    reached |= nxt
                    frontier = nxt
                return [(n,) for n in sorted(reached)[:params["cap"]]]
            assert "tenant_id = @tenant" in sql and "node_id IN UNNEST(@ids)" in sql
            # Deliberately reverse storage order: hydration must honour GQL order.
            return [row for (t, n), row in reversed(list(self.db.rows.items()))
                    if t == tenant and n in params["ids"]]


class SpannerGraphTests(unittest.TestCase):
    def setUp(self):
        self.db = Database()
        self.graph = SpannerGraph(self.db)
        self.pt = patch.object(SpannerGraph, "_pt", return_value=TYPES)
        self.pt.start()
        self.addCleanup(self.pt.stop)

    def test_one_hop_preserves_all_citation_ids(self):
        rows = self.graph.expand(["a"], "acme", hops=1)
        self.assertEqual([r["node_id"] for r in rows], ["a", "b"])
        self.assertEqual(rows[1]["chunk_ids"], ["acme:doc#2", "acme:doc#3"])
        self.assertEqual(len(self.db.snapshots), 1)
        self.assertEqual(len({c[2] for c in self.db.calls}), 1)

    def test_two_hops_deduplicate_paths_and_seed(self):
        rows = self.graph.expand(["a", "a"], "acme", hops=2)
        self.assertEqual([r["node_id"] for r in rows], ["a", "b", "c"])

    def test_reverse_direction_and_cap(self):
        rows = self.graph.expand(["c"], "acme", hops=2, cap=2)
        self.assertEqual([r["node_id"] for r in rows], ["c", "a"])

    def test_tenant_id_collision_does_not_leak_chunks(self):
        rows = self.graph.expand(["a", "isolated"], "zeta", hops=2)
        self.assertEqual([r["node_id"] for r in rows], ["a", "b"])
        self.assertTrue(all(c.startswith("zeta:") for r in rows for c in r["chunk_ids"]))

    def test_disconnected_seed_keeps_its_row(self):
        rows = self.graph.expand(["isolated"], "acme", hops=2)
        self.assertEqual(rows, [{"node_id": "isolated", "name": "Standalone", "kind": "policy", "chunk_ids": []}])

    def test_empty_seeds_skip_database(self):
        self.assertEqual(self.graph.expand([], "acme"), [])
        self.assertEqual(self.db.calls, [])

    def test_zero_cap_skips_database(self):
        self.assertEqual(self.graph.expand(["a"], "acme", cap=0), [])
        self.assertEqual(self.db.calls, [])

    def test_invalid_hops_rejected(self):
        with self.assertRaises(ValueError):
            self.graph.expand(["a"], "acme", hops=3)
        self.assertEqual(self.db.calls, [])


if __name__ == "__main__":
    unittest.main()
