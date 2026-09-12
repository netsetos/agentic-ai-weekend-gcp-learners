"""Price and score the backends on the same questions. Lessons 11.4 / 11.5, gap G10.

The comparison only means something if the questions and the corpus are identical - which is
what deploy/evals/golden.jsonl is for. Anything else compares two prompts.

Three modes:

    python compare_backends.py --rows 20 > compare.csv      # LIVE: one row per backend x question
    python compare_backends.py --summary compare.csv        # the four numbers 10.6 and 13.3 quote
    python compare_backends.py --selftest                   # the summary maths on a fixture, offline

The live pass retrieves ONCE per question through the one retrieve() (shared/documind_tools.py,
so the context is exactly what every brain sees) and then asks EACH backend to answer from that
context through the gateway (LITELLM_URL, model=<backend>) - so the only variable between rows
is the model. The answer comes back in ModelDraft's shape (answer, [Source N] citations,
answerable) and is resolved against the retrieved chunks the same way rag-api does it.

The summary, per backend:
    groundedness        answered AND every must_contain string present, over answerable rows
    citation precision  cited chunks that are in must_retrieve, over all cited chunks
    p95 latency (ms)    the number a dashboard alerts on, not the mean
    Rs per 1k queries   mean cost x 1000 - the number a budget review argues about

Cost per token for documind-slm is a RATE, not a price (see PRICES): it only holds at the volume
it was derived from. That is the point 11.4 makes, and the summary makes it visible.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import statistics
import sys
import time

USD_INR = 85                      # course standard

# USD per 1M tokens. Gemini rates are the standard ones (intro 0.75/3.75 runs to
# 2026-12-31); the self-hosted figure is derived, not quoted - see below.
PRICES = {
    "documind-general": (1.50, 7.50),           # gemini-3.6-flash, standard
    "gemini-3.6-flash": (1.50, 7.50),
    "gemini-3.1-flash-lite": (0.25, 1.50),
    # An L4 on Cloud Run is $0.672/hr for the GPU LINE ITEM. That is not what the
    # service costs. 11.1's table adds 8 vCPU ($0.518) and 32 GiB ($0.230) for a
    # RECOMMENDED TOTAL INSTANCE of ~$1.42/hr = ~$1,023/mo = Rs 86,904/mo - and
    # 11.4 deploys exactly that config.
    #
    # (An earlier edit of this file replaced $1.42 with a $0.672-derived rate,
    # believing $1.42 was invented. It was not: it is 11.1's own bottom line.
    # The GPU is less than half the bill, which is the part people forget.)
    #
    # At Rs 86,904/month and ~50M tokens/month that is ~Rs 0.0017 per token,
    # either direction. A RATE, not a price: idle time is the variable.
    "documind-slm": (20.5, 20.5),
    # 11.5's Autopilot pool: the same L4 class, shared by a batch lane, so the
    # per-token rate is the same order and the same caveat.
    "documind-gke": (20.5, 20.5),
}
COLUMNS = ["backend", "row_id", "shape", "answerable", "latency_ms", "tokens_in", "tokens_out",
           "cost_inr", "answered", "grounded", "cited", "must_retrieve"]


def cost_inr(model: str, tok_in: int, tok_out: int) -> float:
    pin, pout = PRICES.get(model, PRICES["documind-general"])
    return (tok_in * pin + tok_out * pout) / 1_000_000 * USD_INR


# ----------------------------------------------------------------------------- the live pass
SYSTEM = ("You are DocuMind. Answer ONLY from the numbered context. Cite by [Source N] and quote "
          "the words you relied on. Reply as JSON: {\"answer\": str, \"citations\": "
          "[{\"source\": int, \"quote\": str}], \"answerable\": bool}.")


def ask(backend: str, question: str, chunks: list[dict]) -> tuple[dict, int, int]:
    """One answer from one backend through the gateway, in ModelDraft's shape."""
    import requests

    url = os.environ["LITELLM_URL"].rstrip("/") + "/v1/chat/completions"
    # On the lean lane the gateway is behind Cloud Run IAM and the door is an ID token for its URL (make compare mints
    # one as the roster member); the master key is the full profile's. Either rides in the same header.
    key = os.environ.get("LITELLM_ID_TOKEN") or os.environ.get("LITELLM_MASTER_KEY", "")
    context = "\n".join(f"[Source {i}] {c.get('quote') or c.get('text', '')}" for i, c in enumerate(chunks, 1))
    r = requests.post(url, headers={"Authorization": f"Bearer {key}"}, timeout=120, json={
        "model": backend, "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": SYSTEM},
                     {"role": "user", "content": f"Context:\n{context}\n\nQuestion: {question}"}]})
    r.raise_for_status()
    body = r.json()
    usage = body.get("usage", {})
    try:
        draft = json.loads(body["choices"][0]["message"]["content"])
    except (KeyError, ValueError):
        draft = {"answer": "", "citations": [], "answerable": False}
    return draft, int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))


