"""Offline source-identity and filter-isolation checks; no GCP calls are made."""

from copy import deepcopy
import importlib.util
from pathlib import Path
import unittest


SPEC = importlib.util.spec_from_file_location(
    "check_firestore_fallback",
    Path(__file__).resolve().parents[1] / "check-firestore-fallback.py",
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class FirestoreFallbackTests(unittest.TestCase):
    def setUp(self):
        self.uri = "gs://example-rag-uploads/acme/hr_policy_2026.md"
        self.doc_key = "acme_abc123"
        self.row = {
            "tenant_id": "acme",
            "source_uri": self.uri,
            "doc_key": self.doc_key,
            "current": True,
            "kind": "text",
            "doc_type": "unknown",
            "embedding": [1.0] + [0.0] * 767,
            "embedding_model": "text-embedding-005",
            "embedding_version": "1",
            "embedding_task_type": "RETRIEVAL_DOCUMENT",
        }
        self.rows = [("acme:abc123#0", self.row)]

    def select(self, rows=None):
        return checker.select_seed(
            self.rows if rows is None else rows,
            tenant="acme", source_uri=self.uri, doc_key=self.doc_key,
            embedding_model="text-embedding-005", embedding_version="1",
        )

    def hit(self, **changes):
        row = deepcopy(self.row)
        row.pop("embedding")
        row.update(id="acme:abc123#0", found_by="firestore")
        row.update(changes)
        return row

    def verify(self, hits, current_only=True):
        return checker.verify_hits(
            hits, tenant="acme", source_uri=self.uri,
            filters={"doc_type": "unknown", "kind": "text"},
            current_only=current_only,
        )

    def test_unknown_and_policy_are_filtered_as_stored(self):
        for doc_type in ("unknown", "policy"):
            with self.subTest(doc_type=doc_type):
                self.row["doc_type"] = doc_type
                seed_id, row, vector, filters = self.select()
                self.assertEqual(seed_id, "acme:abc123#0")
                self.assertEqual(row["source_uri"], self.uri)
                self.assertEqual(vector, self.row["embedding"])
                self.assertEqual(filters, {"doc_type": doc_type, "kind": "text"})

    def test_missing_or_noncurrent_source_is_not_accepted(self):
        for value in (None, False, "true"):
            with self.subTest(current=value):
                row = deepcopy(self.row)
                row["current"] = value
                with self.assertRaises(ValueError):
                    self.select([("old", row)])
        with self.assertRaises(ValueError):
            self.select([])

    def test_seed_rejects_wrong_tenant_source_or_ledger_key(self):
        for field, value in (("tenant_id", "zeta"),
                             ("source_uri", "gs://other/acme/hr_policy_2026.md"),
                             ("doc_key", "acme_older")):
            with self.subTest(field=field):
                row = deepcopy(self.row)
                row[field] = value
                with self.assertRaises(ValueError):
                    self.select([("wrong", row)])

    def test_seed_rejects_mixed_or_missing_classification(self):
        other = deepcopy(self.row)
        other["doc_type"] = "policy"
        with self.assertRaises(ValueError):
            self.select(self.rows + [("acme:abc123#1", other)])
        for value in (None, "", "   "):
            with self.subTest(doc_type=value):
                row = deepcopy(self.row)
                row["doc_type"] = value
                with self.assertRaises(ValueError):
                    self.select([("bad", row)])

    def test_seed_rejects_staging_and_wrong_kind(self):
        for field, value in (("staged", True), ("kind", "figure"), ("kind", None)):
            with self.subTest(field=field, value=value):
                row = deepcopy(self.row)
                row[field] = value
                with self.assertRaises(ValueError):
                    self.select([("bad", row)])

    def test_seed_rejects_unusable_vectors(self):
        vectors = (None, [], [1.0] * 767, [0.0] * 768,
                   [float("nan")] + [0.0] * 767,
                   [float("inf")] + [0.0] * 767)
        for vector in vectors:
            with self.subTest(vector_type=str(vector)[:35]):
                row = deepcopy(self.row)
                row["embedding"] = vector
                with self.assertRaises(ValueError):
                    self.select([("bad", row)])

    def test_seed_rejects_wrong_embedding_space_or_task(self):
        for field, value in (("embedding_model", "different-model"),
                             ("embedding_version", "2"),
                             ("embedding_task_type", "RETRIEVAL_QUERY")):
            with self.subTest(field=field):
                row = deepcopy(self.row)
                row[field] = value
                with self.assertRaises(ValueError):
                    self.select([("bad", row)])

    def test_results_accept_control_and_same_filter_peers(self):
        self.assertIsNone(self.verify([
            self.hit(), self.hit(id="other", source_uri="gs://example-rag-uploads/acme/other.md")
        ]))

    def test_results_reject_wrong_tenant_filter_backend_or_staging(self):
        for field, value in (("tenant_id", "zeta"), ("doc_type", "policy"),
                             ("kind", "figure"), ("found_by", "vector"),
                             ("staged", True)):
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    self.verify([self.hit(), self.hit(**{field: value})])

    def test_results_require_nonempty_control_source(self):
        for hits in ([], [self.hit(source_uri="gs://example-rag-uploads/acme/other.md")]):
            with self.subTest(hits=hits):
                with self.assertRaises(ValueError):
                    self.verify(hits)

    def test_current_off_can_include_retired_but_current_on_cannot(self):
        hits = [self.hit(), self.hit(id="retired", current=False)]
        self.assertIsNone(self.verify(hits, current_only=False))
        with self.assertRaises(ValueError):
            self.verify(hits, current_only=True)
        with self.assertRaises(ValueError):
            self.verify([self.hit(current=None)], current_only=True)


if __name__ == "__main__":
    unittest.main()
