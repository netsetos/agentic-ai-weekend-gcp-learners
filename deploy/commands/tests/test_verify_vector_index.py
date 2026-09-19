"""Offline regression checks; no GCP credentials or resources are used."""

from contextlib import redirect_stdout, redirect_stderr
from copy import deepcopy
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location(
    "verify_vector_index", Path(__file__).resolve().parents[1] / "verify-vector-index.py"
)
checker = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(checker)


class VectorVerificationTests(unittest.TestCase):
    def setUp(self):
        self.identity = ("example-rag-project", "123456789012")
        self.region = "asia-south1"
        prefix = f"projects/{self.identity[0]}/locations/{self.region}"
        self.index_name = f"{prefix}/indexes/100"
        self.endpoint_name = f"{prefix}/indexEndpoints/200"
        self.index = {
            "name": self.index_name.replace(self.identity[0], self.identity[1]),
            "indexUpdateMethod": "STREAM_UPDATE",
            "metadata": {"config": {"dimensions": 768}},
            "indexStats": {"vectorsCount": "0"},
        }
        self.endpoint = {
            "name": self.endpoint_name.replace(self.identity[0], self.identity[1]),
            "deployedIndexes": [{"id": "documind_chunks_v1", "index": self.index["name"]}],
        }

    def verify(self, index=None, endpoint=None):
        return checker.validate(
            self.index if index is None else index,
            self.endpoint if endpoint is None else endpoint,
            self.index_name, self.endpoint_name, "documind_chunks_v1", self.region, self.identity,
        )

    def test_project_id_and_number_are_equivalent_in_both_directions(self):
        self.verify()
        self.index["name"], self.index_name = self.index_name, self.index["name"]
        self.endpoint["name"], self.endpoint_name = self.endpoint_name, self.endpoint["name"]
        self.endpoint["deployedIndexes"][0]["index"] = self.index["name"]
        self.verify()

    def test_actual_project_must_match_even_when_resource_id_matches(self):
        for field in ("index", "endpoint", "deployment"):
            with self.subTest(field=field):
                index, endpoint = deepcopy(self.index), deepcopy(self.endpoint)
                obj, key = (index, "name") if field == "index" else (endpoint, "name")
                if field == "deployment":
                    obj, key = endpoint["deployedIndexes"][0], "index"
                obj[key] = obj[key].replace(self.identity[1], "999999999999")
                with self.assertRaisesRegex(ValueError, "Project mismatch"):
                    self.verify(index, endpoint)

    def test_wrong_region_resource_id_or_kind_is_rejected(self):
        for old, new in (("asia-south1", "us-central1"), ("/100", "/999"), ("/indexes/", "/indexEndpoints/")):
            with self.subTest(new=new):
                index = deepcopy(self.index)
                index["name"] = index["name"].replace(old, new)
                with self.assertRaises(ValueError):
                    self.verify(index=index)
        endpoint = deepcopy(self.endpoint)
        endpoint["name"] = endpoint["name"].replace("/200", "/999")
        with self.assertRaisesRegex(ValueError, "Unexpected endpoint"):
            self.verify(endpoint=endpoint)

    def test_wrong_attachment_or_missing_or_duplicate_deployment_is_rejected(self):
        for deployments in ([], self.endpoint["deployedIndexes"] * 2,
                            [{"id": "documind_chunks_v1", "index": self.index["name"].replace("/100", "/999")} ]):
            with self.subTest(deployments=deployments):
                endpoint = deepcopy(self.endpoint)
                endpoint["deployedIndexes"] = deployments
                with self.assertRaises(ValueError):
                    self.verify(endpoint=endpoint)

    def test_streaming_and_dimensions_remain_required(self):
        index = deepcopy(self.index)
        index["indexUpdateMethod"] = "BATCH_UPDATE"
        with self.assertRaisesRegex(ValueError, "not streaming"):
            self.verify(index=index)
        index = deepcopy(self.index)
        index["metadata"]["config"]["dimensions"] = 256
        with self.assertRaisesRegex(ValueError, "768 dimensions"):
            self.verify(index=index)

    def test_project_lookup_rejects_stale_or_deleted_project(self):
        project = {"projectId": self.identity[0], "projectNumber": self.identity[1], "lifecycleState": "ACTIVE"}
        self.assertEqual(checker.project_identity(project, self.identity[0]), self.identity)
        with self.assertRaisesRegex(ValueError, "lookup mismatch"):
            checker.project_identity(project, "previous-project")
        project["lifecycleState"] = "DELETE_REQUESTED"
        with self.assertRaisesRegex(ValueError, "not ACTIVE"):
            checker.project_identity(project, self.identity[0])

    def test_cli_success_writes_evidence_without_applying_terraform(self):
        project = {"projectId": self.identity[0], "projectNumber": self.identity[1], "lifecycleState": "ACTIVE"}
        answers = [json.dumps(project), self.index_name, self.endpoint_name, "documind_chunks_v1",
                   json.dumps(self.index), json.dumps(self.endpoint)]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "terraform").mkdir()
            with patch.object(checker, "read_command", side_effect=answers) as read, redirect_stdout(io.StringIO()) as output:
                code = checker.main(["--project", self.identity[0], "--region", self.region, "--deploy-root", directory])
            self.assertEqual(code, 0)
            self.assertIn("PASS:", output.getvalue())
            self.assertEqual(json.loads((root / "operator-evidence/vector-index.json").read_text()), self.index)
            for call in read.call_args_list:
                command = call.args[0]
                self.assertIn("output" if command[0] == "terraform" else "describe", command)

    def test_cli_auth_failure_stops_without_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "terraform").mkdir()
            with patch.object(checker, "read_command", side_effect=subprocess.CalledProcessError(1, "gcloud")), \
                    redirect_stdout(io.StringIO()) as output, redirect_stderr(io.StringIO()):
                code = checker.main(["--project", self.identity[0], "--region", self.region, "--deploy-root", directory])
            self.assertEqual(code, 1)
            self.assertNotIn("PASS:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
