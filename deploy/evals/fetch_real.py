#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Fetch the REAL documents in DocuMind's corpus from the government sites that publish them.

    python deploy/evals/fetch_real.py             # download what is missing, verify, extract mirrors
    python deploy/evals/fetch_real.py --offline   # no network: rebuild the .md mirrors from the PDFs on disk
    python deploy/evals/fetch_real.py --allow-drift   # a source changed its bytes: accept and print the new sha

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
import hashlib
import json
import os
import shutil
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
CORPUS = os.path.join(HERE, "corpus")
SOURCES = os.path.join(HERE, "real_sources.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/128.0 Safari/537.36")      # the ministry's CDN answers a plain urllib agent with 403
LICENCE = ("Act of Parliament. Reproducing an Act is not an infringement of copyright "
           "(Copyright Act 1957, s. 52(1)(q)). No personal data.")


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def download(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=180) as r:
        data = r.read()
    if not data.startswith(b"%PDF"):
        raise RuntimeError(f"not a PDF ({data[:40]!r})")
    return data


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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--offline", action="store_true", help="never download; rebuild mirrors from disk")
    ap.add_argument("--allow-drift", action="store_true",
                    help="accept a PDF whose sha256 differs from real_sources.json and print the new one")
    a = ap.parse_args()

    sources = json.load(open(SOURCES, encoding="utf-8"))
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
