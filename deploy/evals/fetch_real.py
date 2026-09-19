#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the REAL documents in DocuMind's corpus from the government sites that publish them.

    python deploy/evals/fetch_real.py             # download what is missing, verify, extract mirrors
    python deploy/evals/fetch_real.py --offline   # no network: rebuild the .md mirrors from the PDFs on disk
    python deploy/evals/fetch_real.py --allow-drift   # a source changed its bytes: accept and print the new sha
    python deploy/evals/fetch_real.py --pdf-only      # verify/reuse/download PDFs without changing mirrors
    python deploy/evals/fetch_real.py --offline --pdf-only  # verify and copy PDFs already on disk
    python deploy/evals/fetch_real.py --pdf-only --git-repo /path/to/source --git-ref COMMIT
        # reuse the manifest-matching committed PDF before contacting its publisher

real_sources.json lists thirteen real documents - the four Labour Codes, the Payment of Bonus,
Gratuity, Maternity Benefit (with its 2017 amendment), POSH, DPDP, IT and CGST Acts, and the
ministry's compliance handbook for employers - each with the URL it was taken from, the sha256 of the bytes
that were taken, and which tenants hold it. Every figure lesson 4.7's real-data golden rows
assert on is the statute's own wording.

Why real statutes and not a real HR handbook: an Act of Parliament is real, Indian, exactly
what an HR policy has to comply with, carries no personal data, and reproducing it is not an
infringement (Copyright Act 1957, s. 52(1)(q)). A real company's handbook fails at least one of
those tests. The tenants' own handbooks, contracts, invoice and report stay synthetic on
purpose - see README.md - because a corpus where every tenant holds the same law cannot
demonstrate tenant isolation, and an invoice with a real PAN must never be on a shared screen.

For every text-layer PDF this writes a mirror, corpus/<tenant>/<slug>.md: the pypdf text of
each page, pages separated by a form feed (\\f) so page numbers survive chunking (that is how
services/ingest/main.py counts pages for text uploads), under a provenance header. The mirror
is what the offline gate (run_eval.py, tools/check_real_corpus.py) and the notebooks' zero-cost
seed path (shared/documind_corpus.py) read. The PDF is what upload.sh pushes and what the
ingest worker and lesson 4.1 parse with Document AI. posh_act_2013 is a scanned Gazette with no
text layer, so it gets no mirror: it is the document only the OCR lane can read.
"""
import argparse
import errno
import hashlib
import http.client
import io
import json
import os
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")
SOURCES = os.path.join(HERE, "real_sources.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")      # the ministry's CDN answers a plain urllib agent with 403
DOWNLOAD_ATTEMPTS = 4
DOWNLOAD_TIMEOUT = 60
RETRY_BASE_SECONDS = 2

LICENCE = ("Act of Parliament. Reproducing an Act is not an infringement of copyright "
           "(Copyright Act 1957, s. 52(1)(q)). No personal data.")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _transient_download_error(error: Exception) -> bool:
    if isinstance(error, urllib.error.HTTPError):
        return error.code == 429 or 500 <= error.code <= 599
    if isinstance(error, urllib.error.URLError):
        return isinstance(error.reason, Exception) and _transient_download_error(error.reason)
    if isinstance(error, ssl.SSLError):
        return False     # certificate/TLS failures must never be bypassed
    if isinstance(error, socket.gaierror):
        return error.errno == socket.EAI_AGAIN
    if isinstance(error, (TimeoutError, ConnectionError, http.client.IncompleteRead,
                          http.client.RemoteDisconnected)):
        return True
    return isinstance(error, OSError) and error.errno in {
        errno.ETIMEDOUT, errno.ECONNRESET, errno.ECONNABORTED,
        errno.ECONNREFUSED, errno.EPIPE, errno.ENETUNREACH, errno.EHOSTUNREACH,
    }


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(1, DOWNLOAD_ATTEMPTS + 1):
        print(f"Download {attempt}/{DOWNLOAD_ATTEMPTS}: {url}", file=sys.stderr, flush=True)
        try:
            with urllib.request.urlopen(req, timeout=DOWNLOAD_TIMEOUT) as response:
                data = response.read()
            if not data.startswith(b"%PDF"):
                raise RuntimeError(f"not a PDF ({data[:40]!r})")
            return data
        except (OSError, urllib.error.URLError, http.client.HTTPException) as error:
            retry = _transient_download_error(error) and attempt < DOWNLOAD_ATTEMPTS
            if isinstance(error, urllib.error.HTTPError):
                error.close()
            if not retry:
                raise
            delay = RETRY_BASE_SECONDS * 2 ** (attempt - 1)
            print(f"Transient download error: {error}; retrying in {delay}s.", file=sys.stderr, flush=True)
            time.sleep(delay)
    raise RuntimeError("download attempt limit is invalid")


def validate_pdf(src: dict, data: bytes) -> int:
    """Validate frozen manifest metadata without extracting or changing mirrors."""
    if not data.startswith(b"%PDF-"):
        raise ValueError("missing PDF signature")
    if len(data) != src["bytes"]:
        raise ValueError(f"byte count {len(data)} differs from manifest {src['bytes']}")
    got = sha256(data)
    if got != src["sha256"]:
        raise ValueError(f"SHA-256 {got} differs from manifest {src['sha256']}")
    from pypdf import PdfReader
    pages = len(PdfReader(io.BytesIO(data)).pages)
    if pages != src["pages"]:
        raise ValueError(f"page count {pages} differs from manifest {src['pages']}")
    return pages


def _publish_pdf(path: Path, data: bytes, src: dict) -> None:
    """Atomically publish a new file; never replace an existing destination."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as output:
            temporary = Path(output.name)
            output.write(data)
            output.flush()
            os.fsync(output.fileno())
        try:
            os.link(temporary, path)     # no-clobber publication, including a concurrent writer
        except FileExistsError:
            validate_pdf(src, path.read_bytes())
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def git_pdf(src: dict, git_repo: str, git_ref: str) -> bytes:
    """Read a frozen PDF from local Git objects, never a dirty checkout or moving branch."""
    if not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", git_ref):
        raise ValueError("--git-ref must be the full resolved commit ID")
    errors = []
    env = {**os.environ, "GIT_NO_LAZY_FETCH": "1", "GIT_TERMINAL_PROMPT": "0"}
    for tenant in src["tenants"]:
        path = f"deploy/evals/corpus/{tenant}/{src['slug']}.pdf"
        try:
            result = subprocess.run(["git", "-C", git_repo, "show", f"{git_ref}:{path}"],
                                    capture_output=True, check=True, timeout=30, env=env)
            validate_pdf(src, result.stdout)
            print(f"GIT VERIFIED: {path} at {git_ref[:12]}")
            return result.stdout
        except (OSError, subprocess.SubprocessError, ValueError) as error:
            detail = error.stderr.decode(errors="replace").strip() if isinstance(error, subprocess.CalledProcessError) else str(error)
            errors.append(f"{path}: {detail}")
    raise RuntimeError("No matching committed PDF: " + "; ".join(errors))


