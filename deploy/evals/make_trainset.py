#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""make trainset: the tuning dataset, as a document. Module 10 (10.1 managed SFT, 10.5 the small model).

    make trainset PROJECT=... [TENANT=acme] [ROWS=300]        # from deploy/: build, scan, freeze, upload
    python evals/make_trainset.py --project P --tenant acme --rows 300 --upload gs://P-datasets/sft/
    python evals/make_trainset.py --selftest                   # the rules on fixtures, offline, no credential

Where the training data IS (and is not). Not the logs: sink.tf keeps question and answer out of BigQuery
on purpose. Not the answer cache: on the lane it holds the golden questions every eval run asked, and a
model trained on its own test set has learned nothing you can measure. It is the CORPUS - the real Acts
and the synthetic handbooks the lane already chunks (shared/documind_corpus.py, the mirrors under
evals/corpus/) - with one question and one answer written per chunk by gemini-3.6-flash, in the lane's
own grammar: the answer is ModelDraft's JSON (answer, [Source N] citations with a quote, confidence,
answerable), and the user turn is the exact prompt shape services/rag-api/generator.py builds
(SYSTEM, Context:, [Source 1] ..., Question:). A model tuned on this learns the HABIT the lane serves,
not facts (10.5: facts are retrieval's job). Every tenth chunk also yields a refusal: a question the
passage does not answer, answered with answerable=false - so the habit includes saying no.

Four rules make the file defensible, and each is a function below with a fixture that turns it red:
  exclude_golden  a generated question that overlaps a golden question is DROPPED (normalised token
                  overlap; run_eval.normalise). The golden set is the test set. It never enters the file.
  redact          shared/pii.inspect_many over every question and answer; any finding drops the row.
                  Weights cannot be filtered per tenant afterwards.
  write           two formats from one list - Gemini SFT (`contents`) and chat (`messages`, for unsloth
                  and trl) - so 10.1 and 10.5 tune on the same rows; and a manifest: date, tenant, rows,
                  refusals, what was dropped and why, a sha256 per file, the corpus manifest's own sha.
  frozen          the version is in the file name. A changed corpus is a new version, never an edit.

--batch writes the same requests as a Batch API file (10.3: half price, no rate limit), submits the job
and prints how to collect; --collect turns the job's output back into the same rows. Tier B, live.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DEPLOY = os.path.dirname(HERE)
sys.path.insert(0, DEPLOY)          # shared/
sys.path.insert(0, HERE)            # run_eval (normalise), documind_corpus via shared

GOLDEN = os.path.join(HERE, "golden.jsonl")
OUT_DIR = os.path.join(HERE, "sft")
VERSION = os.environ.get("TRAINSET_VERSION", "v1")
GEN_MODEL = "gemini-3.6-flash"
MIN_CHUNK_CHARS = 400
REFUSAL_EVERY = 10
OVERLAP_DROP = 0.5                  # Jaccard over content tokens; or containment, or four shared tokens

# The generator's SYSTEM, verbatim (services/rag-api/generator.py). The tuned model is served behind
# that prompt, so it is trained behind that prompt. tools/check_auth_wiring.py holds the two equal.
SYSTEM = """You are DocuMind, a retrieval-grounded assistant.
Rules:
1. Answer ONLY from the numbered context below. Never invent sources.
2. Cite using [N] where N is the chunk number. Multiple chunks: [1,2].
3. If the context does not contain the answer, set answerable=false and say so.
4. Keep answers under 300 words unless asked for more.
5. A quote is the clause that answers - at most twenty-five words, never a whole section.
"""


# ----------------------------------------------------------------------------- the corpus
def load_chunks(tenant: str, project_id: str = "documind-ai-YOUR-ID") -> list[dict]:
    """The lane's own chunks, from the mirrors - the same windows the worker writes (shared/documind_corpus)."""
    from shared import documind_corpus as dc
    docs = dc.load_documents(tenant, HERE, project_id)
    chunks = [c for d in docs for c in dc.chunk_document(d, tenant)]
    return [c for c in chunks if len(c["text"].strip()) >= MIN_CHUNK_CHARS]


def sample(chunks: list[dict], rows: int) -> list[dict]:
    """Deterministic and spread over every document: sorted by id, then evenly strided."""
    chunks = sorted(chunks, key=lambda c: c["chunk_id"])
    if rows >= len(chunks):
        return chunks
    stride = len(chunks) / rows
    return [chunks[int(i * stride)] for i in range(rows)]


# ----------------------------------------------------------------------------- the pairs
def user_turn(text: str, question: str) -> str:
    """Exactly the prompt the generator serves: SYSTEM, one numbered source, the question."""
    return f"{SYSTEM}\n\nContext:\n[Source 1] {text.strip()}\n\nQuestion: {question.strip()}"


def target(answer: str, quote: str, answerable: bool) -> str:
    """ModelDraft's JSON (shared/documind_schemas.py): what resolve() reads back into Citations."""
    draft = {"answer": answer.strip(),
             "citations": ([{"source": 1, "quote": quote.strip()[:200]}] if answerable and quote else []),
             "confidence": "high" if answerable else "low",
             "answerable": bool(answerable)}
    return json.dumps(draft, ensure_ascii=False)


def _pair_schema():
    from pydantic import BaseModel, Field

    class Pair(BaseModel):
        question: str = Field(description="One natural question a colleague would ask that this passage answers")
        answer: str = Field(description="The answer, in one or two sentences, from the passage only")
        quote: str = Field(description="The clause that answers - at most twenty-five words, copied exactly")
        unanswerable_question: str = Field(description="One natural question on the same topic that this passage does NOT answer")
    return Pair


def ask_pairs(project: str, chunks: list[dict], refusal_every: int = REFUSAL_EVERY) -> list[dict]:
    """One structured call per chunk on the global endpoint; every refusal_every-th chunk also yields a refusal."""
    from google import genai
    from google.genai import types
    client = genai.Client(enterprise=True, project=project, location="global")
    Pair = _pair_schema()
    rows = []
    for i, c in enumerate(chunks):
        r = client.models.generate_content(
            model=GEN_MODEL,
            contents=("Read this passage from an Indian statute or a company document. Write one question a colleague "
                      "would ask that it answers, the answer from the passage only, the clause that answers (copied "
                      "exactly, at most twenty-five words), and one question on the same topic the passage does NOT "
                      "answer. Do not invent facts.\n\nPassage:\n" + c["text"][:2400]),
            config=types.GenerateContentConfig(response_mime_type="application/json", response_schema=Pair,
                                               thinking_config=types.ThinkingConfig(thinking_level="LOW")))
        if not r.parsed:
            continue
        p = r.parsed
        rows.append({"chunk_id": c["chunk_id"], "source_uri": c["source_uri"], "question": p.question,
                     "answer": p.answer, "quote": p.quote, "answerable": True, "text": c["text"]})
        if refusal_every and i % refusal_every == refusal_every - 1 and p.unanswerable_question.strip():
            rows.append({"chunk_id": c["chunk_id"], "source_uri": c["source_uri"],
                         "question": p.unanswerable_question,
                         "answer": "The provided context does not answer this question.", "quote": "",
                         "answerable": False, "text": c["text"]})
    return rows


# ----------------------------------------------------------------------------- the rules
STOPWORDS = {"the", "and", "for", "what", "which", "who", "how", "many", "much", "does", "did", "was", "were", "are",
             "that", "this", "with", "under", "from", "into", "when", "where", "year", "per", "act", "code", "its",
             "their", "his", "her", "has", "have", "can", "may", "must", "should", "there", "about", "between",
             "after", "before", "than", "then", "you", "your", "our", "not", "any", "all", "one", "two"}


def _content(q: str) -> set[str]:
    """Content tokens: normalised (run_eval.normalise), three letters or more, no function words."""
    from run_eval import normalise
    return {w for w in re.findall(r"[a-z0-9]+", normalise(q)) if len(w) > 2 and w not in STOPWORDS}


def exclude_golden(rows: list[dict], golden: list[dict], threshold: float = OVERLAP_DROP) -> tuple[list[dict], list[dict]]:
    """Drop what the golden set could recognise. The golden set is the TEST set, and it never enters the file.

    Two rules, because one was not enough (the first selftest passed 'which region's revenue declined'
    against 'which region shrank' on a lexical score alone):
      EVIDENCE   a chunk from the document or clause a golden row retrieves from (its must_retrieve
                 anchors, in the chunk id, the source or the text) that carries one of the row's
                 must_contain phrases (four characters or more AFTER normalisation, the row's own
                 tenant) is the evidence the gate scores against; no row is written from it.
      QUESTION   a generated question whose content tokens overlap a golden question's - Jaccard over
                 the threshold, one contained in the other, or four tokens shared - is a rephrasing,
                 and a model memorises a rephrasing as well as the words.
    The manifest records how many rows each rule removed and which golden ids they touched.

    F34 (the first live build, 10 September): unscoped, the evidence rule dropped 180 of 330 rows -
    "cannot" (jn-01), "5 years" and "15 days" (the gratuity rows), "set on" (mm-02) occur in hundreds of
    statute chunks, and "eight" passed the length check before it was normalised to "8". A phrase is
    evidence only where the golden row would find it.
    """
    from run_eval import normalise

    def _phrases(g):
        return [p for p in (normalise(m) for m in g.get("must_contain", [])) if len(p) >= 4]

    gold = [(g["id"], _content(g["question"]), _phrases(g), g.get("tenant"),
             [normalise(a) for a in g.get("must_retrieve", []) if a])
            for g in golden]
    kept, dropped = [], []
    for r in rows:
        t = _content(r["question"])
        text_n = normalise(r.get("text", ""))
        where_n = normalise(f"{r.get('chunk_id', '')} {r.get('source_uri', '')}")
        tenant = r.get("chunk_id", "").split(":", 1)[0]
        hit = None
        for gid, gt, musts, gtenant, anchors in gold:
            in_scope = not anchors or any(a in where_n or a in text_n for a in anchors)
            if musts and in_scope and (not gtenant or gtenant == tenant) and any(m in text_n for m in musts):
                hit = (gid, "evidence")
                break
            if t and gt:
                inter = len(t & gt)
                if inter / len(t | gt) >= threshold or t <= gt or gt <= t or inter >= 4:
                    hit = (gid, "question")
                    break
        if hit:
            dropped.append({**r, "overlaps": hit[0], "rule": hit[1]})
        else:
            kept.append(r)
    return kept, dropped


def redact(rows: list[dict], inspect_many=None) -> tuple[list[dict], int]:
    """Drop, never rewrite: a row that carries PII is not worth a redacted version (shared/pii, the ONE list)."""
    if inspect_many is None:
        from shared.pii import inspect_many
    texts = [x for r in rows for x in (r["question"], r["answer"])]
    findings = inspect_many(texts)
    kept, dropped = [], 0
    for i, r in enumerate(rows):
        if findings[2 * i] or findings[2 * i + 1]:
            dropped += 1
            continue
        kept.append(r)
    return kept, dropped


# ----------------------------------------------------------------------------- the files
def to_vertex(r: dict) -> dict:
    """Gemini supervised-tuning JSONL: a system instruction, a user turn, a model turn."""
    return {"systemInstruction": {"role": "system", "parts": [{"text": SYSTEM}]},
            "contents": [{"role": "user", "parts": [{"text": user_turn(r["text"], r["question"])}]},
                         {"role": "model", "parts": [{"text": target(r["answer"], r["quote"], r["answerable"])}]}]}


def to_chat(r: dict) -> dict:
    """The messages format unsloth / trl read (10.5): the same three turns."""
    return {"messages": [{"role": "system", "content": SYSTEM},
                         {"role": "user", "content": user_turn(r["text"], r["question"])},
                         {"role": "assistant", "content": target(r["answer"], r["quote"], r["answerable"])}]}


def _sha(path: str) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()


def _rel(path: str) -> str:
    """deploy/-relative when the file is under deploy/, else the name: a temp dir on another drive is not."""
    try:
        return os.path.relpath(path, DEPLOY).replace(os.sep, "/")
    except ValueError:
        return os.path.basename(path)


def write(rows: list[dict], out_dir: str, version: str, tenant: str, dropped_golden: list[dict], dropped_pii: int,
          today: dt.date | None = None) -> dict:
    os.makedirs(out_dir, exist_ok=True)
    base = os.path.join(out_dir, f"documind_sft_{version}")
    paths = {"vertex": base + ".vertex.jsonl", "chat": base + ".chat.jsonl"}
    for fmt, conv in (("vertex", to_vertex), ("chat", to_chat)):
        with open(paths[fmt], "w", encoding="utf-8", newline="\n") as f:
            for r in rows:
                f.write(json.dumps(conv(r), ensure_ascii=False) + "\n")
    corpus_manifest = os.path.join(HERE, "manifest.json")
    manifest = {
        "version": version, "built_at": (today or dt.date.today()).isoformat(), "tenant": tenant,
        "rows": len(rows), "refusals": sum(1 for r in rows if not r["answerable"]),
        "documents": sorted({r["source_uri"].rsplit("/", 1)[-1] for r in rows}),
        "dropped_golden_overlap": len(dropped_golden),
        "dropped_golden_by_rule": {rule: sum(1 for d in dropped_golden if d.get("rule") == rule) for rule in ("evidence", "question")},
        "dropped_golden_ids": sorted({d["overlaps"] for d in dropped_golden}),
        "dropped_pii": dropped_pii,
        "generator": GEN_MODEL, "source": "evals/corpus mirrors via shared/documind_corpus.py - never traffic, never the answer cache",
        "corpus_manifest_sha256": _sha(corpus_manifest)[:16] if os.path.isfile(corpus_manifest) else None,
        "files": {fmt: {"path": _rel(p), "sha256": _sha(p)} for fmt, p in paths.items()},
        "base_model_default": {"managed": "gemini-3.1-flash-lite", "slm": "unsloth/gemma-4-E2B-it"},
        "rule": "frozen: a changed corpus is a new version, never an edit of this file",
    }
    with open(base + ".manifest.json", "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, indent=1, ensure_ascii=False)
        f.write("\n")
    return manifest


def upload(out_dir: str, version: str, dest: str) -> None:
    """gs://PROJECT-datasets/sft/ - the bucket storage.tf makes for exactly this (and for 10.5's GGUF)."""
    from google.cloud import storage
    bucket, _, prefix = dest.removeprefix("gs://").partition("/")
    client = storage.Client()
    for name in os.listdir(out_dir):
        if name.startswith(f"documind_sft_{version}"):
            blob = client.bucket(bucket).blob(prefix.rstrip("/") + "/" + name)
            blob.upload_from_filename(os.path.join(out_dir, name))
            print(f"  uploaded gs://{bucket}/{blob.name}")


# ----------------------------------------------------------------------------- the batch lane (10.3)
def write_batch_requests(chunks: list[dict], path: str) -> int:
    """The same call as ask_pairs, as Batch API request lines - half price, no rate limit (10.3)."""
    schema = {"type": "OBJECT", "properties": {"question": {"type": "STRING"}, "answer": {"type": "STRING"},
                                               "quote": {"type": "STRING"}, "unanswerable_question": {"type": "STRING"}},
              "required": ["question", "answer", "quote", "unanswerable_question"]}
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        for c in chunks:
            f.write(json.dumps({"custom_id": c["chunk_id"], "request": {
                "contents": [{"role": "user", "parts": [{"text": "Read this passage from an Indian statute or a company document. Write one question a colleague would ask that it answers, the answer from the passage only, the clause that answers (copied exactly, at most twenty-five words), and one question on the same topic the passage does NOT answer. Do not invent facts.\n\nPassage:\n" + c["text"][:2400]}]}],
                "generationConfig": {"responseMimeType": "application/json", "responseSchema": schema}}},
                ensure_ascii=False) + "\n")
    return len(chunks)


def submit_batch(project: str, requests_uri: str, dest_uri: str, display_name: str):
    """Batch is REGIONAL on Vertex: this client submits, it does not generate (10.3)."""
    from google import genai
    from google.genai import types
    client = genai.Client(enterprise=True, project=project, location="us-central1")
    for job in client.batches.list():                       # idempotent: a scheduler retry is not a second bill
        if job.display_name == display_name and str(job.state) not in ("JOB_STATE_FAILED", "JOB_STATE_CANCELLED"):
            return job
    return client.batches.create(model=GEN_MODEL, src=requests_uri,
                                 config=types.CreateBatchJobConfig(dest=dest_uri, display_name=display_name))


def collect_batch(output_lines: list[str], chunks_by_id: dict, refusal_every: int = REFUSAL_EVERY) -> list[dict]:
    """The job's predictions back into rows: the same shape ask_pairs returns."""
    rows = []
    for i, line in enumerate(output_lines):
        obj = json.loads(line)
        cid = obj.get("custom_id")
        c = chunks_by_id.get(cid)
        if not c:
            continue
        try:
            text = obj["response"]["candidates"][0]["content"]["parts"][0]["text"]
            p = json.loads(text)
        except (KeyError, IndexError, ValueError):
            continue
        rows.append({"chunk_id": cid, "source_uri": c["source_uri"], "question": p["question"], "answer": p["answer"],
                     "quote": p.get("quote", ""), "answerable": True, "text": c["text"]})
        if refusal_every and i % refusal_every == refusal_every - 1 and p.get("unanswerable_question"):
            rows.append({"chunk_id": cid, "source_uri": c["source_uri"], "question": p["unanswerable_question"],
                         "answer": "The provided context does not answer this question.", "quote": "",
                         "answerable": False, "text": c["text"]})
    return rows


# ----------------------------------------------------------------------------- selftest
def selftest() -> int:
    import tempfile
    from shared.documind_schemas import ModelDraft
    golden = [{"id": "jn-06", "tenant": "acme", "must_contain": ["EMEA", "11.4"], "must_retrieve": ["annual_report_2026"],
               "question": "Which region shrank in FY2026 and what was ACME's attrition that year?"},
              {"id": "lk-06", "tenant": "acme", "must_contain": ["60"], "must_retrieve": ["NP-03", "hr_policy_2026"],
               "question": "What is the notice period for a confirmed E3?"},
              {"id": "lk-09", "tenant": "acme", "must_contain": ["eight"], "must_retrieve": ["WFH-01", "hr_policy_2026"],
               "question": "How many days a month can I work remotely?"}]
    rows = [
        {"chunk_id": "acme:annual_report_2026#AR-02", "source_uri": "gs://b/acme/annual_report_2026.md",
         "question": "Which region's revenue declined in FY2026?", "answer": "EMEA fell 5.2% to 91 crore.",
         "quote": "EMEA | 96 | 91 | -5.2%", "answerable": True, "text": "| EMEA | 96 | 91 | -5.2% |"},
        {"chunk_id": "acme:code_on_wages_2019#p3-0", "source_uri": "gs://b/acme/code_on_wages_2019.pdf",
         "question": "What does the Code on Wages define as wages?", "answer": "All remuneration expressed in money.",
         "quote": "wages means all remuneration", "answerable": True, "text": "wages means all remuneration ..."},
        {"chunk_id": "acme:inv_2026_0412#0", "source_uri": "gs://b/acme/inv_2026_0412.md",
         "question": "What is the PAN on the invoice?", "answer": "PAN AAAPZ1234C.", "quote": "PAN: AAAPZ1234C",
         "answerable": True, "text": "PAN: AAAPZ1234C"},
        {"chunk_id": "acme:code_on_wages_2019#p3-0", "source_uri": "gs://b/acme/code_on_wages_2019.pdf",
         "question": "Does the Code on Wages set a retirement age?", "answer": "The provided context does not answer this question.",
         "quote": "", "answerable": False, "text": "wages means all remuneration ..."},
        {"chunk_id": "acme:hr_policy_2026#NP-03", "source_uri": "gs://b/acme/hr_policy_2026.md",
         "question": "What is the notice period for a confirmed E3 employee?", "answer": "Sixty days.",
         "quote": "a confirmed E3 serves sixty days", "answerable": True, "text": "NP-03. A confirmed E3 serves a notice period of sixty days."},
        # F34's two twins. EMEA in a handbook chunk is not jn-06's evidence: the row retrieves from the annual
        # report, and the phrase is only evidence where the row would find it. And "eight" is five letters
        # until it is normalised to "8", which is too short to be a phrase at all - this LV-01 chunk carries
        # an 8 and lk-09 must not drop it.
        {"chunk_id": "acme:labour_codes_compliance_handbook#p4-0", "source_uri": "gs://b/acme/labour_codes_compliance_handbook.pdf",
         "question": "Which regions does the handbook cover?", "answer": "India, with notes for EMEA subsidiaries.",
         "quote": "notes for EMEA subsidiaries", "answerable": True, "text": "This handbook covers India, with notes for EMEA subsidiaries."},
        {"chunk_id": "acme:hr_policy_2026#LV-01", "source_uri": "gs://b/acme/hr_policy_2026.md",
         "question": "How does earned leave accrue?", "answer": "At 1.75 days per completed month, capped at 8 days a quarter.",
         "quote": "accrues at 1.75 days per completed month", "answerable": True,
         "text": "LV-01. Earned leave accrues at 1.75 days per completed month, capped at 8 days a quarter."},
    ]
    kept, dropped = exclude_golden(rows, golden)
    assert [(d["overlaps"], d["rule"]) for d in dropped] == [("jn-06", "evidence"), ("lk-06", "question")], dropped
    assert len(kept) == 5, [k["chunk_id"] for k in kept]
    fake_inspect = lambda texts: [([{"info_type": "INDIA_PAN_INDIVIDUAL"}] if "AAAPZ1234C" in t else []) for t in texts]
    kept, n_pii = redact(kept, fake_inspect)
    assert n_pii == 1 and len(kept) == 4 and all("AAAPZ" not in k["answer"] for k in kept)
    with tempfile.TemporaryDirectory() as tmp:
        m = write(kept, tmp, "selftest", "acme", dropped, n_pii, today=dt.date(2026, 9, 9))
        assert m["rows"] == 4 and m["refusals"] == 1 and m["dropped_golden_ids"] == ["jn-06", "lk-06"] and m["dropped_pii"] == 1
        assert m["dropped_golden_by_rule"] == {"evidence": 1, "question": 1}
        vertex = [json.loads(l) for l in open(os.path.join(tmp, "documind_sft_selftest.vertex.jsonl"), encoding="utf-8")]
        chat = [json.loads(l) for l in open(os.path.join(tmp, "documind_sft_selftest.chat.jsonl"), encoding="utf-8")]
        assert len(vertex) == len(chat) == 4
        for v, c in zip(vertex, chat):
            assert v["contents"][0]["role"] == "user" and v["contents"][1]["role"] == "model"
            assert "[Source 1]" in v["contents"][0]["parts"][0]["text"] and v["contents"][0]["parts"][0]["text"].startswith(SYSTEM)
            draft = ModelDraft.model_validate_json(v["contents"][1]["parts"][0]["text"])
            assert draft.answerable is (len(draft.citations) == 1)
            assert c["messages"][2]["content"] == v["contents"][1]["parts"][0]["text"]
        assert all(len(v["contents"][1]["parts"][0]["text"]) < 900 for v in vertex)
        assert m["files"]["vertex"]["sha256"] != m["files"]["chat"]["sha256"]
    with tempfile.TemporaryDirectory() as tmp:
        p = os.path.join(tmp, "req.jsonl")
        assert write_batch_requests(rows[:2], p) == 2
        lines = [json.dumps({"custom_id": r["chunk_id"], "response": {"candidates": [{"content": {"parts": [{"text": json.dumps(
            {"question": r["question"], "answer": r["answer"], "quote": r["quote"], "unanswerable_question": "x?"})}]}}]}})
            for r in rows[:2]]
        back = collect_batch(lines, {r["chunk_id"]: r for r in rows[:2]}, refusal_every=2)
        assert len(back) == 3 and back[-1]["answerable"] is False
    print("selftest: the evidence rule dropped jn-06's chunk and kept EMEA elsewhere and lk-09's 8, the question rule "
          "dropped lk-06's twin, the PAN row dropped, two formats agree, ModelDraft parses, the batch round trip holds")
    return 0


# ----------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--project", default=os.environ.get("DOCUMIND_PROJECT", ""))
    ap.add_argument("--tenant", default="acme")
    ap.add_argument("--rows", type=int, default=300)
    ap.add_argument("--out", default=OUT_DIR)
    ap.add_argument("--version", default=VERSION)
    ap.add_argument("--upload", default="", help="gs://PROJECT-datasets/sft/ - copy the frozen files there")
    ap.add_argument("--batch", default="", help="gs://PROJECT-datasets/sft/batch/ - submit the pairs as a Batch API job instead of calling inline")
    ap.add_argument("--collect", default="", help="a local predictions.jsonl from the batch job's output, to finish the build")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        return selftest()
    if not a.project:
        print("--project (or DOCUMIND_PROJECT) is required - this is a live tool; --selftest is the offline half", file=sys.stderr)
        return 2
    chunks = sample(load_chunks(a.tenant, a.project), a.rows)
    print(f"  {len(chunks)} chunks sampled from {a.tenant}'s corpus mirrors")
    if a.batch and not a.collect:
        os.makedirs(a.out, exist_ok=True)
        req = os.path.join(a.out, f"batch_requests_{a.version}.jsonl")
        n = write_batch_requests(chunks, req)
        from google.cloud import storage
        bucket, _, prefix = a.batch.removeprefix("gs://").partition("/")
        storage.Client().bucket(bucket).blob(prefix.rstrip("/") + f"/requests_{a.version}.jsonl").upload_from_filename(req)
        src = f"gs://{bucket}/{prefix.rstrip('/')}/requests_{a.version}.jsonl"
        job = submit_batch(a.project, src, a.batch.rstrip("/") + f"/out_{a.version}/", f"documind-trainset-{a.version}")
        print(f"  {n} requests -> {src}\n  batch job {job.name} state {job.state}\n"
              f"  when it completes: gcloud storage cp {a.batch.rstrip('/')}/out_{a.version}/*/predictions.jsonl . && "
              f"python evals/make_trainset.py --project {a.project} --collect predictions.jsonl")
        return 0
    if a.collect:
        rows = collect_batch(open(a.collect, encoding="utf-8").read().splitlines(), {c["chunk_id"]: c for c in chunks})
    else:
        rows = ask_pairs(a.project, chunks)
    golden = [json.loads(l) for l in open(GOLDEN, encoding="utf-8") if l.strip()]
    rows, dropped_golden = exclude_golden(rows, golden)
    rows, dropped_pii = redact(rows)
    m = write(rows, a.out, a.version, a.tenant, dropped_golden, dropped_pii)
    print(f"  {m['rows']} rows ({m['refusals']} refusals) from {len(m['documents'])} documents; dropped "
          f"{m['dropped_golden_overlap']} for golden overlap {m['dropped_golden_ids']} and {m['dropped_pii']} by the PII scan")
    print(f"  wrote {a.out}/documind_sft_{a.version}.{{vertex,chat}}.jsonl + .manifest.json (sha {m['files']['vertex']['sha256'][:12]})")
    if a.upload:
        upload(a.out, a.version, a.upload)
    print("  review the rows before you commit them: a generated question inherits the generator's blind spots (4.7)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
