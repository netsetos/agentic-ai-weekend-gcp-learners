"""Offline checks for the operator's preview and stored residency policy.

No Google packages or credentials are needed; the real Mirror and policy reader
run against in-memory rows and stores. No live imports or mutations occur.
"""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace
import unittest
from unittest.mock import Mock, patch


DEPLOY = Path(__file__).resolve().parents[2]


def load(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeDB:
    def __init__(self, policy):
        self.policy = policy
        self.policy_reads = []
        self.writes = []
        self.row = {"status": "indexed", "tenant_id": "acme", "doc_key": "acme_doc",
                    "name": "acme/demo.md", "generation": "7",
                    "gcs_uri": "gs://demo-uploads/acme/demo.md"}

    def collection(self, name):
        db = self

        class Collection:
            def document(self, key):
                class Document:
                    def get(self):
                        if name == "tenant_settings":
                            db.policy_reads.append(key)
                            data = {"data_region": db.policy}
                        else:
                            data = db.row
                        return SimpleNamespace(exists=True, to_dict=lambda: dict(data))

                    def set(self, data, merge):
                        db.writes.append((name, key, data, merge))

                return Document()

            def where(self, **_):
                return self

            def stream(self):
                assert name == "sources"
                return [SimpleNamespace(to_dict=lambda: dict(db.row),
                                        reference=self.document("acme~demo.md"))]

        return Collection()


class Store:
    def __init__(self, name, region):
        self.name, self.region = name, region
        self.project, self.location, self.bucket = "demo", region, "demo-audit"
        self.listings, self.writes = [], []

    def listing(self, tenant):
        self.listings.append(tenant)
        return {}

    def upsert(self, *args):
        self.writes.append(args)
        return "import-complete"


class RemirrorTests(unittest.TestCase):
    def setUp(self):
        self.google = ModuleType("google")
        self.cloud = ModuleType("google.cloud")
        self.firestore = ModuleType("google.cloud.firestore")
        self.firestore.Client = Mock(side_effect=AssertionError("Unexpected cloud client"))
        self.firestore.SERVER_TIMESTAMP = "server-timestamp"
        self.query = ModuleType("google.cloud.firestore_v1.base_query")
        self.query.FieldFilter = lambda *args: args
        self.cloud.firestore = self.firestore
        self.google.cloud = self.cloud
        shared = ModuleType("shared")
        tenancy = load("tenancy", DEPLOY / "shared/tenancy.py")
        managed = load("managed", DEPLOY / "services/ingest/managed.py")
        self.managed = managed
        stubs = {"google": self.google, "google.cloud": self.cloud,
                 "google.cloud.firestore": self.firestore,
                 "google.cloud.firestore_v1": ModuleType("google.cloud.firestore_v1"),
                 "google.cloud.firestore_v1.base_query": self.query,
                 "shared": shared, "shared.tenancy": tenancy, "managed": managed}
        context = patch.dict(sys.modules, stubs)
        context.start()
        self.addCleanup(context.stop)
        self.module = load("remirror_missing", DEPLOY / "operators/remirror_missing.py")

    def run_operator(self, policy, apply=False):
        db = FakeDB(policy)
        self.db = db
        self.stores = [Store("rag_engine", "us-central1"), Store("vertex_search", "global")]
        with patch.dict(os.environ, {"PROJECT": "demo", "GOOGLE_CLOUD_PROJECT": "demo",
                                     "AUDIT_BUCKET": "demo-audit"}), \
             patch.object(self.firestore, "Client", return_value=db), \
             patch.object(self.managed, "RagEngineStore", return_value=self.stores[0]), \
             patch.object(self.managed, "VertexSearchStore", return_value=self.stores[1]), \
             patch.object(self.module, "RetentionAwareSearchRetry", return_value=self.stores[1]), \
             patch.object(self.module, "version_rows", return_value=("Current text", {})), \
             patch.object(self.managed.Mirror, "_record"), \
             contextlib.redirect_stdout(io.StringIO()) as output:
            result = self.module.main(["--tenant", "acme"] + (["--apply"] if apply else []))
        return result, output.getvalue()

    def test_import_does_not_open_a_cloud_client(self):
        self.firestore.Client.assert_not_called()

    def test_preview_reads_stored_any_policy_without_writing(self):
        code, output = self.run_operator("any")
        self.assertEqual(code, 0)
        self.assertEqual(self.db.policy_reads, ["acme"])
        self.assertIn('"apply": false', output)
        self.assertIn('"rag_engine", "vertex_search"', output)
        self.assertFalse(self.db.writes)
        self.assertTrue(all(not store.writes for store in self.stores))

    def test_india_only_policy_is_refused_before_store_access(self):
        with self.assertRaisesRegex(AssertionError, "tenant policy"):
            self.run_operator("in")
        self.assertEqual(self.db.policy_reads, ["acme"])
        self.assertFalse(self.db.writes)
        self.assertTrue(all(not store.listings and not store.writes for store in self.stores))

    def test_apply_uses_stored_policy_for_scoped_mirror_and_stamp(self):
        code, _ = self.run_operator("any", apply=True)
        self.assertEqual(code, 0)
        self.assertEqual(self.db.policy_reads, ["acme", "acme", "acme"])
        self.assertTrue(all(len(store.writes) == 1 for store in self.stores))
        self.assertEqual(len(self.db.writes), 1)
        self.assertEqual(self.db.writes[0][2]["data_region"], "any")


if __name__ == "__main__":
    unittest.main()
