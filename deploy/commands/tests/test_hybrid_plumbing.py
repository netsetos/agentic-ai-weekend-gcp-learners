"""Offline CLI-boundary checks for the hybrid retrieval checkpoint.

The Python query body is syntax-checked but never imports Google clients here.
Fake Terraform/Python executables prove that failed configuration reads cannot
reach the query, including when the script is sourced from a command chain.
"""
import ast
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "check-hybrid-plumbing.sh"
OUTPUTS = {
    "vector_index_endpoint": "projects/test-project/locations/asia-south1/indexEndpoints/1234",
    "vector_deployed_index_id": "documind_chunks_v1",
    "embedding_model": "text-embedding-005",
    "embedding_version": "1",
}
EXPORTS = {
    "VECTOR_INDEX_ENDPOINT": "vector_index_endpoint",
    "VECTOR_DEPLOYED_INDEX_ID": "vector_deployed_index_id",
    "EMBEDDING_MODEL": "embedding_model",
    "EMBEDDING_VERSION": "embedding_version",
}


class HybridPlumbingTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rag-hybrid-check-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.bin = self.root / "bin"
        self.deploy = self.root / "existing deployment"
        self.bin.mkdir()
        self.deploy.mkdir()
        self.fixture = self.root / "terraform.json"
        self.calls = self.root / "terraform-calls.jsonl"
        self.query = self.root / "query-environment.json"
        self.env = os.environ.copy()
        self.env.update({
            "PATH": str(self.bin), "PROJECT": "test-project", "DEMO_ROOT": str(self.deploy),
            "RAG_TEST_TF_FIXTURE": str(self.fixture), "RAG_TEST_TF_CALLS": str(self.calls),
            "RAG_TEST_QUERY": str(self.query), "GOOGLE_CLOUD_PROJECT": "parent-project",
            "PYTHONPATH": "parent-pythonpath",
            **{name: "parent-" + name for name in EXPORTS},
        })
        self.configure()
        self.write_executable("terraform", """
import json, os, pathlib, sys
args = sys.argv[1:]
with open(os.environ['RAG_TEST_TF_CALLS'], 'a') as output:
    output.write(json.dumps({'args': args, 'cwd': os.getcwd()}) + '\\n')
assert args[:3] == ['-chdir=terraform', 'output', '-raw'], args
fixture = json.loads(pathlib.Path(os.environ['RAG_TEST_TF_FIXTURE']).read_text())
name = args[3]
if name == fixture.get('failed'):
    print('partial stdout must not mask the failed Terraform command')
    print('fixture: Terraform read failed', file=sys.stderr)
    raise SystemExit(7)
sys.stdout.write(fixture['outputs'][name])
""")
        self.write_executable("python", """
import ast, json, os, pathlib, sys
assert sys.argv[1:] == ['-'], sys.argv
ast.parse(sys.stdin.read())
names = ['PROJECT', 'GOOGLE_CLOUD_PROJECT', 'PYTHONPATH', 'VECTOR_INDEX_ENDPOINT',
         'VECTOR_DEPLOYED_INDEX_ID', 'EMBEDDING_MODEL', 'EMBEDDING_VERSION']
pathlib.Path(os.environ['RAG_TEST_QUERY']).write_text(json.dumps({k:os.environ.get(k) for k in names}))
""")

    def write_executable(self, name, body):
        path = self.bin / name
        path.write_text("#!" + sys.executable + "\n" + body)
        path.chmod(0o700)

    def configure(self, failed=None, **overrides):
        self.fixture.write_text(json.dumps({"outputs": {**OUTPUTS, **overrides}, "failed": failed}))

    def run_script(self, *, success=True, conditional=False, after=""):
        invocation = "source " + shlex.quote(str(SOURCE))
        if conditional:
            invocation = "if " + invocation + "; then exit 98; else rag_check_status=$?; fi\n" + after + "\nexit \"$rag_check_status\""
        else:
            invocation += "\n" + after
        result = subprocess.run([shutil.which("bash"), "--noprofile", "--norc", "-c", "set -euo pipefail\n" + invocation],
                                env=self.env, cwd=self.root, text=True, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, timeout=10)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertNotEqual(result.returncode, 98, "Invalid checkpoint unexpectedly succeeded")
        return result

    def test_missing_terraform_never_invokes_python(self):
        (self.bin / "terraform").unlink()
        result = self.run_script(success=False, conditional=True)
        self.assertIn("Terraform is not available", result.stderr)
        self.assertFalse(self.query.exists())
        self.assertFalse(self.calls.exists())

    def test_failed_output_never_invokes_python_even_inside_conditional(self):
        for name in OUTPUTS:
            with self.subTest(output=name):
                self.configure(failed=name)
                result = self.run_script(success=False, conditional=True)
                self.assertIn(name, result.stderr)
                self.assertIn("failed", result.stderr)
                self.assertFalse(self.query.exists())

    def test_empty_or_whitespace_output_never_invokes_python(self):
        for name in OUTPUTS:
            for empty in ("", " \t\n"):
                with self.subTest(output=name, value=repr(empty)):
                    self.configure(**{name: empty})
                    result = self.run_script(success=False, conditional=True)
                    self.assertIn(name, result.stderr)
                    self.assertIn("empty", result.stderr)
                    self.assertFalse(self.query.exists())

    def test_success_exports_all_four_checked_outputs(self):
        self.run_script()
        actual = json.loads(self.query.read_text())
        for exported, output in EXPORTS.items():
            self.assertEqual(actual[exported], OUTPUTS[output])
        self.assertEqual(actual["PROJECT"], "test-project")
        self.assertEqual(actual["GOOGLE_CLOUD_PROJECT"], "test-project")
        self.assertEqual(actual["PYTHONPATH"], str(self.deploy))
        calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
        self.assertEqual([call["args"][-1] for call in calls], list(OUTPUTS))
        self.assertTrue(all(call["cwd"] == str(self.deploy) for call in calls))

    def test_sourcing_leaves_parent_exports_and_directory_unchanged(self):
        names = [*EXPORTS, "GOOGLE_CLOUD_PROJECT", "PYTHONPATH", "PWD"]
        after = "\n".join('printf "%s=%s\\n" ' + shlex.quote(name) + ' "$' + name + '"' for name in names)
        result = self.run_script(after=after)
        lines = dict(line.split("=", 1) for line in result.stdout.splitlines() if "=" in line)
        for name in EXPORTS:
            self.assertEqual(lines[name], self.env[name])
        self.assertEqual(lines["GOOGLE_CLOUD_PROJECT"], "parent-project")
        self.assertEqual(lines["PYTHONPATH"], "parent-pythonpath")
        self.assertEqual(lines["PWD"], str(self.root))

    def test_embedded_python_is_valid_syntax(self):
        body = SOURCE.read_text().split("<<'PY'\n", 1)[1].split("\nPY\n", 1)[0]
        ast.parse(body)


if __name__ == "__main__":
    unittest.main()
