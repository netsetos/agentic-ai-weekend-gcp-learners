#!/usr/bin/env python3
"""Per-tenant rupees from the usage rows, on the lean lane. Lesson 12.3 (10 September 2026).

    python evals/usage_rows.py --project P --hours 24            # or: make usage PROJECT=... HOURS=24
    python evals/usage_rows.py --selftest

The full profile answers "who spent what" with tenant_daily, a BigQuery view over the log sink. The lean lane has no
sink and no dataset - but it has the same rows, because the API logs ONE shape on every answer (12.2's usage_row)
and Cloud Logging keeps them for thirty days. This reads the last N hours of those rows through gcloud and groups
them the way the view does: by tenant, by model and backend, by brain. Same GROUP BY, different store; 10.3's
attribution cell, as a tool. Rates are the API's own (cost_usd is priced at the model that answered, or by the
gateway's header); the rupee column is USD_INR=85, the course's standard. The last table is where the time went:
the p95 of each stage (retrieve, rerank, generate - the row's own clocks, 12.2) beside the p95 of the whole, and the
pool the reranker saw - the view's p95_retrieve_ms / p95_rerank_ms / p95_generate_ms / avg_pool, on the lane.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import subprocess
import sys
from collections import defaultdict

USD_INR = 85
STAGES = ("retrieve_ms", "rerank_ms", "generate_ms")


def read_rows(project: str, hours: int, service: str = "documind-api", limit: int = 2000) -> list[dict]:
    since = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%SZ")
    flt = (f'resource.type="cloud_run_revision" AND resource.labels.service_name="{service}" '
           f'AND (jsonPayload.event="query" OR jsonPayload.event="stream" OR jsonPayload.event="media") AND timestamp>="{since}"')
    r = subprocess.run(["gcloud", "logging", "read", flt, "--project", project, "--limit", str(limit), "--format=json"],
                       capture_output=True, text=True)
    if r.returncode != 0:
        print(r.stderr.strip()[:400], file=sys.stderr)
        return []
    return [e["jsonPayload"] for e in json.loads(r.stdout or "[]") if "jsonPayload" in e]


def p95(values: list[int]) -> int:
    if not values:
        return 0
    s = sorted(values)
    return s[max(0, int(round(0.95 * len(s))) - 1)]


def group(rows: list[dict], keys: tuple[str, ...]) -> list[dict]:
    """The view's GROUP BY over the rows: answers, tokens, cost in both currencies, p95 latency - of the whole and of
    each stage - the pool the reranker saw, unanswerable count."""
    acc: dict[tuple, dict] = defaultdict(lambda: {"answers": 0, "tokens_in": 0, "tokens_out": 0, "usd": 0.0, "lat": [], "unanswerable": 0,
                                                  "stages": {s: [] for s in STAGES}, "pool": []})
    for r in rows:
        k = tuple(str(r.get(key, "")) for key in keys)
        a = acc[k]
        a["answers"] += 1
        a["tokens_in"] += int(r.get("tokens_in") or 0)
        a["tokens_out"] += int(r.get("tokens_out") or 0)
        a["usd"] += float(r.get("cost_usd") or 0.0)
        a["lat"].append(int(r.get("latency_ms") or 0))
        a["unanswerable"] += int(r.get("unanswerable_flag") or 0)
        for s in STAGES:                      # a row from before the stage clocks carries no field: absent, not 0
            if r.get(s) is not None:
                a["stages"][s].append(int(r[s]))
        if r.get("pool") is not None:
            a["pool"].append(int(r["pool"]))
    out = []
    for k, a in sorted(acc.items(), key=lambda kv: -kv[1]["usd"]):
        out.append({**dict(zip(keys, k)), "answers": a["answers"], "tokens_in": a["tokens_in"], "tokens_out": a["tokens_out"],
                    "usd": round(a["usd"], 4), "inr": round(a["usd"] * USD_INR, 2), "p95_ms": p95(a["lat"]),
                    **{f"p95_{s}": p95(v) for s, v in a["stages"].items()},
                    "avg_pool": round(sum(a["pool"]) / len(a["pool"]), 1) if a["pool"] else 0.0,
                    "unanswerable_rate": round(a["unanswerable"] / a["answers"], 3) if a["answers"] else 0.0})
    return out


def show(title: str, table: list[dict], keys: tuple[str, ...]) -> None:
    print(f"\n{title}")
    head = "".join(f"{k:22}" for k in keys) + f"{'answers':>8} {'tok_in':>9} {'tok_out':>8} {'USD':>9} {'INR':>9} {'p95 ms':>7} {'unans':>6}"
    print(head)
    print("-" * len(head))
    for row in table:
        print("".join(f"{str(row[k])[:20]:22}" for k in keys)
              + f"{row['answers']:>8} {row['tokens_in']:>9} {row['tokens_out']:>8} {row['usd']:>9.4f} {row['inr']:>9.2f} {row['p95_ms']:>7} {row['unanswerable_rate']:>6.2f}")


def show_stages(title: str, table: list[dict], keys: tuple[str, ...]) -> None:
    """Where the time went: each stage's p95 beside the p95 of the whole, and the pool the reranker saw. A p95 that
    moved names the stage that moved it - the same columns as the view's p95_retrieve_ms / p95_rerank_ms /
    p95_generate_ms / avg_pool. Rows from before the stage clocks count in `answers` and in nothing else here."""
    print(f"\n{title}")
    head = "".join(f"{k:22}" for k in keys) + f"{'answers':>8} {'p95 ms':>7} {'retrieve':>9} {'rerank':>7} {'generate':>9} {'pool':>6}"
    print(head)
    print("-" * len(head))
    for row in table:
        print("".join(f"{str(row[k])[:20]:22}" for k in keys)
              + f"{row['answers']:>8} {row['p95_ms']:>7} {row['p95_retrieve_ms']:>9} {row['p95_rerank_ms']:>7} {row['p95_generate_ms']:>9} {row['avg_pool']:>6}")


def selftest() -> int:
    rows = [
        {"tenant": "acme", "model": "gemini-3.6-flash", "model_backend": "vertex", "brain": "ui", "tokens_in": 2000, "tokens_out": 250, "cost_usd": 0.004875, "latency_ms": 900, "unanswerable_flag": 0,
         "retrieve_ms": 180, "rerank_ms": 110, "generate_ms": 560, "pool": 20},
        {"tenant": "acme", "model": "gemini-3.6-flash", "model_backend": "vertex", "brain": "langchain", "tokens_in": 1800, "tokens_out": 200, "cost_usd": 0.0042, "latency_ms": 1400, "unanswerable_flag": 1,
         "retrieve_ms": 220, "rerank_ms": 130, "generate_ms": 990, "pool": 20},
        {"tenant": "zeta", "model": "documind-slm", "model_backend": "gateway", "brain": "ui", "tokens_in": 1500, "tokens_out": 120, "cost_usd": 0.03321, "latency_ms": 5200, "unanswerable_flag": 0,
         "retrieve_ms": 200, "rerank_ms": 120, "generate_ms": 4800, "pool": 20},
    ]
    by_tenant = group(rows, ("tenant",))
    assert by_tenant[0]["tenant"] == "zeta" and by_tenant[0]["inr"] == round(0.03321 * 85, 2), by_tenant   # dearest first
    acme = next(r for r in by_tenant if r["tenant"] == "acme")
    assert acme["answers"] == 2 and acme["tokens_in"] == 3800 and acme["unanswerable_rate"] == 0.5 and acme["p95_ms"] == 1400, acme
    by_model = group(rows, ("model", "model_backend"))
    assert {(r["model"], r["model_backend"]) for r in by_model} == {("gemini-3.6-flash", "vertex"), ("documind-slm", "gateway")}
    assert p95([]) == 0 and p95([5, 1, 3]) == 5
    # Where the time went: the stage p95s are their own columns, never above the whole; the pool is an average;
    # a row from before the stage clocks (no fields) is counted as an answer and nowhere else.
    assert acme["p95_retrieve_ms"] == 220 and acme["p95_rerank_ms"] == 130 and acme["p95_generate_ms"] == 990 and acme["avg_pool"] == 20.0, acme
    assert all(r[f"p95_{s}"] <= r["p95_ms"] for r in by_tenant for s in STAGES)
    old = group([{"tenant": "old", "latency_ms": 700}], ("tenant",))[0]
    assert old["answers"] == 1 and old["p95_ms"] == 700 and old["p95_generate_ms"] == 0 and old["avg_pool"] == 0.0, old
    show("selftest: by tenant", by_tenant, ("tenant",))
    show_stages("selftest: where the time went (p95 per stage, by tenant)", by_tenant, ("tenant",))
    print("\nselftest OK - grouped like tenant_daily: dearest tenant first, tokens summed, p95 the 95th latency, unanswerable a rate, "
          "p95 per stage and the pool beside it")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project")
    ap.add_argument("--hours", type=int, default=24)
    ap.add_argument("--service", default="documind-api")
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args()
    if args.selftest:
        return selftest()
    if not args.project:
        print("--project is required (or --selftest)", file=sys.stderr)
        return 2
    rows = read_rows(args.project, args.hours, args.service)
    if not rows:
        print(f"no usage rows from {args.service} in the last {args.hours} h - ask a question first (make smoke does)")
        return 1
    print(f"{len(rows)} answers from {args.service} in the last {args.hours} h; USD_INR={USD_INR}")
    show("by tenant", group(rows, ("tenant",)), ("tenant",))
    show("by model and backend (what answered, through which door)", group(rows, ("model", "model_backend")), ("model", "model_backend"))
    show("by brain (8.7's question, from the row)", group(rows, ("brain",)), ("brain",))
    show("by surface", group(rows, ("event",)), ("event",))
    show_stages("where the time went (p95 per stage, by tenant)", group(rows, ("tenant",)), ("tenant",))
    return 0


if __name__ == "__main__":
    sys.exit(main())
