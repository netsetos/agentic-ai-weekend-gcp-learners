# DocuMind AI — deploy kit & live-session dry run

A repeatable way to **validate** (and, on GCP, **stand up**) the whole DocuMind AI
stack that Module 12 builds — so live-session day isn't a coin flip.

> **The notebooks are the single source of truth.** Every file under
> `terraform/` and `services/` is *extracted* from the eight Module 12 notebooks
> by `extract_documind.py`. Don't hand-edit the extracted tree — fix the
> notebook heredoc and re-extract (`python tools/readopt.py <notebook> VAR <file>`
> pushes a file edited on disk back into its heredoc). CI checks the two never drift.

---

## Two tiers

| | Tier A — offline dry run | Tier B — live rehearsal |
|---|---|---|
| **Runs** | anywhere (laptop, CI) | your GCP, a throwaway project |
| **Costs** | nothing | a few ₹ for a short-lived project |
| **Needs GCP?** | no | yes (gcloud + terraform + creds) |
| **Catches** | typos, bad refs, missing modules/deps, broken Dockerfiles, invalid Terraform | the other 20% — IAM, quotas, networking, real Gemini/RAG behaviour |
| **When** | every push (CI gate) | the day before you teach |

The golden rule: **Tier A must be green before you bother with Tier B.**

---

## Tier 0 — the ₹0 lane (`make chat-local`)

The chat service, unchanged, on your laptop: `DOCUMIND_PROFILE=local` swaps Gemini for Ollama
`gemma3:4b` and rag-api for a Chroma directory (`shared/profile.py`, lesson 6.4 step 7). No
project, no IAP, no roster — `LOCAL_USER` / `LOCAL_TENANT` stand in, and
`shared/local_corpus.py` seeds DocuMind's corpus — the same chunks and ids 2.3 and 4.2 put in
Firestore, thirteen real documents included — into the local Chroma store. The lane ranks
lexically (BM25 over every chunk of the tenant), because the default embedding is a
deterministic fake; pass a real local embedding to `build_store()` for semantic ranking.

The corpus — the three tenants' synthetic documents and thirteen **real** documents (twelve Acts
and Codes of Parliament and the ministry's compliance handbook) —
lives in `evals/corpus/` and is described in `evals/README.md`. `evals/fetch_real.py` is the one
step that touches the network; `shared/documind_corpus.py` is the one loader and chunker, the
same code 2.3 and Module 4's notebooks paste and `tools/check_real_corpus.py` gates.

```bash
pip install -r services/chat/requirements.txt -r services/chat/requirements-local.txt
ollama pull gemma3:4b
cd deploy && make chat-local          # seeds the corpus, serves on http://127.0.0.1:8081
curl -s localhost:8081/v1/chat -H 'content-type: application/json' -d '{"question":"What is the notice period?"}'
```

The same `retrieve()` answers in both lanes (`tools/check_one_retrieval.py`), so a tool loop
that is wrong here is wrong in production too — that is the point of the lane.

---

## Tier A — offline (`make dryrun`)

```bash
python deploy/extract_documind.py      # regenerate the tree from the notebooks
python deploy/validate.py              # run all offline checks
# or, with make:
cd deploy && make dryrun
```

`validate.py` runs ten checks (`PASS` / `WARN` / `FAIL` / `SKIP`):

