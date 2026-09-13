#!/usr/bin/env python3
"""The ablation harness: retrieval only, no model in the loop, against the frozen golden set.

    python deploy/evals/ablate.py --project PROJECT              # the 43 anchored rows, four arms (~3 min)
    python deploy/evals/ablate.py --project PROJECT --limit 5    # a wiring check, under a minute
    python deploy/evals/ablate.py --project PROJECT --arms all   # the four and the managed arm (P9.6: needs the mirror)
    make ablate PROJECT=...                                      # the same, from deploy/ (ABLATE_ARGS="--arms all")

One knob per arm, everything else held (lesson 4.8, Part 5):

    dense 5, no reranker          Firestore find_nearest with the tenant pre-filter, top 5 as returned
    dense 20 -> rerank 5          the lane's own path on this profile (retriever.py -> Rank API)
    dense 50 -> rerank 5          the same reranker over a deeper candidate list
    hybrid 20 -> rerank 5         4.5's RRF over the dense ids and a BM25 leg (alpha 0.7) - which the
                                  lean profile does NOT wire: hybrid.py rides Vector Search only
    rag_engine 20 -> rerank 5     P9.4's backend from outside it (13 September 2026): 4.3's corpus - the
                                  mirror's, one RagFile per version named by its doc_key - queried by text,
                                  each context's doc_key resolved to its source through the kit's own rows.
                                  Behind --arms all (or --arms rag_engine): a lane without the mirror has
                                  no corpus, and an arm that cannot run is a finding, not the harness's exit

Scored on the golden rows that carry `must_retrieve` anchors, by recall and MRR against source
file + text (the way run_eval.py matches: the worker's chunk ids name neither the clause nor
the document). Next to every reranked number is the recall at the candidate depth, because a
reranker can only reorder what the retriever returned - if recall@20 did not move, no reranker
setting will. Run 5 of the first live loop was exactly that case.

Under each arm's line is its miss list - the rows that cost a point and where the anchor went:
"ranked out" (in the candidates, not the five: the reranker's) or "not retrieved at depth N" (the
retriever's, or the corpus's). A row that fails twice is counted and printed, never scored.

Cost: about four query embeddings and three Rank API requests per row, and the Firestore reads
of each tenant's text (no embeddings are fetched). Not one generation call. Exit code 0 is a
measurement, not a verdict; it is 1 only when an arm could not run a single row.

Measured on 8 Sept 2026 from Cloud Shell, 43 rows: dense 5 alone 0.91 recall@5 / 0.81 MRR; the
lane's dense 20 -> rerank 5 0.96 / 0.91; dense 50 -> rerank 5 0.99 / 0.94 at four times the
latency; hybrid 20 -> rerank 5 identical to the lane's row at alpha 0.5 and (ACME's 36 rows) 0.7.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import sys
import time

ARM_KEYS = ("dense5", "dense20", "dense50", "hybrid", "rag_engine")
DEFAULT_ARMS = ("dense5", "dense20", "dense50", "hybrid")

HERE = os.path.dirname(os.path.abspath(__file__))
GOLDEN = os.path.join(HERE, "golden.jsonl")


# ------------------------------------------------------------------ pure parts (testable offline)
def tokens(text: str) -> list:
    return re.findall(r"[a-z0-9\-]+", text.lower())


def rrf(dense_ids: list, sparse_ids: list, alpha: float = 0.7, k: int = 60) -> list:
    """Reciprocal Rank Fusion, hybrid.py's semantics: alpha=1 dense only, 0 sparse only."""
    fused = {}
    for rank, cid in enumerate(dense_ids):
        fused[cid] = fused.get(cid, 0.0) + alpha / (k + rank + 1)
    for rank, cid in enumerate(sparse_ids):
        fused[cid] = fused.get(cid, 0.0) + (1 - alpha) / (k + rank + 1)
    return [cid for cid, _ in sorted(fused.items(), key=lambda kv: -kv[1])]


def hay(chunk: dict) -> str:
    return (chunk.get("source_uri", "") + " " + chunk.get("text", "")).lower()


def recall_at(chunks: list, anchors: list, k: int) -> float:
    found = [hay(c) for c in chunks[:k]]
    return sum(1 for a in anchors if any(a.lower() in h for h in found)) / len(anchors)


def mrr(chunks: list, anchors: list) -> float:
    for i, c in enumerate(chunks, 1):
        if any(a.lower() in hay(c) for a in anchors):
            return 1.0 / i
    return 0.0


def p95(values: list) -> float:
    return sorted(values)[int(0.95 * (len(values) - 1))] if values else 0.0