def cited_ids(draft: dict, chunks: list[dict]) -> list[str]:
    """[Source N] -> chunk id, against the chunks the model SAW (the same rule as resolve())."""
    out = []
    for c in draft.get("citations", []) or []:
        n = c.get("source") if isinstance(c, dict) else None
        if isinstance(n, int) and 1 <= n <= len(chunks):
            out.append(str(chunks[n - 1].get("chunk_id") or chunks[n - 1].get("id") or ""))
    return out


def live(args) -> int:
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))  # deploy/
    from shared import documind_tools

    rows = [json.loads(x) for x in open(args.golden, encoding="utf-8") if x.strip()][:args.rows]
    out = csv.writer(sys.stdout)
    out.writerow(COLUMNS)
    for r in rows:
        # Retrieve ONCE, through the one retrieve(): every backend answers from the same context.
        got = documind_tools.retrieve(r["question"], tenant_id=r["tenant"], top_k=6, brain="direct")
        chunks = got.get("citations", [])
        for backend in args.backends.split(","):
            t0 = time.time()
            try:
                draft, tok_in, tok_out = ask(backend, r["question"], chunks)
            except Exception as exc:                      # noqa: BLE001 - a row, not the run
                print(f"  {backend} {r['id']}: {type(exc).__name__}", file=sys.stderr)
                draft, tok_in, tok_out = {"answer": "", "citations": [], "answerable": False}, 0, 0
            ms = int((time.time() - t0) * 1000)
            answer = (draft.get("answer") or "").lower()
            answered = bool(draft.get("answerable")) and bool(answer)
            grounded = answered and all(m.lower() in answer for m in r.get("must_contain", []))
            out.writerow([backend, r["id"], r["shape"], r.get("answerable", True), ms, tok_in, tok_out,
                          f"{cost_inr(backend, tok_in, tok_out):.4f}", int(answered), int(grounded),
                          ";".join(cited_ids(draft, chunks)), ";".join(r.get("must_retrieve", []))])
    return 0


# ----------------------------------------------------------------------------- the summary
def _hit(cited: str, must: list[str]) -> bool:
    """A golden must_retrieve names a document or a clause (hr_policy_2026, EXP-12); a cited id
    is a chunk (hr_policy_2026#12). Match on containment, either way round."""
    c = cited.lower()
    return any(m and (m.lower() in c or c in m.lower()) for m in must)


def summarise(rows: list[dict]) -> dict[str, dict]:
    """Per backend: the four numbers. Pure, so --selftest can prove the maths."""
    by = {}
    for r in rows:
        by.setdefault(r["backend"], []).append(r)
    out = {}
    for backend, rs in by.items():
        answerable = [r for r in rs if str(r.get("answerable", "True")).lower() in ("true", "1")]
        grounded = [int(r["grounded"]) for r in answerable]
        cited_total = cited_hit = 0
        for r in rs:
            cited = [c for c in (r.get("cited") or "").split(";") if c]
            must = [m for m in (r.get("must_retrieve") or "").split(";") if m]
            cited_total += len(cited)
            cited_hit += sum(1 for c in cited if _hit(c, must))
        lat = sorted(int(r["latency_ms"]) for r in rs)
        p95 = lat[max(0, int(round(0.95 * len(lat))) - 1)] if lat else 0
        out[backend] = {
            "rows": len(rs),
            "groundedness": round(sum(grounded) / len(grounded), 3) if grounded else 0.0,
            "citation_precision": round(cited_hit / cited_total, 3) if cited_total else 0.0,
            "p95_ms": p95,
            "inr_per_1k": round(statistics.mean(float(r["cost_inr"]) for r in rs) * 1000, 2) if rs else 0.0,
        }
    return out