| Check | What it proves |
|---|---|
| `extract` | the deploy tree matches the notebooks (no drift) |
| `py_compile` | every service + smoke `.py` parses |
| `imports` | no unresolved local imports (a service isn't missing a module) |
| `requirements` | every dependency is version-pinned |
| `pins` | a package two services share is pinned to the same version in both |
| `dockerfile` | every service ships a Dockerfile |
| `copy-paths` | every `COPY` source exists in the context its Dockerfile assumes (no Docker needed) |
| `terraform` | `fmt` + `init -backend=false` + `validate` |
| `tflint` | Terraform lint |
| `docker` | each python-based image builds from its own context (no push); a Dockerfile on a vendor base (vLLM, LiteLLM, Ollama) is parsed and linted with `docker build --check` |

`terraform` / `tflint` / `docker` **SKIP** when the tool isn't installed locally —
CI (Linux) runs them, so you still get full coverage on every push via
[`.github/workflows/documind-dryrun.yml`](../.github/workflows/documind-dryrun.yml).
Exit code is non-zero only on a real `FAIL`. On GitHub Actions every `WARN` / `FAIL`
is also an annotation on the commit and the PR, and the table is the job summary with the
failing tool's own output folded under it - the raw job log of a public repo is admin-only.

---

## Two profiles

`PROFILE=lean` (the default) is the Module 4 lane and nothing else: `documind-api`,
`documind-ui` and the `documind-ingest` worker on Cloud Run, Firestore with its two composite
vector indexes, one Document AI processor, the uploads bucket, the service accounts, secrets,
budget and alerts. Retrieval is Firestore's own vector search (`RETRIEVAL_BACKEND=firestore`)
with the Ranking API rerank; nothing bills by the hour while idle.

`PROFILE=full` adds what Module 12 teaches on top - Vector Search and its endpoint (the ANN
tier, `retrieval_mode=hybrid`), the Spanner Graph trial, the Cloud SQL checkpointer, GKE, the
BigQuery mirror and Dataplex scan, the log sink, Cloud Deploy - and the admin and chat
services. Same Terraform, `count = local.full ? 1 : 0` on the difference (`variables.tf`);
flip the variable and apply again.

Since Modules 7 and 8 joined the lane (September 2026) both profiles also deploy the agent
surfaces: `documind-mcp` (7.2, the lane's tools over MCP with the kit's identity rules),
`documind-chat` (12.8, the four brains of 8.7 behind one `/v1/chat`) and `documind-agent`
(8.4, an A2A peer that knows DocuMind only through the MCP server - its image copies nothing
from `shared/`). On lean the chat service runs with `CHECKPOINT_DSN=memory` and no IAP of its
own: a backend the UI's brain radio and `make smoke-chat` call with ID tokens, and a
conversation that dies with the instance, which the service says in its own log line. Their
smoke tests are `make smoke-mcp`, `make smoke-chat` and `make smoke-agent`. The chat account
sits on the three golden rosters like the UI's (a surface calls the API as itself and forwards
the person's assertion when there is one); the eval gate's outsider is `documind-outsider-sa`,
a fixture account that IAM admits and every roster refuses.

Since Module 9 joined the lane (9 September 2026) the corpus also holds media, and *media is a
document*: `make media` draws the annual report's Figure 3 from its own table, renders the invoice
as a page image and page 30 of the real Payment of Bonus Act (`evals/build_media.py`; `--video`
synthesises the town hall MP4 from its committed script; ffmpeg via apt-get or imageio-ffmpeg), `make ingest-corpus` uploads them
under the tenant's prefix like any PDF, and the worker DESCRIBES an image or a video with Gemini
and indexes the caption or the segments into the same `chunks` collection, with the pixels
DLP-scanned first (`shared/pii.py`; in asia-southeast1, because image inspection is not offered in
Mumbai - the first live figure said so - while the text scan stays in asia-south1). Nothing downstream changes: `/v1/query`, the MCP server's
`retrieve`, the chat brains and the A2A peer return `kind` / `media_url` / `start` / `end` on a
figure or segment citation, and the UI shows the figure inline or the video at its second. The
Studio tab (9.4) generates through `/v1/media/generate` - roster-checked, audited, metered - and
reads answers aloud through Chirp 3 HD; `/v1/media/upload-url` signs a PUT into the uploads
bucket so a 40 MB video is an ingest, not a stored file. `make smoke-media` proves the five legs.

Module 10 (9 September 2026) made three sentences true. *The dataset is a document*: `make trainset`
writes one question and answer per real chunk of the corpus, in the lane's own citation grammar,
PII-scanned, with every golden question excluded, frozen with a manifest under `evals/sft/` and in
the `datasets` bucket - never traffic, never the answer cache (which holds the test set). *The model
is a setting*: `GENERATOR_MODEL` is a name on the global endpoint or a tuned endpoint path on a
regional one (`generator.py` picks the client), `make tune` runs the managed job, and `make candidate`
serves the result as a no-traffic revision the gate and the judge can call - nothing above the API
changes. *The gate is the judge*: `make eval-live API=<candidate>` decides; `make judge` explains
(Vertex AI Evaluation over the same answers, an Experiments run per commit, pairwise against the live
revision, the three brains' trajectories). Two things the image always carried got callers: `make
cache` creates the tenant's explicit cache (4.5's manager; the next usage row shows `cached_tokens`),
and `ROUTING=on` puts `router.py` and the budget breaker on the request path with the month's counter
in Firestore (`budget.py`) and `SPEND_PCT` for a replay.

Module 11 (10 September 2026) added three more. *The backend is a setting*: `MODEL_BACKEND=gateway` on
the API sends the generator's own prompt to the LiteLLM gateway as a chat completion with a JSON
response format and parses the same `ModelDraft`; `GENERATOR_MODEL` then names a route
(`documind-general`, `documind-slm`, `documind-inference`), the usage row's `model_backend` finally means
what it says and its cost comes from the gateway's `x-litellm-response-cost` header, and
`tenant_settings/{tenant}` in Firestore pins one tenant to a backend without a redeploy. *The gateway is
a route*: `make deploy-gateway` runs `services/litellm` on the lean lane with `config.lean.yaml` - no
database, no master key, behind IAM - the PII guardrail with regexes that match, a token proxy that mints
an ID token per call for the GPU backends, and `ROUTER_ENFORCE=0` for shadow mode; `make smoke-gateway`
proves the door, a JSON completion, the cost header, a PAN re-routed and the SLM route. *The GPU is a
bill*: `make deploy-slm` stages 10.5's GGUF and its generated Modelfile (or `SLM_STOCK=gemma3:4b` as a
named stand-in) into an Ollama image on a Cloud Run L4 at min 0 / max 1 with a startup probe on
`/api/tags`, `make smoke-slm` times the cold start, `make candidate MODEL_BACKEND=gateway
GENERATOR_MODEL=documind-slm` puts it behind the API for the gate and the judge, `make compare` runs the
honest comparison through the gateway, and `make slm-off` ends the day at zero. Optional and explicit:
`make build-vllm deploy-vllm` (11.1's engine, a 13 GB image, a Hugging Face token with Gemma access in
`hf-token`) and `make gke-up` / `make gke-down` (11.5's one-hour Autopilot comparison on the lane's VPC).

Three cost controls sit under all of that (10 September, evening). *The ceiling*: `make gpu-cap` writes a
consumer quota override of 1 on the region's L4 quotas (`services/slm/gpu_quota.py` reads the metric names
from the project; `make gpu-quota` shows them, `make gpu-cap-off` removes the overrides) - a project-level
limit under every service's `--max-instances 1`. *The alarm*: `alerts.tf` raises `gpu_left_warm` when
`documind-slm` or `documind-vllm` has had an instance for two hours, to the on-call and to the admins'
e-mail channel (`ALERT_EMAILS`, derived from `ADMIN_EMAILS`). *The switch*: `off.tf` runs the `documind-off`
Cloud Run job at 23:00 IST - the gcloud image, the job's own account with actAs on the runtime accounts - which
floors the GPU services, the gateway and the UI to zero wherever a floor is set and deletes a leftover Autopilot
cluster; `make off` does the same by hand and `make off-now` runs the job.

Module 12 (10 September 2026, night) is the module the kit is extracted from, and its seams close the gap
between what its files ship and what the lane calls. *The guard is a switch*: `ARMOR=on` on the API runs
`guard.py` before retrieval (`screen_prompt`, a block is a 400 with `prompt_blocked`) and on the buffered answer
(`screen_response`; the stream holds its tokens until then), the usage row gains `guard` (`off`, `pass`, or the
block), `lesson-12.2.sh` carries `ARMOR` / `ARMOR_LOCATION` / `ARMOR_TEMPLATE`, `sa.tf` grants the API
`roles/modelarmor.user`, and the sessions' lane stays `ARMOR=off` - `make candidate ARMOR=on` is where the guard
is judged. *The answer cache is a switch too* (12 September 2026, the RAG plan's W4): `SEMANTIC_CACHE=on` on the API
looks 12.6's `answer_cache` up before retrieval with the query's own embedding - per tenant, under the ledger's current
fingerprint (a reindex makes every earlier answer a miss), for the same filters, top_k and prompt version, within `SEMANTIC_CACHE_TTL_H` (a Firestore TTL policy on
`expire_at` reaps) - and serves a hit with its original citations as `model_backend=cache`, cost 0, `cache_hit=semantic`;
`/v1/query` fills it (answerable, cited, not blocked), `/v1/stream` reads it, `smoke.py` asks the golden question twice
when `/version` says it is on, and the lane stays off until `SEMANTIC_CACHE_THRESHOLD` (0.95) is measured on paraphrase
pairs. *The spans are guarded*: `telemetry.py` is imported after the tracer provider inside a try, and a
failed import logs `telemetry_not_instrumented` instead of an outage. *The release is a candidate*:
`make release-candidate GIT_SHA=<sha>` puts the image `make build` pushed on a `--no-traffic --tag candidate`
revision and records its name in `deploy/.candidate-revision`, `make eval-live API=<its URL>` judges it,
`make promote` moves traffic to that revision by name (never to "the latest": two candidates in flight would
promote the wrong one - 12 September 2026), recording the one it moved traffic off in `deploy/.previous-revision`,
and `make rollback` moves it back to exactly that one; `documind-cd.yml` gains a `profile` input whose lean jobs
(`release-lean`, then `promote-lean` behind the `production` environment's reviewers) run exactly those targets
with no Cloud Deploy verb, `wif.tf` pins the branch through `var.deploy_ref` (`DEPLOY_REF`, default
`refs/heads/main`) and lets `sa-documind-cicd` mint the gate's two identities' tokens. *The smokes are one*:
`make smoke-all` runs the seven smokes with their exports, tallies PASS/FAIL and exits non-zero when any smoke failed
(12 September: a FAIL line satisfied the grep and the target exited 0), `smoke.py` refuses the golden
question without a token as its fourth check, and `services/frontend/requirements.txt` no longer pins the two
model clients nothing imports. *The rows are a tool*: `make usage HOURS=` (`evals/usage_rows.py`) groups the
usage rows in Cloud Logging by tenant, model and backend, brain and surface with INR at `USD_INR=85`, and shows
where the time went (p95 per stage: retrieve, rerank, generate, with the pool the reranker saw) - the lean
profile's `tenant_daily`. *Ingestion has live cells*: `make ingest-one FILE= TENANT=` waits for the worker's
`ingest_ok` line, `make poison` puts a zero-byte object in and waits for `ingest_poison`, `make dlq` peeks at
`ingest-dlq-sub` without acking. `tools/check_contract.py` applies the lane rules to 12.1 to 12.8 and
`tools/check_auth_wiring.py` pins every seam above.

The ledger (11 September 2026, night) gives the index a *current*. The worker keeps `sources/{tenant~path}` - which
version of an object path is current, its generation, its declared date - beside the per-version claim in
`documents/`; every chunk carries `doc_key`, `current` and `indexed_at`; a re-issued document's predecessor is
*retired* (`current=false`, `superseded_by`; never deleted) after the new chunks are written, the tenant's cache
record is dropped so the next answer runs uncached, and the same bytes uploaded again *reactivate* a retired
version without re-embedding (`ingest_superseded`, `ingest_reactivated`). A document may declare
`effective_from: YYYY-MM-DD` (or `_effective_YYYY-MM-DD` in its name); the date rides on the chunk, the context
header, the stream's citation event and the UI's sources list (the shared Citation contract that 3.2, 4.2, 4.5 and
4.6 paste verbatim is untouched), and the generator adds one rule only when a packed source carries a date, so
SYSTEM stays the tuning dataset's. The API pre-filters `current == true` when `RETRIEVAL_CURRENT_ONLY=on`
(the second vector index in `firestore_indexes.tf`; `make backfill-current` first on a lane older than the
ledger) and drops retired chunks before the reranker either way. `services/ingest/reconcile.py` is the full
reconciliation - retire what left the bucket, re-ingest what changed by rewriting the object onto itself,
backfill what predates the ledger - as `make reconcile` (`APPLY=1`), `make reconcile-job` (a Cloud Run job on
the ingest image) and `reconcile.tf`'s 23:30 schedule (`RECONCILE_JOB=true`). `make reindex FILE= TENANT=
[NAME=]` re-issues one document and waits for the worker's line; `make retire SOURCE=` flags one by hand;
`evals/demo/` holds the rehearsal's two documents.

Incremental indexing (12 September 2026, [`INDEXING.md`](INDEXING.md)) takes the ledger from document-level to
chunk-level and makes it a system others can copy. The worker chunks a handbook *by section* (the same rule as
`shared/documind_corpus.py`), stamps every row with `chunk_hash`, `locator`, `embedding_model@embedding_version`
and `schema_version`, and *carries over* the previous version's vectors by hash - a one-clause edit of the
283-chunk handbook embeds 2 chunks (`reused=281 embedded=2 retired=283` on the `ingest_ok` line). A new version
lands *staged* and is *swapped* current in one pass while the old rows are retired with `expire_at`; a Firestore
TTL policy (`firestore_indexes.tf`) is the only deleter on the lane, `retention_days` (variables.tf, 30) the
window. An event older than the ledger's generation is acked as `ingest_stale_event`. The reconcile job is
declared in `reconcile.tf` (`RECONCILE_JOB=true` once an image exists; `make reconcile-job` is an apply), ends
on a `drift` number, and `alerts.tf` turns the lifecycle into metrics and three pagers. The API serves
`GET /v1/sources` (the UI's Documents page renders it; `make sources` prints it), keys the cache to the
tenant's corpus fingerprint (`ledger/{tenant}`, `cache_stale`), and `/version` names the embedding. A reindex
is a release: `make reindex` runs the offline gate first, the golden set has a `version` shape (`vr-01`),
`make eval-live SOURCE=` scopes the live gate to one document's rows, and `make smoke-reindex` is in
`smoke-all`. Not yet: `make reembed` (a model migration) and an as-of filter - the strategy's next phase.

Ingestion is the same in both: a Cloud Storage notification on the uploads bucket into the
`documind-ingest` topic, a push subscription with an OIDC token, five attempts, then the DLQ
(`eventarc.tf`). The worker sends PDFs to Document AI in 15-page slices, which is the online
limit, so the corpus's hundred-page Acts go through inline.

---

## Tier B — live rehearsal (the day before)

Run on a **disposable** project so nothing real is touched. Teardown is not total: `make down` deletes the
Cloud Run services, the candidate tag and the context caches (`make down-services`) and then runs
`terraform destroy`, but it cannot remove Firestore (delete protection), a bucket that holds objects
(`force_destroy = false`), a tuned endpoint (`make tune`) or the BigQuery views (`make bq-views`) - it names
them and `gcloud projects delete` is what removes them. On the full profile the audit bucket's retention
policy is LOCKED (`storage.tf`, `is_locked = local.full`), which blocks even the project's deletion for five
years, by design; the lean lab keeps the five-year term without the lock, so its project can go.

```bash
# 0. one-time: a throwaway project + billing + the tf-state bucket
gcloud projects create documind-ai-live-0901 --set-as-default
gcloud billing projects link documind-ai-live-0901 --billing-account=XXXXXX-XXXXXX-XXXXXX
gsutil mb -l asia-south1 -b on gs://documind-tfstate && gsutil versioning set on gs://documind-tfstate

# 1. THE DRY RUN — previews every resource, creates nothing (PROFILE=full for everything)
cd deploy && make plan PROJECT=documind-ai-live-0901 ADMIN_EMAILS=you@example.com

# 2. bring it up: terraform apply, a first cookie-secret version, Cloud Build for every image
#    this profile deploys, the deploy scripts (IAP on the UI, one accessor per ADMIN_EMAILS),
#    the vector indexes READY, ADMIN_EMAILS on TENANT's roster and the UI's service account
#    on the three golden tenants'. Re-run it any time; it is idempotent.
make up PROJECT=documind-ai-live-0901 ADMIN_EMAILS=you@example.com

# 2b. the corpus - thirteen real Acts as PDFs and the synthetic documents, one prefix per
#     tenant - into the uploads bucket; the worker takes it from there (watch its logs)
make ingest-corpus PROJECT=documind-ai-live-0901

# 3. prove it works end-to-end (add DOCUMIND_CHAT_URL + DOCUMIND_CHAT_TOKEN for the
#    "conversation survives a restart" check - see smoke.py's docstring), then the golden set
make smoke DOCUMIND_API_URL=https://documind-api-NUMBER.us-central1.run.app DOCUMIND_PROJECT=documind-ai-live-0901 DOCUMIND_TENANT=acme
make eval-live PROJECT=documind-ai-live-0901

# 3b. Module 5's lane, once a few documents have been ingested: the feature job over the real
#     chunks + the Dataplex quality gate (prints True/False), then eval CANDIDATES from the feed
make features PROJECT=documind-ai-live-0901
make make-evalset PROJECT=documind-ai-live-0901 TENANT=acme      # -> evals/golden_generated.jsonl, review by hand

# 4. rehearse your demo against the live URLs, then TEAR DOWN — no cost bleed. make down deletes the
#    services first (down-services), destroys the Terraform tree, and prints what only the next line removes
make down PROJECT=documind-ai-live-0901
gcloud projects delete documind-ai-live-0901
```

`make up` is idempotent — if the live demo wedges mid-session, re-run it and
you're back in ~5–8 min. The exact `gcloud run deploy` invocations (image, service
account, VPC connector, env vars) are in
[`commands/lesson-12.2.sh`](commands/lesson-12.2.sh) · `12.3.sh` · `12.4.sh`,
extracted verbatim from the notebooks.

### Pre-flight checklist (green before you walk in)
- [ ] Tier A is green (`make dryrun`)
- [ ] `make plan` shows the expected resource count, no errors
- [ ] the three Cloud Run URLs return `/health` 200 (`make smoke`)
- [ ] a sample RAG query returns an answer **with citations**
- [ ] `make eval-live` clears its nine thresholds and its fifteen required rows, isolation at 100%
- [ ] a rostered person signs in through IAP and sees their tenant; a non-member sees the refusal
- [ ] a query log row lands in BigQuery `query_logs`
- [ ] billing budget alert + monitoring alert policies exist
- [ ] you have a **fallback**: a screen-recording of a working run, in case live GCP misbehaves

---

## Known gaps (surfaced by the dry run — fix before live)

The first Tier-A run flagged these real issues in the Module 12 source. They are
**not** kit bugs — they are things that would break the live demo:

1. **Frontend is incomplete (`imports` FAIL).** `services/frontend/chat.py` imports
   `rag` and `citations`, and `app.py` imports `admin_dashboard`, but the **12.4
   notebook never writes `rag.py`, `citations.py`, or `admin_dashboard.py`** — they
   only appear in 12.4's `FILES` listing. As extracted, the frontend container
   won't start. Fix: add those heredocs to the 12.4 notebook (or import
   `admin_dashboard` from the admin service), then re-extract.
2. **One unpinned dependency (`requirements` WARN).** `rag-api/requirements.txt`
   pins everything except `google-cloud-discoveryengine>=0.13.0`. Pin it (`==`) for
   reproducible builds.
3. **Admin service has no Dockerfile (`dockerfile` WARN).** `services/admin/`
   ships `admin_dashboard.py` / `dlp.py` / `audit.py` but no Dockerfile — it must
   deploy via `gcloud run deploy --source` (buildpacks), or add one.

---

## Layout

```
deploy/
├── extract_documind.py   # notebooks -> this tree (static, no code execution)
├── validate.py           # Tier-A offline checks
├── Makefile              # dryrun / plan / up / smoke / down
├── terraform/            # 11 .tf, all generated from the 12.1/12.3 notebooks:
│   │                     #   variables.tf  project_id, region, india_region, env,
│   │                     #                 admin_emails, residency (india | us)
│   │                     #   backend.tf    GCS state; bucket/prefix come from
│   │                     #                 `make plan`'s -backend-config, not hard-coded
│   │                     #   sa.tf         three service accounts (ui / api / admin) + roles
│   │                     #   network.tf    VPC + Serverless VPC Access connector
│   │                     #   registry.tf   Artifact Registry + two cleanup policies
│   │                     #   firestore.tf  Native mode, asia-south1
│   │                     #   storage.tf    uploads / audit / tts-cache buckets
│   │                     #   secrets.tf    five secrets, never env vars
│   │                     #   budget.tf     billing budget + forecast threshold
│   │                     #   sink.tf       log sink -> BigQuery (query AND stream events)
│   │                     #   alerts.tf     alert policies + var.pagerduty_key
│   └── sql/              #   tenant_daily.sql  per-tenant daily rollup view
├── services/
│   ├── rag-api/          # FastAPI backend (12.2) — auth.py does IAP + Firestore membership
│   ├── admin/            # admin dashboard + DLP + audit (12.3)
│   ├── frontend/         # Streamlit app (12.4) — thin client over rag-api /v1/stream
│   └── chat/             # LangChain tool loop (6.4); imports shared/ like the others
├── shared/               # documind_tools.py — THE one retrieve(); see 8.7; documind_corpus.py — the loader the notebooks paste
├── smoke/smoke.py        # live end-to-end smoke test (Tier B); smoke_mcp / smoke_chat / smoke_agent / smoke_media / smoke_reindex
├── evals/                # the corpus (build_corpus.py, fetch_real.py, build_media.py), the golden set, run_eval.py; demo/ the rehearsal's versions
├── INDEXING.md           # the document lifecycle: identities, carry-over, swap, retention, reconcile, the gates
└── commands/             # reference gcloud/terraform blocks from the notebooks
```

Regenerate anytime: `python deploy/extract_documind.py`.

## Who explains what

Every file in this tree is accounted for by a notebook, and [`INDEX.md`](INDEX.md) says how: **A** owned (extracted
verbatim from a heredoc in a Module 12, 7.1, 7.2 or 8.4 notebook), **B** pasted (a block the notebooks carry verbatim:
the corpus loader), **C** used (imported, read, run or excerpted from the clone by a notebook's code), **D** named in
prose only, **E** unreferenced. `python tools/deploy_index.py` regenerates it from the extractor's map and the
notebooks; `--check` runs inside `tools/check_auth_wiring.py`, which also refuses a code file in D or E - a kit file no
lesson shows is a file nobody can explain from the course.