# ------------------------------------------------------------------ the lane, from outside it
class Lane:
    """The three services the API's retriever calls, with the same settings, from outside it."""

    def __init__(self, project: str, region: str, alpha: float = 0.7, current_only: bool = True):
        from google import genai
        from google.cloud import discoveryengine_v1 as discoveryengine
        from google.cloud import firestore
        self.project, self.alpha = project, alpha
        # 12 September 2026: the lane retrieves only rows the ledger marks current (RETRIEVAL_CURRENT_ONLY=on,
        # retriever.py) - a retired version's chunk is not a hit there, so it is not a hit here either. The
        # review of the rag_prod plan found this harness counting retired chunks the API would never serve.
        self.current_only = current_only
        self.embed_client = genai.Client(enterprise=True, project=project, location=region)   # embeddings: regional
        self.db = firestore.Client(project=project, database="(default)")
        self.ranker = discoveryengine.RankServiceClient()
        self.ranking_config = self.ranker.ranking_config_path(
            project=project, location="global", ranking_config="default_ranking_config")
        self._tenant = {}
        self._rag_module, self._corpora, self._sources = None, {}, {}

    def embed_query(self, q: str) -> list:
        from google.genai import types
        return self.embed_client.models.embed_content(
            model="text-embedding-005", contents=q,
            config=types.EmbedContentConfig(task_type="RETRIEVAL_QUERY", output_dimensionality=768),
        ).embeddings[0].values

    def tenant_corpus(self, tenant: str):
        """One tenant's text chunks (no embeddings: 768 floats each) and a BM25 index - the sparse leg."""
        if tenant not in self._tenant:
            from google.cloud.firestore_v1.base_query import FieldFilter
            from rank_bm25 import BM25Okapi
            rows = []
            for d in (self._current(self.db.collection("chunks").where(filter=FieldFilter("tenant_id", "==", tenant)))
                      .select(["text", "source_uri"]).stream()):
                x = d.to_dict()
                rows.append({"chunk_id": d.id, "text": x.get("text", ""), "source_uri": x.get("source_uri", "")})
            self._tenant[tenant] = (rows, {r["chunk_id"]: r for r in rows}, BM25Okapi([tokens(r["text"]) for r in rows]))
        return self._tenant[tenant]

    def _current(self, query):
        """The ledger's pre-filter, when the lane applies it: the tenant_id + current + embedding index serves it."""
        from google.cloud.firestore_v1.base_query import FieldFilter
        return query.where(filter=FieldFilter("current", "==", True)) if self.current_only else query

    def dense(self, question: str, tenant: str, k: int) -> list:
        """The lane's retriever on the lean profile: find_nearest with the tenant pre-filter and the ledger's."""
        from google.cloud.firestore_v1.base_query import FieldFilter
        from google.cloud.firestore_v1.base_vector_query import DistanceMeasure
        from google.cloud.firestore_v1.vector import Vector
        docs = (self._current(self.db.collection("chunks").where(filter=FieldFilter("tenant_id", "==", tenant)))
                .find_nearest(vector_field="embedding", query_vector=Vector(self.embed_query(question)),
                              distance_measure=DistanceMeasure.COSINE, limit=k, distance_result_field="d")
                .get())
        out = []
        for d in docs:
            x = d.to_dict()
            out.append({"chunk_id": d.id, "text": x.get("text", ""), "source_uri": x.get("source_uri", "")})
        return out

    def hybrid(self, question: str, tenant: str, k: int) -> list:
        rows, by_id, bm25 = self.tenant_corpus(tenant)
        dense_ids = [c["chunk_id"] for c in self.dense(question, tenant, k)]
        scores = bm25.get_scores(tokens(question))
        sparse_ids = [rows[i]["chunk_id"] for i in sorted(range(len(rows)), key=lambda i: -scores[i])[:k] if scores[i] > 0]
        return [by_id[cid] for cid in rrf(dense_ids, sparse_ids, self.alpha)[:k] if cid in by_id]

    def rag(self, location: str = "us-central1"):
        """vertexai.rag on the corpora's region (serverless corpora: us-central1 only), initialised once."""
        if self._rag_module is None:
            import vertexai
            from vertexai import rag
            vertexai.init(project=self.project, location=os.environ.get("RAG_LOCATION", location))
            self._rag_module = rag
        return self._rag_module

    def corpus(self, tenant: str) -> str:
        """The tenant's corpus by the mirror's name (documind-{tenant}); a tenant without one fails the row."""
        if tenant not in self._corpora:
            want = "documind-" + re.sub(r"[^a-z0-9-]+", "-", tenant.lower()).strip("-")
            name = next((c.name for c in self.rag().list_corpora() if c.display_name == want), None)
            if name is None:
                raise RuntimeError(f"no RAG Engine corpus {want!r}: make rag-corpus TENANT={tenant}, then MANAGED_MIRROR=rag_engine")
            self._corpora[tenant] = name
        return self._corpora[tenant]

    def source_of(self, tenant: str, doc_key: str) -> str:
        """A context names its version (the RagFile's display name is the doc_key); the kit's rows name the source."""
        if doc_key not in self._sources:
            from google.cloud.firestore_v1.base_query import FieldFilter
            snap = next(iter(self.db.collection("chunks").where(filter=FieldFilter("tenant_id", "==", tenant))
                             .where(filter=FieldFilter("doc_key", "==", doc_key)).select(["source_uri"]).limit(1).stream()), None)
            self._sources[doc_key] = (snap.to_dict() or {}).get("source_uri", "") if snap else ""
        return self._sources[doc_key]

    def rag_engine(self, question: str, tenant: str, k: int) -> list:
        """P9.4's backend from outside it: 4.3's corpus queried by text (4.3's distance threshold), the contexts as
        chunks with the source the ledger knows - what retriever._managed_retrieve() serves, without the API."""
        rag = self.rag()
        resp = rag.retrieval_query(
            rag_resources=[rag.RagResource(rag_corpus=self.corpus(tenant))], text=question,
            rag_retrieval_config=rag.RagRetrievalConfig(top_k=k, filter=rag.Filter(vector_distance_threshold=0.5)))
        out = []
        for i, ctx in enumerate(resp.contexts.contexts):
            key = getattr(ctx, "source_display_name", "") or ""
            out.append({"chunk_id": f"{tenant}:{key}#rag-{i}", "text": getattr(ctx, "text", "") or "",
                        "source_uri": self.source_of(tenant, key) if key else ""})
        return out

    def rerank(self, question: str, chunks: list, top_n: int = 5) -> list:
        from google.cloud import discoveryengine_v1 as discoveryengine
        if not chunks:
            return chunks
        records = [discoveryengine.RankingRecord(id=str(i), title=c["source_uri"].rsplit("/", 1)[-1],
                                                 content=c["text"][:4000]) for i, c in enumerate(chunks)]
        resp = self.ranker.rank(request=discoveryengine.RankRequest(
            ranking_config=self.ranking_config, model="semantic-ranker-fast-004",
            top_n=top_n, query=question, records=records))
        return [chunks[int(r.id)] for r in resp.records]


