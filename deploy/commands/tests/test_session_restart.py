"""Offline restart checks: exercise the real Bash helper with isolated fixtures.

No Google Cloud calls are permitted. Fake CLI executables accept only the read
operations used by session recovery; source files and graph backups are real
temporary files so quoting, persistence and snapshot checks run normally.
"""
import ast
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest


SOURCE = Path(__file__).resolve().parents[1] / "session-restart.sh"
PROJECT = "documind-restart-test"
PROJECT_NUMBER = "123456789012"
REGION = "asia-south1"
API_URL = "https://serving-audience.example.test"
QUESTION = 'Who signs off on a "big" purchase?\nInclude the CFO\'s threshold.'


def write(path, value, executable=False):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value)
    if executable:
        path.chmod(0o700)


def exports(values):
    return "".join(f"export {name}={shlex.quote(str(value))}\n" for name, value in values.items())


class SessionRestartTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="rag-restart-tests-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.session_home = self.root / "session files"
        self.deploy = self.root / "deployment's files"
        self.repo = self.root / "source repository"
        self.bin = self.root / "mock-bin"
        self.graph = self.root / "graph evidence's original"
        for directory in (self.session_home, self.deploy, self.repo, self.bin, self.graph):
            directory.mkdir()
        self.log = self.root / "calls.jsonl"
        self.real_git = shutil.which("git")
        subprocess.run([self.real_git, "init", "--quiet", str(self.repo)], check=True)
        write(self.repo / "README", "The selected deployment snapshot.\n")
        subprocess.run([self.real_git, "-C", str(self.repo), "add", "README"], check=True)
        subprocess.run([self.real_git, "-C", str(self.repo), "-c", "user.name=Offline Test",
                        "-c", "user.email=offline@example.test", "commit", "--quiet", "-m", "fixture"], check=True)
        self.commit = subprocess.check_output(
            [self.real_git, "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.env = os.environ.copy()
        for key in list(self.env):
            if key.startswith(("RAG_", "GRAPH_", "DOCUMIND_", "SPANNER_")):
                self.env.pop(key)
        self.env.update({
            "PROJECT": PROJECT, "PROJECT_ID": PROJECT,
            "PROJECT_NUMBER": PROJECT_NUMBER, "REGION": REGION,
            "OPERATOR_EMAIL": "operator@example.test", "DEMO_ROOT": str(self.deploy),
            "DEPLOY_ROOT": str(self.deploy), "RAG_SOURCE_REPO": str(self.repo),
            "SOURCE_COMMIT": self.commit, "GIT_SHA": self.commit,
            "SOURCE_BRANCH": "claude/rag-production-hardening",
            "RAG_SOURCE_BACKUP": str(self.root / "source backups"),
            "RAG_SESSION_HOME": str(self.session_home),
            "RAG_TEST_CALL_LOG": str(self.log), "RAG_TEST_REAL_GIT": self.real_git,
            "RAG_TEST_CONFIG": str(self.root / "cloud-fixture.json"),
            "PATH": str(self.bin) + os.pathsep + self.env["PATH"],
        })
        self.write_source_session()
        write(self.session_home / "rag-git-source.sh", """
rag_get_file() { printf 'UNEXPECTED Git file fetch\\n' >&2; return 99; }
rag_refresh_source() { printf 'UNEXPECTED source refresh\\n' >&2; return 99; }
""")
        self.make_python()
        self.make_cloud_commands()
        self.write_graph()

    @property
    def saved(self):
        return self.session_home / "rag-resume.env"

    def write_source_session(self, **overrides):
        values = {name: self.env[name] for name in
                  ("DEMO_ROOT", "RAG_SOURCE_REPO", "SOURCE_COMMIT", "GIT_SHA", "SOURCE_BRANCH", "RAG_SOURCE_BACKUP")}
        values.update(overrides)
        write(self.session_home / "rag-source-session.env", exports(values))

    def make_python(self):
        venv = self.session_home / "rag-shell-venv"
        (venv / "bin").mkdir(parents=True)
        (venv / "bin" / "python").symlink_to(sys.executable)
        write(venv / "bin" / "activate", exports({
            "VIRTUAL_ENV": venv,
            "PATH": str(venv / "bin") + os.pathsep + self.env["PATH"],
        }))
        # Python ADC calls use a local module fixture, not installed credentials.
        write(self.deploy / "google" / "__init__.py", "")
        write(self.deploy / "google" / "auth" / "__init__.py", """
import os
class Credentials:
    quota_project_id = None
    def __init__(self):
        self.quota_project_id = os.environ.get('GOOGLE_CLOUD_QUOTA_PROJECT')
    def refresh(self, request):
        if os.environ.get('RAG_TEST_ADC_FAIL') == '1':
            raise RuntimeError('offline fixture: ADC is unavailable')
def default(scopes=None, quota_project_id=None):
    return Credentials(), os.environ.get('PROJECT')
""")
        write(self.deploy / "google" / "auth" / "transport" / "__init__.py", "")
        write(self.deploy / "google" / "auth" / "transport" / "requests.py", "class Request: pass\n")

    def make_cloud_commands(self):
        self.service = {
            "metadata": {"name": "documind-api", "namespace": PROJECT_NUMBER, "uid": "api1", "generation": 17},
            "spec": {"template": {"spec": {"containers": [{"env": [
                {"name": "SELF_URL", "value": "https://wrong-new-template.example.test"}]}]}}},
            "status": {"observedGeneration": 17, "latestCreatedRevisionName": "new-retired",
                       "latestReadyRevisionName": "serving-old", "url": "https://wrong-service-url.example.test",
                       "conditions": [{"type": "Ready", "status": "True"}],
                       "traffic": [{"revisionName": "serving-old", "percent": 100}]},
        }
        self.revision = {
            "metadata": {"name": "serving-old", "uid": "rev1", "generation": 1},
            "spec": {"containers": [{"env": [{"name": "SELF_URL", "value": API_URL}]}]},
            "status": {"observedGeneration": 1, "conditions": [
                {"type": "Ready", "status": "True"}, {"type": "Active", "status": "True"}]},
        }
        self.update_cloud_fixture()
        fake = "#!" + sys.executable + "\n" + '''
import json, os, pathlib, subprocess, sys
name = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ['RAG_TEST_CALL_LOG'], 'a') as out:
    out.write(json.dumps([name] + args) + '\\n')
config = json.loads(pathlib.Path(os.environ['RAG_TEST_CONFIG']).read_text())
if name == 'git':
    if any(word in args for word in ('fetch', 'pull', 'checkout', 'switch', 'reset')):
        raise SystemExit('FORBIDDEN: restart changed its Git source')
    raise SystemExit(subprocess.call([os.environ['RAG_TEST_REAL_GIT']] + args))
if name == 'curl':
    urls = [arg for arg in args if arg.startswith('https://')]
    if not urls or not urls[-1].startswith(config['url'] + '/'):
        raise SystemExit('FORBIDDEN: request used the wrong audience URL')
    payload = {'status': 'ready', 'git_sha': 'fixture'}
    if '-o' in args:
        pathlib.Path(args[args.index('-o')+1]).write_text(json.dumps(payload))
    else:
        print(json.dumps(payload))
    raise SystemExit(0)
if args[:2] == ['config', 'set']:
    if args[2] not in ('project', 'billing/quota_project', 'account'):
        raise SystemExit('Unexpected local gcloud configuration mutation')
elif args[:2] == ['config', 'get-value']:
    print('operator@example.test' if args[2] == 'account' else os.environ['PROJECT'])
elif args[:2] == ['auth', 'list']:
    print('operator@example.test' if any('value(' in a for a in args) else '[]')
elif args[:2] == ['auth', 'print-access-token']:
    print('offline-access-token')
elif args[:2] == ['auth', 'print-identity-token']:
    if '--audiences=' + config['url'] not in args:
        raise SystemExit('FORBIDDEN: wrong token audience')
    print('offline-outsider-token' if any('outsider' in a for a in args) else 'offline-member-token')
elif args[:2] == ['projects', 'describe']:
    print(config['project_number'] if any('value(' in a for a in args) else json.dumps({'projectNumber':config['project_number'],'projectId':os.environ['PROJECT']}))
elif args[:3] == ['run', 'services', 'describe']:
    print(json.dumps(config['service']))
elif args[:3] == ['run', 'revisions', 'describe']:
    if args[3] != config['revision']['metadata']['name']:
        raise SystemExit('FORBIDDEN: selected the latest candidate instead of serving revision')
    print(json.dumps(config['revision']))
else:
    raise SystemExit('FORBIDDEN cloud command: ' + repr(args))
'''
        for command in ("gcloud", "git", "curl"):
            write(self.bin / command, fake, executable=True)

    def update_cloud_fixture(self):
        write(Path(self.env["RAG_TEST_CONFIG"]), json.dumps({
            "service": self.service, "revision": self.revision,
            "url": API_URL, "project_number": PROJECT_NUMBER,
        }))

    def write_graph(self, with_ask=False):
        before = {
            "metadata": {"name": "documind-api", "namespace": PROJECT_NUMBER},
            "spec": {"template": {"spec": {"containers": [{"env": [
                {"name": "GOOGLE_CLOUD_PROJECT", "value": PROJECT},
                {"name": "EMBEDDING_MODEL", "value": "fixture-embedding-model"},
                {"name": "REGION", "value": "us-central1"}]}]}}},
        }
        write(self.graph / "api-before.json", json.dumps(before))
        write(self.graph / "acme-pin-before.json", json.dumps({"retrieval_backend": "rag_engine"}))
        evidence = self.deploy / "operator-evidence"
        write(evidence / "graph-source.json", json.dumps({"project": PROJECT, "source_commit": self.commit}))
        write(evidence / "graph-source.env", exports({
            "SOURCE_COMMIT": self.commit, "GIT_SHA": self.commit,
            "RAG_SOURCE_REPO": self.repo, "DEMO_ROOT": self.deploy,
        }))
        if with_ask:
            write(self.graph / "ask-spanner.json", json.dumps({
                "backend": "spanner", "seeded_by": "meaning", "question": QUESTION,
                "seed_distance": 0.37, "seeds": [{"node_id": "node1"}],
                "chunk_ids": ["acme:document#0"],
            }))

    def run_shell(self, commands, *, env=None, success=True):
        # These routing/state tests intentionally use a lightweight fake venv and
        # stub only the dependency boundary. OperatorDependencyTests below run
        # the real checker with a real venv and offline package fixtures.
        dependency_stub = """
rag_check_python_dependencies() {
  if [[ ${RAG_TEST_DEPENDENCY_FAIL:-0} == 1 ]]; then
    _rag_stop 'fixture missing google.cloud.firestore; run rag_install_python_dependencies'
    return 1
  fi
  return 0
}
"""
        script = ("set -euo pipefail\nsource " + shlex.quote(str(SOURCE)) + "\n" +
                  dependency_stub + commands)
        result = subprocess.run(["bash", "--noprofile", "--norc", "-c", script],
                                env={**self.env, **(env or {})}, cwd=self.root,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        if success:
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
        return result

    def dump(self, names):
        return "python -c " + shlex.quote(
            "import json,os; print('STATE:' + json.dumps({k:os.environ.get(k) for k in " + repr(names) + "}))")

    def state(self, result):
        lines = [line.removeprefix("STATE:") for line in result.stdout.splitlines() if line.startswith("STATE:")]
        self.assertTrue(lines, result.stdout + result.stderr)
        return json.loads(lines[-1])

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def save(self, *, graph=False):
        env = {"GRAPH_EVIDENCE_DIR": str(self.graph), "SPANNER_INSTANCE": "documind-graph",
               "SPANNER_DATABASE": "documind", "GRAPH_SEED_K": "7",
               "GRAPH_QUESTION": QUESTION, "GRAPH_SEED_DISTANCE": "0.37"} if graph else {}
        self.run_shell("rag_save_session", env=env)

    def test_source_only_defines_functions(self):
        result = self.run_shell("declare -F rag_resume rag_resume_graph rag_save_session rag_promote_ready_api\nprintf '%s\\n' \"$SOURCE_COMMIT\"")
        self.assertIn(self.commit, result.stdout)
        self.assertEqual(self.calls(), [])
        self.assertFalse(self.saved.exists())

    def test_save_is_private_allowlisted_and_round_trips_quotes(self):
        self.run_shell("rag_save_session", env={
            "GRAPH_EVIDENCE_DIR": str(self.graph), "GRAPH_QUESTION": QUESTION,
            "GRAPH_SEED_K": "7", "GRAPH_SEED_DISTANCE": "0.37",
            "DOCUMIND_ID_TOKEN": "do-not-save-member", "DOCUMIND_OUTSIDER_TOKEN": "do-not-save-outsider",
            "API_KEY": "do-not-save-api-key", "RANDOM_UNRELATED_SETTING": "do-not-save-unrelated",
        })
        self.assertEqual(stat.S_IMODE(self.saved.stat().st_mode), 0o600)
        raw = self.saved.read_text()
        for name in ("DOCUMIND_ID_TOKEN", "DOCUMIND_OUTSIDER_TOKEN", "API_KEY", "RANDOM_UNRELATED_SETTING"):
            self.assertNotIn(name, raw)
        for secret in ("do-not-save-member", "do-not-save-outsider", "do-not-save-api-key", "do-not-save-unrelated"):
            self.assertNotIn(secret, raw)
        result = self.run_shell("source " + shlex.quote(str(self.saved)) + "\n" + self.dump(["GRAPH_QUESTION", "GRAPH_EVIDENCE_DIR", "DEMO_ROOT"]))
        state = self.state(result)
        self.assertEqual(state["GRAPH_QUESTION"], QUESTION)
        self.assertEqual(state["GRAPH_EVIDENCE_DIR"], str(self.graph))
        self.assertEqual(state["DEMO_ROOT"], str(self.deploy))

    def test_resume_keeps_snapshot_and_uses_serving_revision_audience(self):
        self.save()
        result = self.run_shell("rag_resume\ndeclare -F rag_get_file rag_refresh_demo_tokens rag_promote_ready_api\n" +
                                self.dump(["SOURCE_COMMIT", "GIT_SHA", "PROJECT", "API_URL", "DOCUMIND_ID_TOKEN", "GOOGLE_CLOUD_QUOTA_PROJECT", "DEPLOY_ROOT"]),
                                env={"API_URL": "https://stale.example.test", "DOCUMIND_ID_TOKEN": "expired"})
        state = self.state(result)
        self.assertEqual(state["SOURCE_COMMIT"], self.commit)
        self.assertEqual(state["GIT_SHA"], self.commit)
        self.assertEqual(state["PROJECT"], PROJECT)
        self.assertEqual(state["API_URL"], API_URL)
        self.assertEqual(state["DOCUMIND_ID_TOKEN"], "offline-member-token")
        self.assertEqual(state["GOOGLE_CLOUD_QUOTA_PROJECT"], PROJECT)
        self.assertEqual(state["DEPLOY_ROOT"], str(self.deploy))
        self.assertFalse(any("fetch" in call or "update-traffic" in call or "deploy" in call for call in self.calls()))

    def test_resume_rejects_source_session_changed_since_save(self):
        self.save()
        self.write_source_session(SOURCE_COMMIT="a" * 40, GIT_SHA="a" * 40)
        self.run_shell("rag_resume", success=False)
        self.assertFalse(any(call[0] == "curl" for call in self.calls()))

    def test_resume_real_git_helper_preserves_explicit_legacy_source(self):
        self.save()
        write(self.session_home / "rag-git-source.sh", (SOURCE.parent / "git-source.sh").read_text())
        original_session = (self.session_home / "rag-source-session.env").read_bytes()
        keys = ["SOURCE_BRANCH", "SOURCE_COMMIT", "GIT_SHA", "RAG_SOURCE_REPO", "DEMO_ROOT"]
        result = self.run_shell("rag_resume --shell-only\n" + self.dump(keys),
                                env={"SOURCE_BRANCH": "rag-production-hardening"})
        self.assertEqual(self.state(result), {key: self.env[key] for key in keys})
        self.assertEqual((self.session_home / "rag-source-session.env").read_bytes(), original_session)
        self.assertFalse(any(word in call for call in self.calls()
                             for word in ("fetch", "clone", "checkout", "switch", "reset")))

    def test_resume_rejects_split_serving_traffic(self):
        self.save()
        self.service["status"]["traffic"] = [{"revisionName": "serving-old", "percent": 50}, {"revisionName": "other", "percent": 50}]
        self.update_cloud_fixture()
        self.run_shell("rag_resume", success=False)
        self.assertFalse(any(call[:3] == ["gcloud", "auth", "print-identity-token"] for call in self.calls()))

    def test_resume_dependency_failure_stops_before_cloud_or_adc(self):
        self.save()
        self.log.unlink(missing_ok=True)
        result = self.run_shell("rag_resume --shell-only",
                                env={"RAG_TEST_DEPENDENCY_FAIL": "1", "RAG_TEST_ADC_FAIL": "1"},
                                success=False)
        self.assertIn("missing google.cloud.firestore", result.stderr)
        self.assertNotIn("Python ADC failed", result.stderr)
        self.assertFalse(any(call[0] in ("gcloud", "curl") for call in self.calls()))

    def test_resume_adc_failure_stops_before_api_requests(self):
        self.save()
        self.run_shell("rag_resume", env={"RAG_TEST_ADC_FAIL": "1"}, success=False)
        self.assertFalse(any(call[0] == "curl" for call in self.calls()))

    def test_failed_save_preserves_previous_private_state(self):
        self.save()
        original = self.saved.read_bytes()
        self.write_source_session(SOURCE_COMMIT="a" * 40, GIT_SHA="a" * 40)
        # Real runbook command chains suppress Bash errexit inside functions.
        # Required validation must propagate errors explicitly, not rely on -e.
        self.run_shell("rag_save_session && printf 'UNEXPECTED: saved invalid snapshot\\n'", success=False)
        self.assertEqual(self.saved.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(self.saved.stat().st_mode), 0o600)

    def test_save_accepts_fresh_graph_directory_before_c1_backup(self):
        fresh = self.root / "just-selected-graph-directory"
        fresh.mkdir()
        self.run_shell("rag_save_session", env={"GRAPH_EVIDENCE_DIR": str(fresh)})
        result = self.run_shell("source " + shlex.quote(str(self.saved)) + "\n" + self.dump(["GRAPH_EVIDENCE_DIR"]))
        self.assertEqual(self.state(result)["GRAPH_EVIDENCE_DIR"], str(fresh))
        self.assertEqual(list(fresh.iterdir()), [])

    def test_save_does_not_block_rerunning_c3_with_failed_old_result(self):
        failed = self.graph / "ask-spanner.json"
        failed.write_text("previous command produced incomplete JSON {")
        original = failed.read_bytes()
        self.save(graph=True)
        self.assertEqual(failed.read_bytes(), original)

    def test_save_skips_legacy_graph_pointer_for_another_project(self):
        self.save()
        original = self.saved.read_bytes()
        write(self.session_home / "rag-graph-resume.env", exports({
            "RAG_GRAPH_SAVED_PROJECT": "another-project", "GRAPH_EVIDENCE_DIR": self.graph,
            "GRAPH_SEED_K": "9",
        }))
        self.run_shell("rag_save_session")
        self.assertEqual(self.saved.read_bytes(), original)
        result = self.run_shell("source " + shlex.quote(str(self.saved)) + "\n" + self.dump(["GRAPH_EVIDENCE_DIR"]))
        self.assertIsNone(self.state(result)["GRAPH_EVIDENCE_DIR"])

    def test_graph_restore_rejects_foreign_legacy_pointer(self):
        self.save()
        write(self.session_home / "rag-graph-resume.env", exports({
            "RAG_GRAPH_SAVED_PROJECT": "another-project", "GRAPH_EVIDENCE_DIR": self.graph,
        }))
        self.run_shell("rag_resume --shell-only\nrag_resume_graph", success=False)

    def test_shell_only_restores_saved_functions_without_api_lookup(self):
        self.save()
        write(self.session_home / "rag-wait-source.sh", "rag_wait_source() { :; }\nrag_select_smoke_fixture() { :; }\n")
        write(self.session_home / "rag-upload-all-text-pdf.sh", "rag_upload_all_text_pdf() { :; }\n")
        self.run_shell("rag_resume --shell-only\ndeclare -F rag_wait_source rag_select_smoke_fixture rag_upload_all_text_pdf")
        self.assertFalse(any(call[0] == "curl" or call[:2] == ["gcloud", "run"] for call in self.calls()))

    def test_graph_resume_before_c3_preserves_original_backups(self):
        self.save(graph=True)
        before = {name: (self.graph / name).read_bytes() for name in ("api-before.json", "acme-pin-before.json")}
        result = self.run_shell("rag_resume --shell-only\nrag_resume_graph\n" + self.dump(["GRAPH_EVIDENCE_DIR", "GRAPH_SEED_K", "EMBEDDING_MODEL", "EMBED_LOCATION"]))
        state = self.state(result)
        self.assertEqual(state["GRAPH_EVIDENCE_DIR"], str(self.graph))
        self.assertEqual(state["GRAPH_SEED_K"], "7")
        self.assertEqual(state["EMBEDDING_MODEL"], "fixture-embedding-model")
        self.assertEqual(state["EMBED_LOCATION"], "us-central1")
        for name, content in before.items():
            self.assertEqual((self.graph / name).read_bytes(), content)
        self.assertFalse((self.graph / "ask-spanner.json").exists())

    def test_graph_resume_uses_successful_c3_question_and_distance(self):
        self.write_graph(with_ask=True)
        # Legacy operator settings saved K but not the question or distance.
        self.run_shell("rag_save_session", env={"GRAPH_EVIDENCE_DIR": str(self.graph), "GRAPH_SEED_K": "7"})
        result = self.run_shell("rag_resume --shell-only\nrag_resume_graph\n" + self.dump(["GRAPH_QUESTION", "GRAPH_SEED_DISTANCE", "GRAPH_SEED_K"]),
                                env={"GRAPH_QUESTION": "stale question", "GRAPH_SEED_DISTANCE": "0.99", "GRAPH_SEED_K": "99"})
        self.assertEqual(self.state(result), {"GRAPH_QUESTION": QUESTION, "GRAPH_SEED_DISTANCE": "0.37", "GRAPH_SEED_K": "7"})

    def test_graph_resume_rejects_old_c3_after_new_request_settings_saved(self):
        self.write_graph(with_ask=True)
        old_result = (self.graph / "ask-spanner.json").read_bytes()
        self.run_shell("rag_save_session", env={
            "GRAPH_EVIDENCE_DIR": str(self.graph), "GRAPH_SEED_K": "7",
            "GRAPH_QUESTION": "A newly selected question before rerunning C3?", "GRAPH_SEED_DISTANCE": "0.25",
        })
        result = self.run_shell("rag_resume --shell-only\nrag_resume_graph", success=False)
        self.assertIn("C3", result.stdout + result.stderr)
        self.assertEqual((self.graph / "ask-spanner.json").read_bytes(), old_result)

    def test_graph_resume_rejects_wrong_project_manifest(self):
        self.save(graph=True)
        file = self.deploy / "operator-evidence" / "graph-source.json"
        data = json.loads(file.read_text()); data["project"] = "another-project"
        file.write_text(json.dumps(data))
        self.run_shell("rag_resume --shell-only\nrag_resume_graph", success=False)

    def test_graph_resume_rejects_graph_commit_disagreement(self):
        self.save(graph=True)
        file = self.deploy / "operator-evidence" / "graph-source.json"
        data = json.loads(file.read_text()); data["source_commit"] = "b" * 40
        file.write_text(json.dumps(data))
        self.run_shell("rag_resume --shell-only\nrag_resume_graph", success=False)

    def test_graph_snapshot_does_not_replace_original_parent_snapshot(self):
        write(self.repo / "README", "A later graph-only source snapshot.\n")
        subprocess.run([self.real_git, "-C", str(self.repo), "-c", "user.name=Offline Test",
                        "-c", "user.email=offline@example.test", "commit", "--quiet", "-am", "graph fix"], check=True)
        graph_commit = subprocess.check_output(
            [self.real_git, "-C", str(self.repo), "rev-parse", "HEAD"], text=True).strip()
        self.assertNotEqual(graph_commit, self.commit)
        evidence = self.deploy / "operator-evidence"
        write(evidence / "graph-source.json", json.dumps({"project": PROJECT, "source_commit": graph_commit}))
        write(evidence / "graph-source.env", exports({
            "DEMO_ROOT": self.deploy, "RAG_SOURCE_REPO": self.repo,
            "SOURCE_COMMIT": graph_commit, "GIT_SHA": graph_commit,
        }))
        self.save(graph=True)
        result = self.run_shell("rag_resume --shell-only\nrag_resume_graph\n" + self.dump(["SOURCE_COMMIT", "GIT_SHA"]))
        self.assertEqual(self.state(result), {"SOURCE_COMMIT": self.commit, "GIT_SHA": self.commit})

    def test_graph_resume_restores_documented_embedding_defaults(self):
        before = self.graph / "api-before.json"
        saved = json.loads(before.read_text())
        saved["spec"]["template"]["spec"]["containers"][0]["env"] = []
        before.write_text(json.dumps(saved))
        self.save(graph=True)
        result = self.run_shell("rag_resume --shell-only\nrag_resume_graph\n" + self.dump(["EMBEDDING_MODEL", "EMBED_LOCATION"]))
        self.assertEqual(self.state(result), {"EMBEDDING_MODEL": "text-embedding-005", "EMBED_LOCATION": "us-central1"})

    def test_graph_resume_does_not_infer_seed_k_from_result_count(self):
        self.write_graph(with_ask=True)
        self.run_shell("rag_save_session", env={"GRAPH_EVIDENCE_DIR": str(self.graph)})
        result = self.run_shell("rag_resume --shell-only\nrag_resume_graph\n" + self.dump(["GRAPH_SEED_K", "GRAPH_QUESTION"]),
                                env={"GRAPH_SEED_K": "99"})
        self.assertIsNone(self.state(result)["GRAPH_SEED_K"])
        self.assertEqual(self.state(result)["GRAPH_QUESTION"], QUESTION)
        self.assertIn("GRAPH_SEED_K", result.stdout)

    def test_graph_resume_before_pin_checkpoint_does_not_create_backup(self):
        pin = self.graph / "acme-pin-before.json"
        pin.unlink()
        self.save(graph=True)
        result = self.run_shell("rag_resume --shell-only\nrag_resume_graph")
        self.assertIn("C1", result.stdout)
        self.assertFalse(pin.exists())

    def test_graph_resume_rejects_invalid_c3_evidence(self):
        self.write_graph(with_ask=True)
        self.save(graph=True)
        file = self.graph / "ask-spanner.json"
        for modification in ({"seed_distance": -1}, {"seed_distance": True}, {"chunk_ids": ["zeta:secret#0"]}, {"seeds": []}):
            with self.subTest(modification=modification):
                self.write_graph(with_ask=True)
                data = json.loads(file.read_text()); data.update(modification)
                file.write_text(json.dumps(data))
                self.run_shell("rag_resume --shell-only\nrag_resume_graph", success=False)


    def isolated_cli_path(self):
        return str(self.session_home / 'rag-shell-venv' / 'bin') + os.pathsep + str(self.bin)

    def write_terraform(self, path, version='1.9.0', *, raw=None, exit_code=0):
        payload = json.dumps({'terraform_version': version}) if raw is None else raw
        write(path, '#!' + sys.executable + '\n' +
              'import sys\n' +
              "assert sys.argv[1:] == ['version', '-json'], 'unexpected Terraform operation'\n" +
              'print(' + repr(payload) + ')\n' +
              f'raise SystemExit({exit_code})\n', executable=True)

    def test_require_terraform_missing_fails_with_step_one_guidance(self):
        result = self.run_shell('export PATH=' + shlex.quote(self.isolated_cli_path()) +
                                '\nrag_require_terraform', success=False)
        self.assertIn('Step 1', result.stderr)
        self.assertIn('not installed or is outside PATH', result.stderr)
        self.assertEqual(self.calls(), [])

    def test_require_terraform_restores_user_bin_without_shadowing_venv(self):
        local_bin = self.session_home / '.local' / 'bin'
        self.write_terraform(local_bin / 'terraform', '1.9.8')
        write(local_bin / 'python', '#!' + sys.executable + "\nraise SystemExit('wrong Python selected')\n", executable=True)
        result = self.run_shell('export PATH=' + shlex.quote(self.isolated_cli_path()) +
                                '\nrag_require_terraform\ncommand -v python\n' + self.dump(['PATH']))
        restored = self.state(result)['PATH'].split(os.pathsep)
        self.assertEqual(restored[0], str(self.session_home / 'rag-shell-venv' / 'bin'))
        self.assertEqual(restored[-1], str(local_bin))
        self.assertIn('Terraform 1.9.8', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_require_terraform_recovers_known_system_location(self):
        # RAG_SESSION_HOME maps known system locations into the isolated test root.
        binary = self.session_home / 'usr' / 'local' / 'bin' / 'terraform'
        self.write_terraform(binary, '1.10.2')
        result = self.run_shell('export PATH=' + shlex.quote(self.isolated_cli_path()) +
                                '\nrag_require_terraform\ncommand -v terraform')
        self.assertIn(str(binary), result.stdout)
        self.assertEqual(self.calls(), [])

    def test_require_terraform_respects_current_path_and_rejects_old_version(self):
        self.write_terraform(self.bin / 'terraform', '1.8.9')
        self.write_terraform(self.session_home / '.local' / 'bin' / 'terraform', '1.10.2')
        result = self.run_shell('export PATH=' + shlex.quote(self.isolated_cli_path()) +
                                '\nrag_require_terraform', success=False)
        self.assertIn('1.8.9', result.stderr)
        self.assertIn('Step 1', result.stderr)
        self.assertNotIn('PASS: Terraform', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_require_terraform_version_and_command_failures_are_not_success(self):
        for kwargs in ({'version': '1.9.0-rc1'}, {'raw': 'not JSON'}, {'exit_code': 1}):
            with self.subTest(kwargs=kwargs):
                self.write_terraform(self.bin / 'terraform', **kwargs)
                result = self.run_shell('export PATH=' + shlex.quote(self.isolated_cli_path()) +
                                        '\nrag_require_terraform', success=False)
                self.assertIn('Step 1', result.stderr)
                self.assertNotIn('PASS: Terraform', result.stdout)
        self.assertEqual(self.calls(), [])

    def test_api_resume_warns_but_succeeds_without_terraform(self):
        self.save()
        write(self.session_home / 'rag-shell-venv' / 'bin' / 'activate', exports({
            'VIRTUAL_ENV': self.session_home / 'rag-shell-venv', 'PATH': self.isolated_cli_path(),
        }))
        result = self.run_shell('rag_resume\n' + self.dump(['API_URL']))
        self.assertEqual(self.state(result)['API_URL'], API_URL)
        self.assertIn('WARNING: Terraform', result.stderr)
        self.assertIn('API-only resume can continue', result.stderr)
        self.assertIn('PASS: API health', result.stdout)

    def test_resume_recovers_user_terraform_path_and_keeps_venv_python(self):
        self.save()
        local_bin = self.session_home / '.local' / 'bin'
        self.write_terraform(local_bin / 'terraform', '1.9.0')
        write(self.session_home / 'rag-shell-venv' / 'bin' / 'activate', exports({
            'VIRTUAL_ENV': self.session_home / 'rag-shell-venv', 'PATH': self.isolated_cli_path(),
        }))
        result = self.run_shell('rag_resume --shell-only\nrag_require_terraform\n' + self.dump(['PATH']))
        restored = self.state(result)['PATH'].split(os.pathsep)
        self.assertEqual(restored[0], str(self.session_home / 'rag-shell-venv' / 'bin'))
        self.assertEqual(restored[-1], str(local_bin))
        self.assertIn('PASS: Terraform 1.9.0', result.stdout)
        self.assertNotIn('WARNING: Terraform', result.stderr)


# Source manifests are used only for offline distribution-version fixtures.
OPERATOR_MANIFEST_ROOT = SOURCE.parent.parent


@unittest.skipUnless(sys.version_info[:2] == (3, 12), "Operator environment requires Python 3.12")
class OperatorDependencyTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix='rag-dependencies-')
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.home = self.root / 'session home'
        self.deploy = self.root / 'deploy sources'
        self.venv = self.home / 'rag-shell-venv'
        subprocess.run([sys.executable, '-m', 'venv', '--without-pip', str(self.venv)], check=True)
        self.site = self.venv / 'lib' / f'python{sys.version_info.major}.{sys.version_info.minor}' / 'site-packages'
        self.log = self.root / 'pip-log.jsonl'
        source = SOURCE.read_text()
        code = source.split("<<'RAG_DEPENDENCIES' || return\n", 1)[1].split('\nRAG_DEPENDENCIES',1)[0]
        tree = ast.parse(code)
        checks = next(ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign) and any(isinstance(t,ast.Name) and t.id=='checks' for t in n.targets))
        for module, symbols in checks.items():
            parts=module.split('.')
            for i in range(1,len(parts)+1):
                folder=self.site.joinpath(*parts[:i]);folder.mkdir(parents=True,exist_ok=True)
                file=folder/'__init__.py'
                if not file.exists():file.write_text('')
            file.write_text(file.read_text()+''.join(f'{symbol} = object()\n' for symbol in symbols))
        expected={'rank-bm25':'0.2.2'}
        for relative in ('shared/requirements.txt','services/ingest/requirements.txt','services/rag-api/requirements.txt','services/mcp/requirements.txt'):
            dest=self.deploy/relative;dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(OPERATOR_MANIFEST_ROOT/relative,dest)
            for line in dest.read_text().splitlines():
                match=re.fullmatch(r'([\w.-]+)(?:\[.*?\])?==([^\s]+)',line.strip())
                if match:expected[match[1]]=match[2]
        for name,version in expected.items():
            folder=self.site/(name.replace('-','_')+'-'+version+'.dist-info');folder.mkdir()
            (folder/'METADATA').write_text(f'Metadata-Version: 2.1\nName: {name}\nVersion: {version}\n')
        pip=self.site/'pip';pip.mkdir();(pip/'__init__.py').write_text('')
        (pip/'__main__.py').write_text('''import json,os,sys
with open(os.environ['RAG_DEP_TEST_LOG'],'a') as f:f.write(json.dumps(sys.argv[1:])+'\\n')
if sys.argv[1]=='check' and os.environ.get('RAG_DEP_TEST_CONFLICT'):
 print('fixture: broken dependency',file=sys.stderr);sys.exit(1)
print('fixture pip success')
''')
        self.env={**os.environ,'RAG_SESSION_HOME':str(self.home),'DEMO_ROOT':str(self.deploy),'RAG_DEP_TEST_LOG':str(self.log)}
        self.env.pop('PYTHONPATH',None)
        self.env.pop('VIRTUAL_ENV',None)

    def shell(self, command, activate=True, extra=None):
        body=f'source {shlex.quote(str(SOURCE))}\n'
        if activate:body+=f'source {shlex.quote(str(self.venv/"bin/activate"))}\nhash -r\n'
        body+=command+'\n'
        return subprocess.run(['bash','--noprofile','--norc','-c',body],env={**self.env,**(extra or {})},text=True,capture_output=True)

    def test_complete_environment_passes_without_network(self):
        r=self.shell('rag_check_python_dependencies')
        self.assertEqual(r.returncode,0,r.stderr)
        self.assertIn('local Python dependencies are ready',r.stdout)
        self.assertEqual([json.loads(x) for x in self.log.read_text().splitlines()],[['check']])

    def test_missing_firestore_is_dependency_error_before_pip_check(self):
        shutil.rmtree(self.site/'google/cloud/firestore')
        r=self.shell('rag_check_python_dependencies')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('google.cloud.firestore: ModuleNotFoundError',r.stderr)
        self.assertIn('rag_install_python_dependencies',r.stderr)
        self.assertFalse(self.log.exists())

    def test_mixed_active_interpreter_is_rejected(self):
        r=self.shell('rag_check_python_dependencies',activate=False)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('python on PATH is outside rag-shell-venv',r.stderr)
        self.assertFalse(self.log.exists())

    def test_environment_prefix_must_be_real_venv(self):
        (self.venv/'pyvenv.cfg').rename(self.venv/'pyvenv.saved')
        r=self.shell('rag_check_python_dependencies')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('required Python 3.12 virtual environment',r.stderr)

    def test_installed_version_must_match_repository_pin(self):
        p=next(self.site.glob('google_cloud_firestore-*.dist-info/METADATA'))
        p.write_text(p.read_text().replace('Version: 2.30.0','Version: 2.29.0'))
        r=self.shell('rag_check_python_dependencies')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('google-cloud-firestore: installed 2.29.0, source requires 2.30.0',r.stderr)

    def test_pip_check_failure_does_not_print_ready(self):
        r=self.shell('rag_check_python_dependencies',extra={'RAG_DEP_TEST_CONFLICT':'1'})
        self.assertNotEqual(r.returncode,0)
        self.assertIn('pip check found an inconsistent environment',r.stderr)
        self.assertNotIn('local Python dependencies are ready',r.stdout)

    def test_installer_uses_one_transaction_then_check(self):
        r=self.shell('rag_install_python_dependencies',activate=False)
        self.assertEqual(r.returncode,0,r.stderr)
        calls=[json.loads(x) for x in self.log.read_text().splitlines()]
        self.assertEqual(len(calls),2)
        self.assertEqual(calls[0][0],'install')
        self.assertEqual(calls[0].count('-r'),4)
        self.assertIn('rank-bm25==0.2.2',calls[0]);self.assertIn('numpy',calls[0])
        self.assertEqual(calls[1],['check'])
        self.assertNotIn('--user',calls[0])

    def test_missing_manifest_stops_installer_before_pip(self):
        (self.deploy/'services/mcp/requirements.txt').unlink()
        r=self.shell('rag_install_python_dependencies')
        self.assertNotEqual(r.returncode,0)
        self.assertIn('Complete Git bulk-copy',r.stderr)
        self.assertFalse(self.log.exists())

    def test_resume_missing_firestore_stops_before_cloud_or_adc(self):
        shutil.rmtree(self.site/'google/cloud/firestore')
        saved=f'export DEMO_ROOT={shlex.quote(str(self.deploy))}\nexport PROJECT=fixture-project\nexport REGION=asia-south1\n'
        (self.home/'rag-resume.env').write_text(saved)
        (self.home/'rag-source-session.env').write_text(saved)
        (self.home/'rag-git-source.sh').write_text('# fixture only\n')
        cloud=self.root/'cloud-called'
        command=f'''_rag_verify_base_snapshot() {{ return 0; }}
gcloud() {{ touch {shlex.quote(str(cloud))}; return 99; }}
rag_resume --shell-only'''
        r=self.shell(command)
        self.assertNotEqual(r.returncode,0)
        self.assertIn('operator Python dependencies',r.stderr)
        self.assertNotIn('Complete application-default login',r.stderr)
        self.assertFalse(cloud.exists())



if __name__ == "__main__":
    unittest.main()
