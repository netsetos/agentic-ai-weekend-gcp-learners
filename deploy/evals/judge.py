#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make judge: the lane's own answers, scored by a second judge. Module 10 (10.4 Vertex AI Evaluation).

    make judge PROJECT=...                                # the deployed API's answers to the 64 golden rows
    make judge PROJECT=... API_B=https://candidate---...  # + pairwise: the candidate revision against the live one
    make judge PROJECT=... CHAT_URL=https://documind-chat-...   # + trajectories: /v1/chat's tool_calls, three brains
    python evals/judge.py --selftest                      # the assembly and the trajectory maths, offline

run_eval.py is the gate: nine thresholds and fifteen required rows, no model in the loop, green or red. This is the OTHER judge -
Gemini reading the same 64 answers for groundedness (is the answer supported by the context it cites?)
and fulfilment (did it answer what was asked?) through Vertex AI Evaluation, with bring-your-own
responses: nothing here generates; every response was produced by the deployed service, so the score
is a score of the lane, not of a prompt in a notebook. Every run is an Experiments run named by the
API revision (GIT_SHA) and the model behind it, so "did the tuned model help" is two runs side by side.

Pairwise (10.1's A/B, 10.4's cell 5): with --api-b the same questions go to a second URL - a Cloud Run
revision tagged `candidate` with GENERATOR_MODEL set to the tuned endpoint (make candidate) - and the
judge says which answer is better, row by row, with the live revision as the baseline.