def arms(lane: Lane, keys=DEFAULT_ARMS) -> list:
    every = {  # key: (name, candidates(question, tenant), reranked to 5?)
        "dense5": ("dense 5, no reranker", lambda q, t: lane.dense(q, t, 5), False),
        "dense20": ("dense 20 -> rerank 5   (the lane)", lambda q, t: lane.dense(q, t, 20), True),
        "dense50": ("dense 50 -> rerank 5", lambda q, t: lane.dense(q, t, 50), True),
        "hybrid": ("hybrid 20 -> rerank 5  (4.5, not wired)", lambda q, t: lane.hybrid(q, t, 20), True),
        "rag_engine": ("rag_engine 20 -> rerank 5  (4.3's corpus, P9.4)", lambda q, t: lane.rag_engine(q, t, 20), True),
    }
    return [every[k] for k in keys]


def parse_arms(spec: str) -> tuple:
    """--arms: `all`, or a comma-separated list of arm keys; the default is the four the lane can always run."""
    if not spec or spec == "default":
        return DEFAULT_ARMS
    if spec == "all":
        return ARM_KEYS
    keys = tuple(k.strip() for k in spec.split(",") if k.strip())
    unknown = [k for k in keys if k not in ARM_KEYS]
    if unknown:
        raise ValueError(f"unknown arm(s) {unknown}: one of {', '.join(ARM_KEYS)} or all")
    return keys


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--project", default=os.environ.get("PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT"),
                    help="the project the lane runs in (default: $PROJECT)")
    ap.add_argument("--region", default="us-central1", help="where text-embedding-005 is served (regional)")
    ap.add_argument("--golden", default=GOLDEN)
    ap.add_argument("--limit", type=int, default=0, help="run only the first N anchored rows (a wiring check)")
    ap.add_argument("--tenant", help="only this tenant's rows")
    ap.add_argument("--alpha", type=float, default=0.7, help="RRF weight of the dense leg in the hybrid arm")
    ap.add_argument("--all-versions", action="store_true",
                    help="retrieve retired rows too (the lane does not: RETRIEVAL_CURRENT_ONLY=on); for a corpus that predates the ledger")
    ap.add_argument("--ledger", help="append one JSON line per arm to this file (the loop's memory)")
    ap.add_argument("--arms", default="default",
                    help="which arms: all, or a comma list of " + ", ".join(ARM_KEYS) + " (default: the four the lane always has)")
    a = ap.parse_args()
    try:
        keys = parse_arms(a.arms)
    except ValueError as e:
        print(e, file=sys.stderr)
        return 2
    if not a.project:
        print("--project (or $PROJECT) is required", file=sys.stderr)
        return 2

    rows = [json.loads(l) for l in open(a.golden, encoding="utf-8") if l.strip()]
    rows = [r for r in rows if r.get("must_retrieve") and (not a.tenant or r["tenant"] == a.tenant)]
    if a.limit:
        rows = rows[:a.limit]
    lane = Lane(a.project, a.region, a.alpha, current_only=not a.all_versions)
    print(f"{len(rows)} rows with anchors, {sum(len(r['must_retrieve']) for r in rows)} anchors, one knob per arm "
          f"(project {a.project}, embeddings in {a.region}, ranker on global)\n")
    print(f"{'arm':44} {'recall@depth':>12} {'recall@5':>9} {'mrr':>6} {'rows@1.0':>8} {'p95 ms':>7}")
    failed = 0
    for name, cands, do_rerank in arms(lane, keys):
        r_depth, r5, rr, ms, errors, misses = [], [], [], [], [], []
        for row in rows:
            t0, c, top = time.time(), None, None
            for attempt in (1, 2):                      # one retry: a dropped stream is the network, not the lane
                try:
                    c = cands(row["question"], row["tenant"])
                    top = lane.rerank(row["question"], c) if do_rerank else c[:5]
                    break
                except Exception as e:                  # one row must not stop the arm
                    c = None
                    if attempt == 2:
                        errors.append(f"{row['id']}: {type(e).__name__}: {str(e)[:120]}")
                    else:
                        time.sleep(2)
            if c is None:
                continue
            ms.append((time.time() - t0) * 1000)
            r_depth.append(recall_at(c, row["must_retrieve"], len(c)))     # this arm's ceiling
            r5.append(recall_at(top, row["must_retrieve"], 5))
            rr.append(mrr(top, row["must_retrieve"]))
            if r5[-1] < 1.0:                            # the miss list: the rows that cost a point, and WHERE the anchor went
                got = [hay(x) for x in top]
                missing = [a for a in row["must_retrieve"] if not any(a.lower() in h for h in got)]
                deeper = [a for a in missing if any(a.lower() in hay(x) for x in c)]
                misses.append(f"{row['id']}: {', '.join(missing)} - "
                              + ("ranked out (in the candidates, not the five)" if deeper and len(deeper) == len(missing)
                                 else f"not retrieved at depth {len(c)}" if not deeper
                                 else f"{', '.join(deeper)} ranked out; the rest not retrieved at depth {len(c)}"))
        if not r5:
            print(f"{name:44} {'every row failed':>12}"); failed += 1
            for e in errors[:3]:
                print("      ", e)
            continue
        line = {"arm": name, "rows": len(r5), "recall_at_depth": round(statistics.mean(r_depth), 3),
                "recall_at_5": round(statistics.mean(r5), 3), "mrr": round(statistics.mean(rr), 3),
                "rows_at_1": sum(1 for x in r5 if x == 1.0), "p95_ms": round(p95(ms)), "errors": len(errors),
                "misses": misses}
        print(f"{name:44} {line['recall_at_depth']:12.2f} {line['recall_at_5']:9.2f} {line['mrr']:6.2f} "
              f"{line['rows_at_1']:8d} {line['p95_ms']:7d}" + (f"   ({len(errors)} rows failed)" if errors else ""))
        for e in errors[:3]:
            print("      ", e)
        for m_ in misses:
            print("       lost:", m_)
        if a.ledger:
            with open(os.path.expanduser(a.ledger), "a", encoding="utf-8") as f:
                f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "project": a.project, **line}) + "\n")
    print("\nRead it in this order: recall@depth is the reranker's ceiling - if the 20 and 50 rows agree, depth is not the knob;"
          "\nrecall@5 of the lane's row minus the first row is the reranker's lift, and p95 is what it costs;"
          "\nthe hybrid row says whether a BM25 leg is worth wiring - twenty of the anchors are clause codes, its home ground;"
          "\nthe rag_engine row (--arms all) is 4.3's corpus against the lane's own rows - a managed chunk that splits a clause from its code shows up as a miss.")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