def print_summary(summary: dict[str, dict]) -> None:
    print(f"{'backend':18} {'rows':>4} {'groundedness':>13} {'cite-precision':>15} {'p95 ms':>8} {'Rs/1k queries':>14}")
    print("-" * 78)
    for b, s in summary.items():
        print(f"{b:18} {s['rows']:>4} {s['groundedness']:>13.3f} {s['citation_precision']:>15.3f} "
              f"{s['p95_ms']:>8} {s['inr_per_1k']:>14.2f}")


def selftest() -> int:
    """The summary maths, on a fixture that can go red."""
    fixture = "\n".join([
        ",".join(COLUMNS),
        "documind-general,lk-01,lookup,True,800,1200,80,0.0510,1,1,hr_policy_2026#12;acme_msa#3,EXP-12;hr_policy_2026",
        "documind-general,lk-02,lookup,True,1200,1100,60,0.0450,1,0,hr_policy_2026#31,PR-05;hr_policy_2026",
        "documind-general,rf-01,refusal,False,600,900,20,0.0300,0,0,,",
        "documind-slm,lk-01,lookup,True,2400,1200,80,2.2310,1,1,hr_policy_2026#12,EXP-12;hr_policy_2026",
        "documind-slm,lk-02,lookup,True,3100,1100,60,2.0230,1,1,acme_msa#3,PR-05;hr_policy_2026",
        "documind-slm,rf-01,refusal,False,900,900,20,1.6000,0,0,,",
    ])
    s = summarise(list(csv.DictReader(io.StringIO(fixture))))
    g, m = s["documind-general"], s["documind-slm"]
    assert g["groundedness"] == 0.5 and m["groundedness"] == 1.0, (g, m)          # refusal rows excluded
    assert g["citation_precision"] == round(2 / 3, 3), g                           # acme_msa#3 is not in must_retrieve
    assert m["citation_precision"] == 0.5, m
    assert g["p95_ms"] == 1200 and m["p95_ms"] == 3100, (g, m)
    assert g["inr_per_1k"] == 42.0 and m["inr_per_1k"] == round(statistics.mean([2.231, 2.023, 1.6]) * 1000, 2)
    assert abs(cost_inr("documind-slm", 1000, 1000) - (1000 * 20.5 + 1000 * 20.5) / 1e6 * 85) < 1e-9
    print_summary(s)
    print("\nselftest OK - groundedness excludes refusal rows, precision is per cited chunk, p95 is the 95th latency, Rs/1k is mean cost x 1000")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--golden", default=os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..", "evals", "golden.jsonl"))
    ap.add_argument("--rows", type=int, default=30)
    ap.add_argument("--backends", default="documind-general,documind-slm")
    ap.add_argument("--summary", metavar="CSV", help="summarise a CSV the live pass wrote ('-' for stdin)")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()

    if args.selftest:
        return selftest()
    if args.summary:
        f = sys.stdin if args.summary == "-" else open(args.summary, encoding="utf-8")
        print_summary(summarise(list(csv.DictReader(f))))
        return 0
    if not os.path.exists(args.golden):
        print(f"golden set not found: {args.golden}", file=sys.stderr)
        return 2
    if not os.environ.get("LITELLM_URL"):
        print("LITELLM_URL is not set - the live pass goes through the gateway (11.3)", file=sys.stderr)
        return 2
    return live(args)


if __name__ == "__main__":
    raise SystemExit(main())
