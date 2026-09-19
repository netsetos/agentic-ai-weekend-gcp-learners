"""Exercise source selection and pinned file reads using an offline Git origin."""
import json
import os
from pathlib import Path
import shlex
import subprocess
import tempfile
import unittest


HELPER = Path(__file__).resolve().parents[1] / "git-source.sh"


class GitSourceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rag-git-source-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.origin = self.root / "original source"
        self.repo = self.root / "operator source"
        self.deploy = self.root / "operator's deployment"
        self.session = self.root / "source session.env"
        self.branch = "claude/rag-production-hardening"
        self.git("init", "--quiet", "--initial-branch=" + self.branch, str(self.origin))
        source = self.origin / "deploy" / "terraform"
        source.mkdir(parents=True)
        (source / "backend.tf").write_text('terraform { backend "gcs" {} }\n')
        (source / "example.tf").write_text("# original pinned bytes\n")
        self.git("-C", str(self.origin), "add", "deploy")
        self.commit()
        self.snapshot = self.git("-C", str(self.origin), "rev-parse", "HEAD").stdout.strip()
        self.git("clone", "--quiet", str(self.origin), str(self.repo))
        self.env = {key: value for key, value in os.environ.items()
                    if not key.startswith(("RAG_", "SOURCE_", "GIT_SHA", "DEMO_ROOT"))}
        self.env.update(RAG_SOURCE_REPO=str(self.repo), SOURCE_BRANCH=self.branch,
                        DEMO_ROOT=str(self.deploy), RAG_BACKUP_ROOT=str(self.root),
                        RAG_SESSION_FILE=str(self.session))

    def git(self, *args):
        return subprocess.run(["git", *args], text=True, capture_output=True, check=True)

    def commit(self):
        self.git("-C", str(self.origin), "-c", "user.name=Offline Test", "-c",
                 "user.email=offline@example.test", "commit", "--quiet", "-am", "snapshot")

    def shell(self, commands, extra=None):
        result = subprocess.run(["bash", "--noprofile", "--norc", "-eu", "-c",
                                 "source " + shlex.quote(str(HELPER)) + "\n" + commands],
                                env={**self.env, **(extra or {})}, text=True, capture_output=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def test_sourcing_helper_preserves_settings_without_creating_session(self):
        self.shell('test "$SOURCE_BRANCH" = ' + shlex.quote(self.branch) + '\n'
                   'test "$SOURCE_COMMIT" = ' + shlex.quote(self.snapshot) + '\n'
                   'declare -F rag_refresh_source rag_get_file\n', {"SOURCE_COMMIT": self.snapshot})
        self.assertFalse(self.session.exists())
        self.assertFalse(self.deploy.exists())

    def test_explicit_refresh_uses_configured_legacy_branch(self):
        result = self.shell("rag_refresh_source\npython - <<'PY'\nimport json,os\n"
                            "print(json.dumps({k:os.environ[k] for k in ('SOURCE_BRANCH','SOURCE_COMMIT','GIT_SHA','RAG_SOURCE_REPO')}))\nPY\n")
        self.assertEqual(json.loads(result.stdout.splitlines()[-1]), {
            "SOURCE_BRANCH": self.branch, "SOURCE_COMMIT": self.snapshot,
            "GIT_SHA": self.snapshot, "RAG_SOURCE_REPO": str(self.repo),
        })
        self.assertIn(self.branch, self.session.read_text())
        self.assertNotIn("agentic-ai-weekend-gcp-learners", self.session.read_text())

    def test_get_file_uses_saved_commit_after_origin_branch_advances(self):
        self.shell("rag_refresh_source")
        (self.origin / "deploy/terraform/example.tf").write_text("# newer branch bytes\n")
        self.commit()
        result = self.shell("source " + shlex.quote(str(self.session)) + "\nrag_get_file terraform/example.tf\n")
        self.assertIn("Git blob verified", result.stdout)
        self.assertEqual((self.deploy / "terraform/example.tf").read_text(), "# original pinned bytes\n")
        self.assertEqual(self.git("-C", str(self.repo), "rev-parse", "HEAD").stdout.strip(), self.snapshot)


if __name__ == "__main__":
    unittest.main()
