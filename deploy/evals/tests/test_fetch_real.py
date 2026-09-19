"""Offline regression checks for resumable, verified public-PDF downloads.

Run with ``python -m unittest discover -s evals/tests -p test_fetch_real.py``
from deploy/. The tests use temporary corpus files and never contact a URL.
The same pypdf dependency as the downloader is required for page validation.
"""
import contextlib
import errno
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import socket
import ssl
import subprocess
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from pypdf import PdfWriter


MODULE_PATH = Path(__file__).resolve().parents[1] / "fetch_real.py"
spec = importlib.util.spec_from_file_location("fetch_real_under_test", MODULE_PATH)
fetch = importlib.util.module_from_spec(spec)
spec.loader.exec_module(fetch)
URL = "https://publisher.example.test/act.pdf"


def pdf_bytes(pages=1):
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=200, height=200)
    output = io.BytesIO()
    writer.write(output)
    return output.getvalue()


class DownloadRetryTests(unittest.TestCase):
    def setUp(self):
        self.data = pdf_bytes()
        self.output = io.StringIO()

    def test_transient_dns_then_success_retries_without_losing_url(self):
        transient = urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"))
        with patch.object(fetch.urllib.request, "urlopen", side_effect=[transient, io.BytesIO(self.data)]) as urlopen, \
                patch.object(fetch.time, "sleep") as sleep, contextlib.redirect_stderr(self.output):
            self.assertEqual(fetch.download(URL), self.data)
        self.assertEqual(urlopen.call_count, 2)
        self.assertEqual(sleep.call_count, 1)
        self.assertIn(URL, self.output.getvalue())

    def test_retry_exhaustion_is_bounded(self):
        transient = urllib.error.URLError(socket.gaierror(socket.EAI_AGAIN, "temporary DNS failure"))
        with patch.object(fetch.urllib.request, "urlopen", side_effect=transient) as urlopen, \
                patch.object(fetch.time, "sleep") as sleep, contextlib.redirect_stderr(self.output):
            with self.assertRaises(Exception):
                fetch.download(URL)
        self.assertEqual(urlopen.call_count, fetch.DOWNLOAD_ATTEMPTS)
        self.assertEqual(sleep.call_count, fetch.DOWNLOAD_ATTEMPTS - 1)
        self.assertIn(URL, self.output.getvalue())

    def test_rate_limit_and_server_error_are_retryable(self):
        for status in (429, 503):
            with self.subTest(status=status):
                error = urllib.error.HTTPError(URL, status, "try later", {}, None)
                with patch.object(fetch.urllib.request, "urlopen", side_effect=[error, io.BytesIO(self.data)]) as urlopen, \
                        patch.object(fetch.time, "sleep"), contextlib.redirect_stderr(self.output):
                    self.assertEqual(fetch.download(URL), self.data)
                self.assertEqual(urlopen.call_count, 2)

    def test_permanent_http_and_tls_errors_do_not_retry(self):
        errors = [urllib.error.HTTPError(URL, status, "permanent failure", {}, None) for status in (403, 404)]
        errors += [ssl.SSLCertVerificationError("certificate verification failed"),
                   urllib.error.URLError(ssl.SSLCertVerificationError("certificate verification failed"))]
        for error in errors:
            with self.subTest(error=repr(error)):
                with patch.object(fetch.urllib.request, "urlopen", side_effect=error) as urlopen, \
                        patch.object(fetch.time, "sleep") as sleep:
                    with self.assertRaises(Exception):
                        fetch.download(URL)
                self.assertEqual(urlopen.call_count, 1)
                sleep.assert_not_called()

    def test_non_pdf_response_is_not_retried(self):
        with patch.object(fetch.urllib.request, "urlopen", return_value=io.BytesIO(b"<html>blocked</html>")) as urlopen, \
                patch.object(fetch.time, "sleep") as sleep:
            with self.assertRaises(Exception):
                fetch.download(URL)
        self.assertEqual(urlopen.call_count, 1)
        sleep.assert_not_called()


class PdfOnlyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rag-pdf-tests-")
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.corpus = self.root / "corpus"
        self.sources_path = self.root / "real_sources.json"
        self.manifest = self.root / "manifest.json"
        self.data = pdf_bytes()
        self.source = self.source_record("act", self.data, ["acme", "zeta"])
        self.sources = [self.source]
        self.sources_path.write_text(json.dumps(self.sources, indent=2) + "\n")
        self.manifest.write_text('{"frozen_manifest": true}\n')
        for tenant in ("acme", "zeta", "globex"):
            (self.corpus / tenant).mkdir(parents=True)
            (self.corpus / tenant / "act.md").write_text("Frozen Git text mirror; keep byte-for-byte.\n")
        self.pristine = {path: path.read_bytes() for path in
                         [self.sources_path, self.manifest, *self.corpus.glob("*/*.md")]}
        for name, value in (("CORPUS", str(self.corpus)), ("SOURCES", str(self.sources_path))):
            patcher = patch.object(fetch, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.out, self.err = io.StringIO(), io.StringIO()

    @staticmethod
    def source_record(slug, data, tenants, pages=1):
        return {"slug": slug, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data),
                "pages": pages, "tenants": tenants, "source_url": URL.replace("act.pdf", slug + ".pdf"),
                "text_layer": True, "title": "Offline fixture Act", "publisher": "Fixture publisher",
                "retrieved": "2026-09-18"}

    def path(self, tenant, slug="act"):
        return self.corpus / tenant / (slug + ".pdf")

    def main(self, args):
        with contextlib.redirect_stdout(self.out), contextlib.redirect_stderr(self.err):
            return fetch.main(args)

    def assert_frozen_files(self):
        for path, original in self.pristine.items():
            self.assertEqual(path.read_bytes(), original, str(path))

    def git(self, repository, *args):
        """Use a local repository only; test fixtures never fetch or push."""
        return subprocess.run(
            ["git", "-C", str(repository), "-c", "user.name=PDF fixture",
             "-c", "user.email=pdf-fixture@example.test", "-c", "commit.gpgsign=false",
             "-c", "core.hooksPath=/dev/null", *args],
            check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        ).stdout.decode().strip()

    def committed_pdf(self, data=None, tenant="acme"):
        repository = self.root / "source-checkout"
        repository.mkdir()
        self.git(repository, "init", "--quiet")
        path = repository / "deploy" / "evals" / "corpus" / tenant / "act.pdf"
        path.parent.mkdir(parents=True)
        path.write_bytes(self.data if data is None else data)
        self.git(repository, "add", ".")
        self.git(repository, "commit", "--quiet", "-m", "Frozen PDF fixture")
        return repository, self.git(repository, "rev-parse", "HEAD"), path

    @staticmethod
    def git_args(repository, commit, *, offline=False):
        args = ["--pdf-only", "--git-repo", str(repository), "--git-ref", commit]
        return args + (["--offline"] if offline else [])

    def test_reuses_valid_copy_from_any_tenant(self):
        self.path("zeta").write_bytes(self.data)
        original_mtime = self.path("zeta").stat().st_mtime_ns
        with patch.object(fetch, "download") as download:
            self.assertEqual(self.main(["--pdf-only"]), 0)
        download.assert_not_called()
        self.assertEqual(self.path("acme").read_bytes(), self.data)
        self.assertEqual(self.path("zeta").stat().st_mtime_ns, original_mtime)
        self.assert_frozen_files()

    def test_partial_resume_only_publishes_missing_tenant_copy(self):
        self.source["tenants"].append("globex")
        self.sources_path.write_text(json.dumps(self.sources))
        for tenant in ("acme", "zeta"):
            self.path(tenant).write_bytes(self.data)
        mtimes = {tenant: self.path(tenant).stat().st_mtime_ns for tenant in ("acme", "zeta")}
        with patch.object(fetch, "download") as download:
            self.assertEqual(self.main(["--pdf-only"]), 0)
        download.assert_not_called()
        self.assertEqual(self.path("globex").read_bytes(), self.data)
        for tenant, original in mtimes.items():
            self.assertEqual(self.path(tenant).stat().st_mtime_ns, original)

    def test_existing_mismatch_is_reported_and_never_overwritten(self):
        bad = b"an unrelated existing document"
        self.path("acme").write_bytes(bad)
        self.path("zeta").write_bytes(self.data)
        with patch.object(fetch, "download") as download:
            self.assertNotEqual(self.main(["--pdf-only"]), 0)
        download.assert_not_called()
        self.assertEqual(self.path("acme").read_bytes(), bad)
        self.assertEqual(self.path("zeta").read_bytes(), self.data)
        self.assert_frozen_files()

    def test_new_download_never_changes_markdown_or_manifests(self):
        with patch.object(fetch, "download", return_value=self.data) as download:
            self.assertEqual(self.main(["--pdf-only"]), 0)
        self.assertEqual(download.call_count, 1)
        for tenant in self.source["tenants"]:
            self.assertEqual(self.path(tenant).read_bytes(), self.data)
        self.assert_frozen_files()

    def test_failed_document_does_not_skip_other_documents(self):
        second = self.source_record("second-act", self.data, ["globex"])
        self.sources_path.write_text(json.dumps([self.source, second]))
        with patch.object(fetch, "download", side_effect=[RuntimeError("DNS retries exhausted"), self.data]) as download:
            self.assertNotEqual(self.main(["--pdf-only"]), 0)
        self.assertEqual(download.call_count, 2)
        self.assertFalse(self.path("acme").exists())
        self.assertEqual(self.path("globex", "second-act").read_bytes(), self.data)
        self.assertIn("act", self.out.getvalue() + self.err.getvalue())
        self.assertIn("DNS retries exhausted", self.out.getvalue() + self.err.getvalue())
        self.assertIn("1/2 sources complete", self.out.getvalue())
        self.assertIn("1/3 tenant copies valid", self.out.getvalue())

    def test_atomic_publish_failure_leaves_no_truncated_final_file(self):
        with patch.object(fetch, "download", return_value=self.data), \
                patch.object(fetch.os, "link", side_effect=OSError(errno.EIO, "publication failed")):
            self.assertNotEqual(self.main(["--pdf-only"]), 0)
        self.assertFalse(self.path("acme").exists())
        self.assertFalse(self.path("zeta").exists())
        # Temporary files must be removed even when final publication fails.
        remaining = [path for path in self.corpus.rglob("*") if path.is_file() and path.suffix != ".md"]
        self.assertEqual(remaining, [])
        self.assert_frozen_files()

    def test_hash_bytes_signature_and_page_count_are_all_validated(self):
        cases = [({"sha256": "0" * 64}, self.data),
                 ({"bytes": len(self.data) + 1}, self.data),
                 ({"pages": 2}, self.data)]
        not_pdf = b"not a PDF document"
        cases.append(({"sha256": hashlib.sha256(not_pdf).hexdigest(), "bytes": len(not_pdf)}, not_pdf))
        for overrides, data in cases:
            with self.subTest(overrides=overrides):
                source = {**self.source, **overrides}
                with self.assertRaises(Exception):
                    fetch.validate_pdf(source, data)
        self.assertEqual(fetch.validate_pdf(self.source, self.data), 1)

    def test_bad_downloaded_hash_does_not_publish_any_tenant_copy(self):
        with patch.object(fetch, "download", return_value=pdf_bytes(pages=2)):
            self.assertNotEqual(self.main(["--pdf-only"]), 0)
        self.assertFalse(self.path("acme").exists())
        self.assertFalse(self.path("zeta").exists())
        self.assert_frozen_files()

    def test_offline_pdf_only_reuses_copy_without_network(self):
        self.path("zeta").write_bytes(self.data)
        with patch.object(fetch, "download") as download:
            self.assertEqual(self.main(["--pdf-only", "--offline"]), 0)
        download.assert_not_called()
        self.assertEqual(self.path("acme").read_bytes(), self.data)
        self.assert_frozen_files()

    def test_offline_missing_pdf_reports_failure_without_network(self):
        with patch.object(fetch, "download") as download:
            self.assertNotEqual(self.main(["--pdf-only", "--offline"]), 0)
        download.assert_not_called()
        self.assertFalse(self.path("acme").exists())
        self.assert_frozen_files()

    def test_pdf_only_rejects_allow_drift_before_download_or_write(self):
        with patch.object(fetch, "download") as download:
            with self.assertRaises(SystemExit) as stopped:
                self.main(["--pdf-only", "--allow-drift"])
        self.assertEqual(stopped.exception.code, 2)
        download.assert_not_called()
        self.assert_frozen_files()

    def test_legacy_offline_mode_still_builds_markdown_mirrors(self):
        self.path("acme").write_bytes(self.data)
        with patch.object(fetch, "download") as download:
            self.assertEqual(self.main(["--offline"]), 0)
        download.assert_not_called()
        self.assertIn("source:", (self.corpus / "acme" / "act.md").read_text())
        self.assertIn("Fixture publisher", (self.corpus / "zeta" / "act.md").read_text())

    def test_git_recovery_uses_selected_commit_not_new_head_or_dirty_worktree(self):
        repository, original_commit, path = self.committed_pdf()
        path.write_bytes(pdf_bytes(pages=2))
        self.git(repository, "add", ".")
        self.git(repository, "commit", "--quiet", "-m", "A later, different document")
        self.assertNotEqual(self.git(repository, "rev-parse", "HEAD"), original_commit)
        dirty_bytes = b"an uncommitted, unrelated working-tree document"
        path.write_bytes(dirty_bytes)
        with patch.object(fetch, "download", side_effect=AssertionError("publisher must not be called")) as download:
            self.assertEqual(self.main(self.git_args(repository, original_commit)), 0)
        download.assert_not_called()
        for tenant in self.source["tenants"]:
            self.assertEqual(self.path(tenant).read_bytes(), self.data)
        self.assertEqual(path.read_bytes(), dirty_bytes)
        self.assert_frozen_files()

    def test_offline_git_recovery_uses_other_tenant_and_disables_lazy_fetch(self):
        repository, commit, _ = self.committed_pdf(tenant="zeta")
        original_run = subprocess.run
        with patch.object(fetch, "download") as download, \
                patch.object(fetch.subprocess, "run", wraps=original_run) as run:
            self.assertEqual(self.main(self.git_args(repository, commit, offline=True)), 0)
        download.assert_not_called()
        self.assertTrue(run.call_args_list, "Expected an actual local Git read")
        for call in run.call_args_list:
            self.assertEqual(call.kwargs.get("env", {}).get("GIT_NO_LAZY_FETCH"), "1")
        for tenant in self.source["tenants"]:
            self.assertEqual(self.path(tenant).read_bytes(), self.data)
        self.assert_frozen_files()

    def test_invalid_git_blob_falls_back_to_verified_publisher_online(self):
        repository, commit, _ = self.committed_pdf(data=pdf_bytes(pages=2))
        with patch.object(fetch, "download", return_value=self.data) as download:
            self.assertEqual(self.main(self.git_args(repository, commit)), 0)
        download.assert_called_once_with(URL)
        self.assertTrue(self.err.getvalue(), "The rejected Git candidate needs a diagnostic")
        for tenant in self.source["tenants"]:
            self.assertEqual(self.path(tenant).read_bytes(), self.data)
        self.assert_frozen_files()

    def test_invalid_git_blob_fails_offline_without_publishing_or_network(self):
        repository, commit, _ = self.committed_pdf(data=pdf_bytes(pages=2))
        with patch.object(fetch, "download") as download:
            self.assertNotEqual(self.main(self.git_args(repository, commit, offline=True)), 0)
        download.assert_not_called()
        for tenant in self.source["tenants"]:
            self.assertFalse(self.path(tenant).exists())
        self.assert_frozen_files()

    def test_valid_git_pdf_never_replaces_existing_mismatched_destination(self):
        repository, commit, _ = self.committed_pdf()
        unrelated = b"Keep this existing file even though it is not the frozen PDF"
        self.path("acme").write_bytes(unrelated)
        with patch.object(fetch, "download") as download:
            self.assertNotEqual(self.main(self.git_args(repository, commit, offline=True)), 0)
        download.assert_not_called()
        self.assertEqual(self.path("acme").read_bytes(), unrelated)
        self.assertEqual(self.path("zeta").read_bytes(), self.data)
        self.assert_frozen_files()

    def test_missing_git_commit_can_fall_back_to_publisher(self):
        repository, _, _ = self.committed_pdf()
        with patch.object(fetch, "download", return_value=self.data) as download:
            self.assertEqual(self.main(self.git_args(repository, "0" * 40)), 0)
        download.assert_called_once_with(URL)
        self.assertTrue(self.err.getvalue())
        self.assert_frozen_files()

    def test_valid_destination_copy_does_not_read_git_or_publisher(self):
        self.path("zeta").write_bytes(self.data)
        with patch.object(fetch, "git_pdf") as git_pdf, patch.object(fetch, "download") as download:
            self.assertEqual(self.main(self.git_args(self.root / "does-not-exist", "0" * 40)), 0)
        git_pdf.assert_not_called()
        download.assert_not_called()
        self.assertEqual(self.path("acme").read_bytes(), self.data)
        self.assert_frozen_files()

    def test_git_cli_requires_paired_options_pdf_only_and_immutable_commit(self):
        cases = [
            ["--pdf-only", "--git-repo", str(self.root)],
            ["--pdf-only", "--git-ref", "0" * 40],
            ["--git-repo", str(self.root), "--git-ref", "0" * 40],
            ["--pdf-only", "--git-repo", str(self.root), "--git-ref", "HEAD"],
            ["--pdf-only", "--git-repo", str(self.root), "--git-ref", "--help"],
        ]
        for args in cases:
            with self.subTest(args=args), patch.object(fetch, "git_pdf") as git_pdf, \
                    patch.object(fetch, "download") as download:
                with self.assertRaises(SystemExit) as stopped:
                    self.main(args)
                self.assertEqual(stopped.exception.code, 2)
                git_pdf.assert_not_called()
                download.assert_not_called()
        self.assert_frozen_files()


if __name__ == "__main__":
    unittest.main()