Trajectories (10.4's cell 6): with --chat-url, a handful of rows go to each of the chat service's brains
and the tool_calls it returns are compared with the reference trajectory - the lane's real tool, in the
order a grounded answer requires. Exact, in-order and any-order match, computed here; the same columns
feed Vertex's trajectory metrics when the SDK is present.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from run_eval import GOLDEN, ask, load_golden, normalise  # noqa: E402

BRAINS = ("langchain", "langgraph", "adk")
REFERENCE_TRAJECTORY = [{"tool_name": "retrieve"}]        # one grounded answer = one retrieve, then the answer


# ----------------------------------------------------------------------------- collection
def collect(api_url: str, golden: list[dict], token: str | None, member: str, limit: int = 0) -> list[dict]:
    rows = []
    for row in golden[: limit or None]:
        status, body = ask(api_url, row["question"], row["tenant"], member, token)
        if status != 200:
            rows.append({**row, "status": status, "response": "", "context": "", "cited": []})
            continue
        cites = body.get("citations") or []
        rows.append({**row, "status": 200, "response": body.get("answer") or "",
                     "context": "\n".join(f"[{i}] {c.get('quote', '')}" for i, c in enumerate(cites, 1)),
                     "cited": [c.get("chunk_id") for c in cites], "answerable_said": bool(body.get("answerable")),
                     "model": body.get("model")})
    return rows


def chunk_texts(project: str, ids: list[str]) -> dict[str, str]:
    """The cited chunks' full text from the lane's store (lean: Firestore `chunks/{chunk_id}`, what the indexer
    wrote). Empty on any failure - the caller falls back to the quotes and says so."""
    if not ids or not project:
        return {}
    try:
        from google.cloud import firestore
        db = firestore.Client(project=project)
        out = {}
        for i in range(0, len(ids), 100):
            for snap in db.get_all([db.collection("chunks").document(c) for c in ids[i:i + 100]]):
                if snap.exists:
                    out[snap.id] = (snap.to_dict() or {}).get("text", "")
        return out
    except Exception:  # noqa: BLE001 - the judge still runs, on the quotes
        return {}


def enrich_context(rows: list[dict], project: str, fetch=None) -> int:
    """The judge reads the context the answer CITED, in full. The API returns a citation's quote (the clause,
    at most twenty-five words), and a groundedness judge given only quotes marks every answer that says more
    than its quote as unsupported: the first live run scored the lane 0.25 grounded on 64 rows for exactly that
    (F39, 10 September). The cited chunks' text is the context; the quotes stay as the fallback."""
    ids = sorted({c for r in rows for c in (r.get("cited") or []) if c})
    texts = (fetch or (lambda i: chunk_texts(project, i)))(ids)
    hit = 0
    for r in rows:
        parts = []
        for i, cid in enumerate(r.get("cited") or [], 1):
            t = texts.get(cid)
            if t:
                hit += 1
            parts.append(f"[{i}] {t or ''}".rstrip() if t else None)
        if parts and any(parts):
            quotes = r["context"].split("\n") if r.get("context") else []
            r["context"] = "\n".join(p if p else (quotes[i] if i < len(quotes) else "") for i, p in enumerate(parts))
    return hit


def to_frame(rows: list[dict], baseline: list[dict] | None = None):
    """The columns Vertex AI Evaluation reads for bring-your-own responses."""
    import pandas as pd
    # The catalogue's GROUNDEDNESS reads `prompt` and `response` and nothing else: it asks whether the response
    # says more than the prompt supports. A bare question as the prompt makes every grounded statute answer
    # "unsupported" and every refusal "grounded" - which is the 0.25 the second live run scored, sixteen refusals
    # of sixty-four rows, unchanged by the full chunk text in a column the template never read (F40). The prompt
    # is the lane's own prompt shape: the context, then the question. `context` stays for rubrics that name it.
    df = pd.DataFrame({
        "prompt": [f"Answer the question using only the context below.\n\nContext:\n{r['context'] or '(no passages were retrieved)'}"
                   f"\n\nQuestion: {r['question']}" for r in rows],
        "response": [r["response"] for r in rows],
        "context": [r["context"] for r in rows],
        "reference": [" ".join(r.get("must_contain", [])) for r in rows],
        "instruction": ["Answer the question using only the retrieved DocuMind context."] * len(rows),
        "shape": [r["shape"] for r in rows],
        "id": [r["id"] for r in rows],
    })
    if baseline:
        by_id = {b["id"]: b["response"] for b in baseline}
        df["baseline_model_response"] = [by_id.get(r["id"], "") for r in rows]
    return df


# ----------------------------------------------------------------------------- trajectories
def chat_turn(chat_url: str, question: str, brain: str, token: str | None) -> dict:
    body = json.dumps({"question": question, "session_id": f"judge-{brain}", "brain": brain}).encode()
    req = urllib.request.Request(f"{chat_url.rstrip('/')}/v1/chat", data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(req, timeout=180) as r:
        return json.loads(r.read())


def trajectory_metrics(pred: list[dict], ref: list[dict]) -> dict:
    """10.4's three matches, on tool NAMES: exact, in order (subsequence), any order (multiset)."""
    p = [t["tool_name"] for t in pred]
    q = [t["tool_name"] for t in ref]
    i = 0
    for name in p:
        if i < len(q) and name == q[i]:
            i += 1
    in_order = int(i == len(q))
    any_order = int(all(p.count(n) >= q.count(n) for n in set(q)))
    return {"exact": int(p == q), "in_order": in_order, "any_order": any_order, "calls": len(p)}


def trajectories(chat_url: str, golden: list[dict], token: str | None, rows: int = 6) -> list[dict]:
    out = []
    picks = [g for g in golden if g["answerable"] and g["tenant"] == "acme"][:rows]
    for brain in BRAINS:
        for g in picks:
            try:
                body = chat_turn(chat_url, g["question"], brain, token)
                pred = [{"tool_name": n} for n in body.get("tool_calls") or []]
                m = trajectory_metrics(pred, REFERENCE_TRAJECTORY)
                out.append({"brain": brain, "id": g["id"], **m, "answered": bool(body.get("answer")),
                            "refusals": body.get("refusals") or []})
            except Exception as e:  # noqa: BLE001
                out.append({"brain": brain, "id": g["id"], "exact": 0, "in_order": 0, "any_order": 0, "calls": 0,
                            "answered": False, "error": f"{type(e).__name__}: {str(e)[:80]}"})
    return out


# ----------------------------------------------------------------------------- vertex ai evaluation
SECOND_METRIC = ("FULFILLMENT", "INSTRUCTION_FOLLOWING", "QUESTION_ANSWERING_QUALITY")


def pointwise_metrics():
    """GROUNDEDNESS, and the SDK's name for "did it answer what was asked". The catalogue renamed FULFILLMENT
    to INSTRUCTION_FOLLOWING between SDK generations, and the first live `make judge` (F36, 10 September) stopped
    on the old name after seventeen minutes of collection. Resolve by preference, and say which one ran."""
    from vertexai.evaluation import MetricPromptTemplateExamples as ex
    have = [n for n in dir(ex.Pointwise) if n.isupper()]
    second = next((n for n in SECOND_METRIC if n in have), None)
    if second is None:
        raise SystemExit(f"none of {SECOND_METRIC} in this SDK's pointwise examples: {have}")
    return ["GROUNDEDNESS", second], [ex.Pointwise.GROUNDEDNESS, getattr(ex.Pointwise, second)]


def revision_sha(api_url: str, project: str) -> str:
    """The GIT_SHA on the API revision that answered. The run is named for what was judged, not for the
    clone's HEAD: the first live run was labelled with the notebook commit the shell happened to be at."""
    m = re.match(r"https://(?:[a-z0-9-]+---)?documind-api-\d+\.([a-z0-9-]+)\.run\.app", api_url or "")
    if not m or not project:
        return ""
    try:
        out = subprocess.run(["gcloud", "run", "services", "describe", "documind-api", "--region", m.group(1),
                              "--project", project, "--format=json"], capture_output=True, text=True, timeout=60).stdout
        env = {e["name"]: e.get("value", "") for e in json.loads(out)["spec"]["template"]["spec"]["containers"][0].get("env", [])}
        return env.get("GIT_SHA", "")
    except Exception:  # noqa: BLE001 - a label, never the run
        return ""


def template_hash(names: list[str]) -> str:
    """Six hex digits of the metric prompt templates the installed SDK ships: a judge is its prompt, and a run
    judged by a different one is a different run. Goes into the run name."""
    import hashlib
    from vertexai.evaluation import MetricPromptTemplateExamples as ex
    text = "\n".join(str(getattr(ex.Pointwise, n).metric_prompt_template) for n in names)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:6]


def evaluate(df, experiment: str, run_name: str, pairwise: bool, project: str, location: str = "us-central1",
             judge_model: str = "") -> dict:
    """Pointwise groundedness and fulfilment (or its successor); pairwise question-answering quality when a baseline column exists.

    12 September 2026: the judge is named. --judge-model pins the autorater when the SDK exposes AutoraterConfig
    (else the service's default judges, and the run says so); what did NOT run - pairwise without --api-b,
    trajectories without --chat-url - is printed as off, never left to be inferred from an absent column."""
    import vertexai
    from vertexai.evaluation import EvalTask, MetricPromptTemplateExamples, PairwiseMetric
    vertexai.init(project=project, location=location, experiment=experiment)
    names, metrics = pointwise_metrics()
    print(f"  pointwise: {' + '.join(names)}  (templates {template_hash(names)})")
    if pairwise:
        metrics.append(PairwiseMetric(
            metric="pairwise_question_answering_quality",
            metric_prompt_template=MetricPromptTemplateExamples.get_prompt_template("pairwise_question_answering_quality")))
    else:
        print("  pairwise: off (no --api-b)")
    kwargs = {}
    if judge_model:
        try:
            from vertexai.evaluation import AutoraterConfig
            kwargs["autorater_config"] = AutoraterConfig(autorater_model=judge_model)
            print(f"  judge: {judge_model}")
        except ImportError:
            print(f"  judge: the service default - this SDK has no AutoraterConfig, --judge-model {judge_model} not applied")
    else:
        print("  judge: the service default (pass --judge-model to pin one)")
    result = EvalTask(dataset=df, metrics=metrics, experiment=experiment, **kwargs).evaluate(experiment_run_name=run_name)
    summary = dict(result.summary_metrics)
    # By shape, from the per-row table: a refusal row has no context to be grounded in, so the overall mean
    # mixes what the judge can score with what it cannot. The answerable shapes are the number that means something.
    try:
        table = result.metrics_table
        col = next(c for c in table.columns if c.endswith("groundedness/score"))
        for shape, sub in table.groupby("shape"):
            summary[f"groundedness/mean[{shape}]"] = float(sub[col].mean())
    except Exception:  # noqa: BLE001 - the summary is the contract; the breakdown is a courtesy
        pass
    return summary


# ----------------------------------------------------------------------------- selftest
def selftest() -> int:
    rows = [{"id": "lk-06", "shape": "lookup", "tenant": "acme", "question": "notice?", "must_contain": ["60"],
             "answerable": True, "status": 200, "response": "60 days [1]", "context": "[1] sixty days", "cited": ["c1"]},
            {"id": "rf-04", "shape": "refusal", "tenant": "acme", "question": "FY2024?", "must_contain": [],
             "answerable": False, "status": 200, "response": "Not in the documents.", "context": "", "cited": []}]
    hit = enrich_context(rows, "p", fetch=lambda ids: {"c1": "NP-03. A confirmed E3 serves a notice period of sixty days."})
    assert hit == 1 and rows[0]["context"] == "[1] NP-03. A confirmed E3 serves a notice period of sixty days." and rows[1]["context"] == "", rows
    assert enrich_context(rows, "p", fetch=lambda ids: {}) == 0 and rows[0]["context"].startswith("[1] NP-03")   # nothing fetched: the context stands
    df = to_frame(rows, baseline=[{"id": "lk-06", "response": "sixty days"}, {"id": "rf-04", "response": "no"}])
    assert list(df.columns) == ["prompt", "response", "context", "reference", "instruction", "shape", "id", "baseline_model_response"]
    assert df.loc[0, "baseline_model_response"] == "sixty days" and df.loc[0, "reference"] == "60"
    assert df.loc[0, "prompt"].startswith("Answer the question using only the context below.\n\nContext:\n[1] NP-03") \
        and df.loc[0, "prompt"].endswith("Question: notice?"), df.loc[0, "prompt"]
    assert "(no passages were retrieved)" in df.loc[1, "prompt"]
    ref = REFERENCE_TRAJECTORY
    assert trajectory_metrics([{"tool_name": "retrieve"}], ref) == {"exact": 1, "in_order": 1, "any_order": 1, "calls": 1}
    assert trajectory_metrics([{"tool_name": "calculate_cost"}, {"tool_name": "retrieve"}], ref) == {"exact": 0, "in_order": 1, "any_order": 1, "calls": 2}
    assert trajectory_metrics([], ref) == {"exact": 0, "in_order": 0, "any_order": 0, "calls": 0}
    assert normalise("Sixty days") == "60 days"
    print("selftest: the cited chunk's text replaces its quote as the context and a miss keeps the quote; the prompt the "
          "judge reads carries the context then the question; the frame carries the six judge columns plus the baseline; "
          "the trajectory maths is right on the three cases")
    return 0


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--api-url", default=os.environ.get("DOCUMIND_API_URL", ""))
    ap.add_argument("--api-b", default=os.environ.get("DOCUMIND_API_B", ""), help="a candidate revision URL: pairwise against --api-url")
    ap.add_argument("--chat-url", default=os.environ.get("DOCUMIND_CHAT_URL", ""), help="the chat service: trajectories for the three brains")
    ap.add_argument("--project", default=os.environ.get("DOCUMIND_PROJECT") or os.environ.get("GOOGLE_CLOUD_PROJECT", ""))
    ap.add_argument("--experiment", default="documind-eval")
    ap.add_argument("--label", default="", help="names the Experiments run api-<label>-<time>; default: the API revision's GIT_SHA")
    ap.add_argument("--rows", type=int, default=0, help="only the first N golden rows (a wiring check)")
    ap.add_argument("--judge-model", default=os.environ.get("DOCUMIND_JUDGE_MODEL", ""),
                    help="pin the autorater (e.g. gemini-3.1-flash-lite); default: the Evaluation service's own")
    ap.add_argument("--trajectory-rows", type=int, default=6)
    ap.add_argument("--no-vertex", action="store_true", help="collect and print, skip the Evaluation service")
    ap.add_argument("--reuse", default="", help="a JSON file: load the collected rows from it if it exists, else collect and write it")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.api_url:
        print("--api-url (or DOCUMIND_API_URL) is required - this is a LIVE tool; --selftest is the offline half")
        return 2
    token = os.environ.get("DOCUMIND_ID_TOKEN") or None
    member = os.environ.get("DOCUMIND_USER_EMAIL", "eval@documind.in")
    golden = load_golden()
    t0 = time.time()
    # --reuse: the collection is the slow, billed half (64 answers from the lane); a run that stops in the
    # Evaluation service should not have to ask the lane again. The file holds the rows as collected.
    if a.reuse and os.path.isfile(a.reuse):
        rows = json.load(open(a.reuse, encoding="utf-8"))
        print(f"  {len(rows)} rows reused from {a.reuse} (delete it to collect again)")
    else:
        rows = collect(a.api_url, golden, token, member, a.rows)
        if a.reuse:
            with open(a.reuse, "w", encoding="utf-8") as f:
                json.dump(rows, f, ensure_ascii=False)
    ok = [r for r in rows if r["status"] == 200]
    print(f"  {len(ok)}/{len(rows)} answers collected from {a.api_url} in {time.time() - t0:.0f}s "
          f"(model {next((r.get('model') for r in ok if r.get('model')), '?')})")
    bad = [(r["id"], r["status"]) for r in rows if r["status"] != 200]
    if bad:
        print(f"  not collected (HTTP status): {bad}")
    if not ok:
        print("  nothing to judge: no answer came back 200 - fix the lane (or the token's audience) before judging it")
        return 1
    baseline = None
    if a.api_b:
        cand = collect(a.api_b, golden, os.environ.get("DOCUMIND_ID_TOKEN_B") or token, member, a.rows)
        print(f"  {sum(r['status'] == 200 for r in cand)}/{len(cand)} candidate answers from {a.api_b}")
        if not any(r["status"] == 200 for r in cand):
            # The first pairwise run judged an empty frame and wrote a row_count 0 run to Experiments (F42).
            print(f"  nothing to judge: the candidate answered {sorted({r['status'] for r in cand})} on every row - "
                  f"a 401 is the token's audience (the candidate URL must be one the API accepts), a 500 is the model")
            return 1
        baseline, rows = rows, cand                         # the candidate is judged; the live revision is the baseline
    hit = enrich_context([r for r in rows if r["status"] == 200], a.project)
    cited = sum(len(r.get("cited") or []) for r in rows if r["status"] == 200)
    print(f"  context: {hit}/{cited} cited chunks read in full from the store" + ("" if hit else " - the judge reads the quotes only"))
    df = to_frame([r for r in rows if r["status"] == 200], baseline=baseline)
    if not a.chat_url:
        print("  trajectories: off (no --chat-url)")
    if a.chat_url:
        tr = trajectories(a.chat_url, golden, os.environ.get("DOCUMIND_ID_TOKEN_CHAT") or token, a.trajectory_rows)
        for brain in BRAINS:
            b = [t for t in tr if t["brain"] == brain]
            if b:
                print(f"  trajectory {brain:10} exact {sum(t['exact'] for t in b)}/{len(b)}  in-order {sum(t['in_order'] for t in b)}/{len(b)}  "
                      f"any-order {sum(t['any_order'] for t in b)}/{len(b)}  mean calls {sum(t['calls'] for t in b) / len(b):.1f}"
                      + (f"  errors {sum(1 for t in b if t.get('error'))}" if any(t.get("error") for t in b) else ""))
    if a.no_vertex:
        print(df.head(3).to_string())
        return 0
    if not a.project:
        print("--project is required for the Evaluation service")
        return 2
    # Unique per invocation: Experiments refuses a run name that exists (409, F38 - the second live run collided
    # with the first's half-made run). The label groups runs by the revision that answered; the time keeps them apart.
    label = a.label or revision_sha(a.api_url, a.project) or os.environ.get("GIT_SHA", "dev")
    run_name = f"api-{label}-{time.strftime('%Y%m%d-%H%M')}" + ("-vs-candidate" if a.api_b else "")
    summary = evaluate(df, a.experiment, run_name, pairwise=bool(a.api_b), project=a.project, judge_model=a.judge_model)
    print(f"\n  Experiments run {a.experiment}/{run_name}:")
    for k in sorted(summary):
        if k.endswith("/mean") or "/mean[" in k or k.endswith("/std") or "win_rate" in k or k == "row_count":
            v = summary[k]
            print(f"    {k:48} {v:.3f}" if isinstance(v, (int, float)) else f"    {k:48} {v}")
    print("\n  the gate (run_eval.py) still decides; this judge explains. Where they disagree, read the row.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