def fetch_pdfs_only(sources: list, offline: bool = False, git_repo: str = None,
                    git_ref: str = None) -> int:
    """Fill missing tenant PDFs, preserving frozen Markdown, manifest and existing files."""
    problems = []
    for src in sources:
        paths = [Path(CORPUS, tenant, src["slug"] + ".pdf") for tenant in src["tenants"]]
        data, missing = None, []
        for path in paths:
            if not os.path.lexists(path):
                missing.append(path)
                continue
            try:
                existing = path.read_bytes()
                validate_pdf(src, existing)
                if data is None:
                    data = existing
                print(f"REUSE: {path}")
            except Exception as error:      # continue other PDFs; never repair by overwriting
                problems.append(f"{path}: existing file left unchanged: {error}")
        if missing and data is None and git_repo and git_ref:
            try:
                data = git_pdf(src, git_repo, git_ref)
            except Exception as error:
                print(f"Git source unavailable for {src['slug']}: {error}", file=sys.stderr)
        if missing and data is None:
            if offline:
                problems.append(f"{src['slug']}: no valid local tenant or selected Git copy available with --offline")
                continue
            try:
                data = download(src["source_url"])
                validate_pdf(src, data)
            except Exception as error:
                problems.append(f"{src['slug']}: download/validation failed: {error} <- {src['source_url']}")
                continue
        for path in missing:
            try:
                _publish_pdf(path, data, src)
                print(f"VERIFIED: {path}")
            except Exception as error:
                problems.append(f"{path}: could not publish verified PDF: {error}")

    # Recheck every destination: a complete earlier source is not a complete corpus.
    verified, unique = 0, 0
    expected = sum(len(src["tenants"]) for src in sources)
    for src in sources:
        source_complete = True
        for tenant in src["tenants"]:
            path = Path(CORPUS, tenant, src["slug"] + ".pdf")
            try:
                validate_pdf(src, path.read_bytes())
                verified += 1
            except Exception as error:
                source_complete = False
                problems.append(f"{path}: final verification failed: {error}")
        unique += int(source_complete)
    print(f"PDF verification: {unique}/{len(sources)} sources complete; {verified}/{expected} tenant copies valid.")
    if problems:
        print("PROBLEMS (existing files, frozen Markdown and manifest were not replaced):", file=sys.stderr)
        for problem in dict.fromkeys(problems):
            print("  - " + problem, file=sys.stderr)
        print("Resolve the reported source/DNS/access or file mismatch, then rerun --pdf-only; valid PDFs are reused.", file=sys.stderr)
        return 1
    print(f"PASS: all {len(sources)} source PDFs and {expected} tenant copies verified; frozen Markdown and manifest unchanged.")
    return 0


