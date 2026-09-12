#!/usr/bin/env python3
"""make-evalset: turn the corpus into eval CANDIDATES. Lesson 5.5 step 11, gap G9.

    make make-evalset PROJECT=... [TENANT=acme] [ROWS=200]
    python deploy/evals/make_evalset.py --project P --tenant acme --rows 200

Reads the chunks the quality gate ALLOWED - rag_data.index_feed joined to chunk_source, so
nothing PII-flagged and nothing outside the token band ever becomes an eval row, because an eval
row built on a chunk you would not index tests a system you are not running - and asks Gemini
for one question/answer pair per chunk (a structured call, so the pair is two fields and not a
paragraph). Every pair is then scanned with the ONE PII list (shared/pii.py) and dropped on any
finding: a generated question can restate a payslip.

The output is deploy/evals/golden_generated.jsonl, in 4.7's row shape with shape="generated" and
must_retrieve=[chunk_id]. These are CANDIDATES, not a golden set. 4.7 was explicit that a golden
row is a contract a human writes; questions generated from the corpus inherit the generator's
blind spots. Review, cut most of them, merge the survivors into golden.jsonl - run_eval.py will
not read this file, on purpose.

Tier B (live): needs the bq CLI, google-genai and google-cloud-dlp, and a project where the
feature job (`make features`) has run.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "golden_generated.jsonl")
MODEL = "gemini-3.6-flash"


def feed_rows(project: str, tenant: str, rows: int) -> list[dict]:
    sql = (f"SELECT f.chunk_id, f.doc_type, s.text FROM `{project}.rag_data.index_feed` f "
           f"JOIN `{project}.rag_data.chunk_source` s USING (chunk_id) "
           f"WHERE f.tenant_id = '{tenant}' ORDER BY f.chunk_id LIMIT {int(rows)}")
    out = subprocess.run(["bq", "--project_id", project, "query", "--nouse_legacy_sql",
                          "--format=json", "--max_rows", str(rows), sql],
                         capture_output=True, text=True, check=True)
    return json.loads(out.stdout or "[]")


def ask_pairs(project: str, chunks: list[dict]) -> list[dict]:
    """One question a colleague would ask that the passage answers, and the answer quoting it."""
    from google import genai
    from google.genai import types
    from pydantic import BaseModel, Field

    class Pair(BaseModel):
        question: str = Field(description="One natural question the passage answers")
        expected_answer: str = Field(description="The answer, quoting the passage")

    client = genai.Client(enterprise=True, project=project, location="global")  # generation is global-only
    pairs = []
    for c in chunks:
        r = client.models.generate_content(
            model=MODEL,
            contents=("Write one question a colleague would ask that this passage answers, and the "
                      "answer, quoting the passage. Do not invent facts.\n\nPassage:\n" + c["text"][:1500]),
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Pair,
                                               thinking_config=types.ThinkingConfig(thinking_level="LOW")))
        if r.parsed:
            pairs.append({**c, "question": r.parsed.question, "expected": r.parsed.expected_answer})
    return pairs


def redact(pairs: list[dict]) -> tuple[list[dict], int]:
    """Drop, never rewrite: a question that carries PII is not worth a redacted version."""
    sys.path.insert(0, os.path.dirname(HERE))          # deploy/, for shared/
    from shared.pii import inspect

    kept, dropped = [], 0
    for p in pairs:
        if inspect(p["question"]) or inspect(p["expected"]):
            dropped += 1
            continue
        kept.append(p)
    return kept, dropped


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.environ.get("DOCUMIND_PROJECT", ""))
    ap.add_argument("--tenant", default="acme")
    ap.add_argument("--rows", type=int, default=200)
    a = ap.parse_args()
    if not a.project:
        print("--project (or DOCUMIND_PROJECT) is required - this is a live tool", file=sys.stderr)
        return 2
    chunks = feed_rows(a.project, a.tenant, a.rows)
    if not chunks:
        print("index_feed is empty for this tenant - run `make features` first", file=sys.stderr)
        return 1
    pairs = ask_pairs(a.project, chunks)
    kept, dropped = redact(pairs)
    with open(OUT, "w", encoding="utf-8") as f:
        for i, p in enumerate(kept, 1):
            f.write(json.dumps({"id": f"gen-{i:03d}", "shape": "generated", "question": p["question"],
                                "tenant": a.tenant, "must_contain": [], "must_retrieve": [p["chunk_id"]],
                                "answerable": True, "expected": p["expected"], "doc_type": p.get("doc_type"),
                                "note": "CANDIDATE - review, then merge the survivors into golden.jsonl"},
                               ensure_ascii=False) + "\n")
    print(f"  {len(chunks)} feed chunks -> {len(pairs)} pairs -> {len(kept)} kept, {dropped} dropped by the PII scan")
    print(f"  wrote {OUT}  (candidates; run_eval.py reads golden.jsonl only)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
