#!/usr/bin/env python3
"""The answer cache's threshold, measured. 12 September 2026 (the RAG plan, W4; the execution plan, P5.4).

    python deploy/evals/cache_threshold.py --project P            # embeds the pairs, prints the curve (~1 minute, regional)
    python deploy/evals/cache_threshold.py --selftest             # offline: the maths and the pairs' consistency

semantic_cache.py serves an earlier answer when a new question's embedding is within THRESHOLD (cosine similarity,
0.95 today) of an earlier one. The number was chosen, not measured. This measures it: paraphrases.jsonl holds
pairs against golden rows - `same: true` is the same fact in other words (a hit is right), `same: false` is a
question a few words away with a DIFFERENT answer (a hit is a wrong answer served fast: E3 -> E2, minimum ->
maximum, after -> before, gratuity -> pension). Both sides are embedded the way the API embeds queries
(text-embedding-005, RETRIEVAL_QUERY, 768-d, regional), and for each candidate threshold the tool prints the hit
rate on the true pairs and the FALSE-HIT rate on the false pairs. The switch (SEMANTIC_CACHE=on) waits for a
threshold whose false-hit rate is 0 on this set; the plan says so, and this is the evidence.

The pairs are labelled by hand and checked offline: every `of` names a golden row, every true pair's row is
answerable, every false pair names a fact the golden row's must_contain does not answer.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PAIRS = os.path.join(HERE, "paraphrases.jsonl")
GOLDEN = os.path.join(HERE, "golden.jsonl")
CANDIDATES = (0.85, 0.88, 0.90, 0.92, 0.94, 0.95, 0.96, 0.97, 0.98)


def load_pairs() -> list[dict]:
    with open(PAIRS, encoding="utf-8") as f:
        return [json.loads(l) for l in f if l.strip()]


def load_golden() -> dict[str, dict]:
    with open(GOLDEN, encoding="utf-8") as f:
        return {r["id"]: r for r in (json.loads(l) for l in f if l.strip())}


def check_pairs(pairs: list[dict], golden: dict[str, dict]) -> list[str]:
    """Offline: the pairs can judge a threshold only if they are what they say they are."""
    bad = []
    for p in pairs:
        g = golden.get(p["of"])
        if not g:
            bad.append(f"{p['id']}: golden row {p['of']} does not exist"); continue
        if p["same"] and not g["answerable"]:
            bad.append(f"{p['id']}: a true pair against an unanswerable row cannot be a right hit")
        if p["question"].strip().lower() == g["question"].strip().lower():
            bad.append(f"{p['id']}: identical to its golden question - the exact rung would serve it; a pair must differ")
        if p.get("tenant") != g["tenant"]:
            bad.append(f"{p['id']}: tenant {p.get('tenant')} is not the golden row's {g['tenant']}")
    same, diff = sum(p["same"] for p in pairs), sum(not p["same"] for p in pairs)
    if same < 15 or diff < 10:
        bad.append(f"too few pairs to measure anything: {same} same, {diff} different (want >= 15 and >= 10)")
    return bad


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def curve(sims: list[tuple[bool, float]]) -> list[dict]:
    """For each candidate threshold: hits on the true pairs, false hits on the false pairs."""
    out = []
    for t in CANDIDATES:
        true = [s for same, s in sims if same]
        false = [s for same, s in sims if not same]
        out.append({"threshold": t,
                    "hit_rate": sum(s >= t for s in true) / len(true) if true else 0.0,
                    "false_hit_rate": sum(s >= t for s in false) / len(false) if false else 0.0,
                    "hits": sum(s >= t for s in true), "false_hits": sum(s >= t for s in false)})
    return out


def recommend(rows: list[dict]) -> float | None:
    """The lowest threshold with no false hit, or None when every candidate serves a wrong answer."""
    safe = [r["threshold"] for r in rows if r["false_hits"] == 0]
    return min(safe) if safe else None


def embed_all(project: str, region: str, texts: list[str]) -> list[list[float]]:
    from google import genai
    from google.genai import types
    client = genai.Client(enterprise=True, project=project, location=region)
    out = []
    for i in range(0, len(texts), 32):
        resp = client.models.embed_content(model="text-embedding-005", contents=texts[i:i + 32],
                                           config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY", output_dimensionality=768))
        out += [e.values for e in resp.embeddings]
    return out


def selftest() -> int:
    pairs, golden = load_pairs(), load_golden()
    bad = check_pairs(pairs, golden)
    assert not bad, "\n".join(bad)
    sims = [(True, 0.99), (True, 0.96), (True, 0.93), (False, 0.94), (False, 0.90), (False, 0.80)]
    rows = curve(sims)
    at = {r["threshold"]: r for r in rows}
    assert at[0.95]["false_hits"] == 0 and at[0.95]["hits"] == 2 and at[0.92]["false_hits"] == 1 and at[0.90]["false_hits"] == 2
    assert recommend(rows) == 0.95 and recommend(curve([(False, 0.99)])) is None
    assert abs(cosine([1, 0], [1, 0]) - 1) < 1e-9 and abs(cosine([1, 0], [0, 1])) < 1e-9
    print(f"selftest OK - {len(pairs)} pairs ({sum(p['same'] for p in pairs)} same, {sum(not p['same'] for p in pairs)} different) "
          "name real golden rows and differ from them; the curve counts hits and false hits per threshold; the recommendation "
          "is the lowest threshold with no false hit")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.environ.get("PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT"))
    ap.add_argument("--region", default="us-central1", help="where text-embedding-005 is served (regional)")
    ap.add_argument("--report", help="write the similarities and the curve to this JSON file")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.project:
        print("--project is required (or --selftest)", file=sys.stderr)
        return 2
    pairs, golden = load_pairs(), load_golden()
    bad = check_pairs(pairs, golden)
    if bad:
        print("\n".join("  " + b for b in bad)); return 1
    originals = sorted({p["of"] for p in pairs})
    vec = dict(zip(originals, embed_all(a.project, a.region, [golden[i]["question"] for i in originals])))
    pvec = embed_all(a.project, a.region, [p["question"] for p in pairs])
    sims = [(p["same"], cosine(vec[p["of"]], v)) for p, v in zip(pairs, pvec)]
    rows = curve(sims)
    print(f"  {len(pairs)} pairs against {len(originals)} golden questions, text-embedding-005 @ {a.region}\n")
    print("  threshold   hit rate (same)   false-hit rate (different)")
    for r in rows:
        print(f"  {r['threshold']:9.2f}   {r['hit_rate']:7.0%} ({r['hits']:2})      {r['false_hit_rate']:7.0%} ({r['false_hits']:2})")
    rec = recommend(rows)
    print(f"\n  lowest threshold with no false hit: {rec if rec is not None else 'none - every candidate serves a wrong answer on this set'}")
    print("  nearest false pairs:", ", ".join(f"{p['id']} {s:.3f}" for (p, (_, s)) in sorted(zip(pairs, sims), key=lambda x: -x[1][1]) if not p["same"])[:200])
    if a.report:
        with open(a.report, "w", encoding="utf-8") as f:
            json.dump({"pairs": [{**p, "similarity": s} for p, (_, s) in zip(pairs, sims)], "curve": rows, "recommended": rec}, f, indent=1)
        print(f"  report: {a.report}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