def extract_pages(pdf_path: str) -> list:
    from pypdf import PdfReader     # only the fetcher needs it: pip install pypdf
    return [(p.extract_text() or "") for p in PdfReader(pdf_path).pages]


def mirror_text(src: dict, pages: list) -> str:
    head = "\n".join([
        "<!--",
        f"source: {src['source_url']}",
        f"publisher: {src['publisher']}",
        f"sha256: {src['sha256']}",
        f"retrieved: {src['retrieved']}",
        f"pages: {len(pages)}",
        "text: extracted with pypdf; a form feed (\\f) separates pages so page numbers survive chunking",
        f"licence: {LICENCE}",
        "-->",
    ])
    body = "\f".join(p.strip() + "\n" for p in pages)
    return f"{head}\n# {src['title']}\n\n{body}"


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="never download; rebuild mirrors from disk")
    ap.add_argument("--allow-drift", action="store_true",
                    help="accept a PDF whose sha256 differs from real_sources.json and print the new one")
    ap.add_argument("--pdf-only", action="store_true",
                    help="verify/reuse/download PDFs only; never rewrite frozen Markdown or the manifest")
    ap.add_argument("--git-repo", help="local source Git repository; used before publisher downloads in --pdf-only mode")
    ap.add_argument("--git-ref", help="full immutable commit ID from the runbook's source snapshot (requires --git-repo)")
    a = ap.parse_args(argv)
    if a.pdf_only and a.allow_drift:
        ap.error("--pdf-only cannot be combined with --allow-drift; manifest bytes must remain frozen")
    if bool(a.git_repo) != bool(a.git_ref):
        ap.error("--git-repo and --git-ref must be supplied together")
    if a.git_repo and not a.pdf_only:
        ap.error("--git-repo and --git-ref require --pdf-only")
    if a.git_ref and not re.fullmatch(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})", a.git_ref):
        ap.error("--git-ref must be the full resolved commit ID, not a branch name")

    sources = json.load(open(SOURCES, encoding="utf-8"))
    if a.pdf_only:
        return fetch_pdfs_only(sources, offline=a.offline, git_repo=a.git_repo, git_ref=a.git_ref)
    problems = []
    print(f"{'slug':38} {'pages':>5} {'bytes':>9}  tenants          status")
    print("-" * 92)
    for src in sources:
        first = os.path.join(CORPUS, src["tenants"][0], src["slug"] + ".pdf")
        data = None
        if os.path.isfile(first):
            data = open(first, "rb").read()
            if sha256(data) != src["sha256"]:
                problems.append(f"{src['slug']}: the PDF on disk is not the bytes real_sources.json records")
                data = None
        if data is None and not a.offline:
            try:
                data = download(src["source_url"])
            except Exception as e:                      # noqa: BLE001 - report every failure at the end
                problems.append(f"{src['slug']}: download failed: {e} <- {src['source_url']}")
                print(f"{src['slug']:38} {'-':>5} {'-':>9}  {','.join(src['tenants']):16} FAILED")
                continue
            got = sha256(data)
            if got != src["sha256"]:
                msg = (f"{src['slug']}: the source changed - sha256 {got[:12]} on the site, "
                       f"{src['sha256'][:12]} in real_sources.json")
                if not a.allow_drift:
                    problems.append(msg + " (re-run with --allow-drift after reading the new file)")
                    continue
                print("  DRIFT ACCEPTED:", msg)
                src["sha256"], src["bytes"] = got, len(data)
        if data is None:
            problems.append(f"{src['slug']}: no PDF on disk and --offline given")
            continue

        pages = extract_pages(first) if src["text_layer"] and os.path.isfile(first) else None
        for tenant in src["tenants"]:
            d = os.path.join(CORPUS, tenant)
            os.makedirs(d, exist_ok=True)
            pdf = os.path.join(d, src["slug"] + ".pdf")
            if not os.path.isfile(pdf) or sha256(open(pdf, "rb").read()) != src["sha256"]:
                with open(pdf, "wb") as f:
                    f.write(data)
            if src["text_layer"]:
                if pages is None:
                    pages = extract_pages(pdf)
                with open(os.path.join(d, src["slug"] + ".md"), "w", encoding="utf-8", newline="\n") as f:
                    f.write(mirror_text(src, pages))
        n_pages = len(pages) if pages else src["pages"]
        print(f"{src['slug']:38} {n_pages:5d} {src['bytes']:9,d}  {','.join(src['tenants']):16} "
              f"{'ok, mirror written' if src['text_layer'] else 'ok, scanned - no text layer, no mirror'}")

    if a.allow_drift:
        with open(SOURCES, "w", encoding="utf-8", newline="\n") as f:
            json.dump(sources, f, indent=1, ensure_ascii=False)
            f.write("\n")
    print()
    if problems:
        print("PROBLEMS:")
        for p in problems:
            print("  -", p)
        return 1
    print("Every real document is on disk with the bytes real_sources.json records.")
    print("Next: python deploy/evals/build_corpus.py  (rebuilds manifest.json with these entries)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
