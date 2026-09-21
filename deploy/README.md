# DocuMind AI — deploy kit & live-session dry run

A repeatable way to **validate** (and, on GCP, **stand up**) the DocuMind AI stack.

> **On `prod_agent`, the committed `deploy/` tree is the source to run and maintain.**
> This branch has no course module folders or notebooks. Edit the deployment files
> directly and run the offline checks below. `extract_documind.py` is retained for
> the separate curriculum checkout; without its private `Module */` notebooks it
> performs no extraction, and the extraction freshness check is skipped.

Lesson numbers below record the kit's course history; they do not refer to files
available on this branch.

## Workstation infrastructure recovery and reruns

Run the committed deployment files directly. See [INFRASTRUCTURE.md](INFRASTRUCTURE.md)
for the Git-only update and checked `prepare → plan → check → apply` sequence.
It includes native billing-account discovery, the Acme/Zeta digital-parser fix,
persisted project inputs and preservation of existing GitHub CI trust. Public
source downloads do not require switching CI trust to the learners repository.

`make plan` prepares and checks a saved plan; `make up` now requires that selected,
reviewed plan before it applies infrastructure and continues service setup. Live
plan/up no longer regenerate deployment source. On this branch, validate changes
against the committed deployment files; notebook synchronization belongs to the
separate curriculum checkout.

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
cd deploy                            # from the repository root
pip install -r services/chat/requirements.txt -r services/chat/requirements-local.txt
ollama pull gemma3:4b
make chat-local                      # seeds the corpus, serves on http://127.0.0.1:8081
# In a second terminal while the service is running:
curl -s localhost:8081/v1/chat -H 'content-type: application/json' -d '{"question":"What is the notice period?"}'
```

The same `retrieve()` answers in both lanes (`tools/check_one_retrieval.py`), so a tool loop
that is wrong here is wrong in production too — that is the point of the lane.

---

## Tier A — offline (`make dryrun`)

```bash
# From the repository root:
python deploy/validate.py             # validate the committed deployment tree
python deploy/evals/run_eval.py       # run the offline eval gate
# or, with make:
make -C deploy dryrun
```

`validate.py` runs ten checks (`PASS` / `WARN` / `FAIL` / `SKIP`):

| Check | What it proves |
|---|---|
| `extract` | checks notebook drift in the curriculum checkout; `SKIP` on this branch because no curriculum notebooks are present |
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

## One shape

Until 15 September 2026 the kit had two profiles - `lean`, the Module 4 lane, and `full`, everything
Module 12 teaches on top of it, count-gated in the same Terraform behind `local.full`. The course runs
the full shape only now, so the switch is gone and, after a reviewed `make plan`, `make up` stands up all of it: `documind-ingest`,
`documind-api`, `documind-admin`, `documind-ui`, `documind-chat`, `documind-mcp` and `documind-agent` on
Cloud Run; Firestore with its vector indexes and Vector Search with its endpoint (the ANN tier,
`RETRIEVAL_BACKEND=vector` as the deployment's default, the Firestore rung beneath it); the Spanner
Graph trial; the Cloud SQL checkpointer and the gateway's Cloud SQL; the Autopilot cluster; the BigQuery
mirror, the log sink, the daily view and the Dataplex scan; Cloud Deploy; the managed stores of 4.3 and
4.4 (`MANAGED_MIRROR=both`, `MANAGED_SEARCH=true`: `make up` switches RAG Engine to serverless mode,
creates a corpus for every `any` tenant and pins acme to `rag_engine` and zeta to `vertex_search` -
`make managed-stores`, after the roster - and `make down` deletes the corpora that would bill storage);
one Document AI processor, the buckets, the accounts, secrets, budget and alerts. `make tenant-backend
TENANT= RETRIEVAL_BACKEND=` moves one tenant by hand. It bills while it exists - the Vector Search
endpoint, two Cloud SQL instances and the cluster by the hour - which is what `make off` (the night
switch), `make down` and the throwaway project are for.

Two things the profile used to switch that are not resource counts are switches of their own, both
false for a lab (`variables.tf`): `AUDIT_LOCK=true` locks the audit bucket's five-year retention
(irreversible; it liens the project), and `GEMINI_QUOTA_OVERRIDE=true` applies `quota.tf`'s consumer
quota override, whose metric names are unverified (an unknown one fails the apply). The shape as a
whole has not yet been applied to a live project: the lean lane ran live from 6 September, and the
first `make up` of the one shape is the first live test of the rest.

**The ANN tier starts empty, on purpose (16 September 2026).** `vector.tf` names no `contents_delta_uri`:
that field is the *batch* ingest input, read once at create time, and the kit has no batch writer - the
worker's `upsert_datapoints` is the only way in. Naming a folder of a bucket the same apply creates meant
an empty folder, and CreateIndex refuses one; the first from-blank apply lost its index that way. So
`make up` ends with `make vector-status` - the index's own `vectorsCount`, 0 until `make ingest-corpus`
has run - and `make wait-vectors WANT=200` blocks until the corpus is in. Every answer says which rung
served it: `stages.retrieval_backend` is the backend chosen for that request, `stages.vector_chunks` the
index's share of the pool, and each citation's chunk carries `found_by: vector | firestore`. `make smoke`
fails when a deployment on `vector` answered from the Firestore rung. If the tier is empty and Firestore
is not - a worker deployed before the index existed - `make backfill-vectors APPLY=1` streams every
current row's stored embedding up, no model call.

**Spanner is read now (16 September 2026).** `spanner.tf` carries 4.6's DDL at last - `GraphEdge` interleaved in
its node `ON DELETE CASCADE`, `updated_at`, the edge label `RELATES_TO` the walk matches on - plus an `embedding`
column the lesson does not have. `GRAPH_BACKEND=spanner` on the API (`make candidate GRAPH_BACKEND=spanner
RETRIEVAL_GRAPH=auto`) walks `DocuMindGraph` with `shared/documind_graph.SpannerGraph`, and seeds it **by meaning**:
the question's embedding against the node names' (`COSINE_DISTANCE`, exact; `GRAPH_SEED_K`, `GRAPH_SEED_DISTANCE`),
so "who signs off on a big purchase?" reaches the CFO without the word CFO. `make graph GRAPH_BACKEND=spanner`
builds it with each name's vector. The instance is provisioned Enterprise, 100 PU, hourly - the comment that called
it a free trial is gone; 4.6's cell 5 shows the `--instance-type free-instance` route if a cohort wants ₹0.

Since Modules 7 and 8 joined the lane (September 2026) `make up` also deploys the agent
surfaces: `documind-mcp` (7.2, the lane's tools over MCP with the kit's identity rules),
`documind-chat` (12.8, the four brains of 8.7 behind one `/v1/chat`) and `documind-agent`
(8.4, an A2A peer that knows DocuMind only through the MCP server - its image copies nothing
from `shared/`). The chat service runs on the Cloud SQL checkpointer behind IAP, and stays a
backend the UI's brain radio and `make smoke-chat` call with ID tokens (the bearer leg); a
laptop can still set `CHECKPOINT_DSN=memory`, a conversation that dies with the instance, which
the service says in its own log line. Their smoke tests are `make smoke-mcp`, `make smoke-chat` and `make smoke-agent`. The chat account
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
a route*: `make deploy-gateway` runs `services/litellm` with its one `config.yaml` (15 September 2026) -
Postgres for the spend logs and tag budgets, no master key, behind IAM - the PII guardrail with regexes that match, a token proxy that mints
an ID token per call for the GPU backends, and `ROUTER_ENFORCE=0` for shadow mode; `make smoke-gateway`
proves the door, a JSON completion, the cost header, a PAN re-routed and the SLM route. *The GPU is a
bill*: `make deploy-slm` stages 10.5's GGUF and its generated Modelfile (or `SLM_STOCK=gemma3:4b` as a
named stand-in) into an Ollama image on a Cloud Run L4 at min 0 / max 1 with a startup probe on
`/api/tags`, `make smoke-slm` times the cold start, `make candidate MODEL_BACKEND=gateway
GENERATOR_MODEL=documind-slm` puts it behind the API for the gate and the judge, `make compare` runs the
honest comparison through the gateway, and `make slm-off` ends the day at zero. Optional and explicit:
`make build-vllm deploy-vllm` (11.1's engine, a 13 GB image, a Hugging Face token with Gemma access in
`hf-token`) and `make gke-up` / `make gke-down` (11.5's Autopilot GPU comparison on the lane's VPC).

**GKE disk-quota configuration (17 September 2026).** Module 4 ingestion, retrieval, generation,
and the UI use Cloud Run; the GKE GPU workload is only for the optional Module 11.5 lesson.
Terraform now defaults to `gke_autopilot=false`: a regional Standard control plane with ONE
`e2-standard-2` CPU node in `REGION-a`, with a 30 GB `pd-standard` boot disk. The temporary
bootstrap pool uses the same small standard disk before removal. The final pool has no
autoscaling or upgrade surge; upgrades can interrupt this single-node lab. Standard disks
consume `DISKS_TOTAL_GB`, not `SSD_TOTAL_GB`. Standard disk, CPU and IP quota must still be
available, and temporary old-resource cleanup can overlap provisioning.

The historical name `documind-autopilot` and Terraform address are retained for compatibility;
inspect `terraform output -raw gke_mode` to see the selected mode. Applying this change to
an existing Autopilot cluster REPLACES it and removes its GKE workloads. The seven Cloud Run
services are independent of that cluster. Inspect a fresh full plan and account for any
GKE workloads or volumes before accepting the replacement. Do not reuse an earlier plan.

The Standard CPU lab cannot schedule the L4 manifest. `make gke-up` checks the actual
cluster mode and explains this instead of leaving a GPU pod Pending. For Module 11.5,
set `gke_autopilot=true` in your Terraform inputs, obtain sufficient SSD/GPU quota,
review another cluster replacement, and apply before running the GPU target.
The regional control-plane fee and the Standard CPU node continue while provisioned.

References: [GKE boot disks](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/custom-boot-disks),
[regional clusters with a single-zone pool](https://docs.cloud.google.com/kubernetes-engine/docs/how-to/creating-a-regional-cluster),
[Compute disk quotas](https://docs.cloud.google.com/compute/resource-usage).

Three cost controls sit under all of that (10 September, evening). *The ceiling*: `make gpu-cap` writes a
consumer quota override of 1 on the region's L4 quotas (`services/slm/gpu_quota.py` reads the metric names
from the project; `make gpu-quota` shows them, `make gpu-cap-off` removes the overrides) - a project-level
limit under every service's `--max-instances 1`. *The alarm*: `alerts.tf` raises `gpu_left_warm` when
`documind-slm` or `documind-vllm` has had an instance for two hours, to the on-call and to the admins'
e-mail channel (`ALERT_EMAILS`, derived from `ADMIN_EMAILS`). *The switch*: `off.tf` runs the `documind-off`
Cloud Run job at 23:00 IST - the gcloud image, the job's own account with actAs on the runtime accounts - which
floors the GPU services, the gateway and the UI to zero wherever a floor is set. It does not remove
Terraform's GKE cluster or Standard CPU node; `make down` tears those down. `make off` scales the
services by hand and `make off-now` runs the job.

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
and `make rollback` moves it back to exactly that one; `documind-cd.yml`'s `path` input picks the candidate jobs
(`release-candidate`, then `promote` behind the `production` environment's reviewers), which run exactly those
targets with no Cloud Deploy verb, or `release-clouddeploy` (staging, the live gate, a person, a canary), `wif.tf` pins the branch through `var.deploy_ref` (`DEPLOY_REF`, default
`refs/heads/main`) and lets `sa-documind-cicd` mint the gate's two identities' tokens. *The smokes are one*:
`make smoke-all` runs the seven smokes with their exports, tallies PASS/FAIL and exits non-zero when any smoke failed
(12 September: a FAIL line satisfied the grep and the target exited 0), `smoke.py` refuses the golden
question without a token as its fourth check, and `services/frontend/requirements.txt` no longer pins the two
model clients nothing imports. *The rows are a tool*: `make usage HOURS=` (`evals/usage_rows.py`) groups the
usage rows in Cloud Logging by tenant, model and backend, brain and surface with INR at `USD_INR=85`, and shows
where the time went (p95 per stage: retrieve, rerank, generate, with the pool the reranker saw) - the same
figures `tenant_daily` holds, from the rows before the sink has copied them. *Ingestion has live cells*: `make ingest-one FILE= TENANT=` waits for the worker's
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
limit, so the corpus's hundred-page Acts go through inline. Anything over `MAX_INLINE_PAGES` (250) is queued
for the batch lane's consumer (13 September 2026): `documind-ingest-batch`, a Cloud Run job on the ingest image
(`batch.tf`, `make batch-job`) that runs the worker's own `index_document()` with no request deadline, started
by the worker as it queues and hourly regardless (`make batch` runs it now, `make queued` lists the queue).
And 4.6's graph is on the API: `make graph TENANT=` builds it with the lesson's `FirestoreGraph`
(`shared/documind_graph.py`, the notebook's text verbatim, gated), and `RETRIEVAL_GRAPH=on|auto` walks it in
front of the dense pool - off on the lane, `make candidate RETRIEVAL_GRAPH=auto` first. The managed stores of
4.3 and 4.4 are mirrors of the ledger (P9, `course-bibles/managed-retrieval-plan-2026-09-13.md`):
`MANAGED_MIRROR=rag_engine|vertex_search|both` copies every version the worker swaps current, as the text its rows
hold, into the tenant's RAG Engine corpus (`make rag-engine-enable` once, `make rag-corpus TENANT=` per tenant)
and / or Vertex AI Search data store (`managed.tf`, `MANAGED_SEARCH=true`), and takes a retired version out;
`make managed-status` reads every store against the ledger. On by default (*One shape*, above). Which *tenants* are
mirrored is each
tenant's `data_region` policy (13 September 2026, evening: `tenant_settings/{tenant}`, `make tenant-policy TENANT=
DATA_REGION=in|any`, `shared/tenancy.py`; absent is `in`): `make roster` sets `acme` and `zeta` to `any` and
`globex` to `in`, so one deployment shows both - a forbidden store is skipped once per tenant and said so, a
delete is never refused, every copy in or out is a `doc.mirror` audit event, and `GET /v1/sources` says where
each version is held (`mirrored`).
The API reads the corpus as `RETRIEVAL_BACKEND=rag_engine` (P9.4): the tenant's corpus queried by text, each
context mapped to the kit's chunk contract through its version's row, the same reranker, packing and citations,
figures and segments still from the kit's index, and the Firestore rung with the filters when a tenant has no
corpus or the store will not answer (`rag_engine_fallback`). `RETRIEVAL_BACKEND=vertex_search` (R4, 13 September
2026 evening) reads 4.4's data store the same way: the tenant's store searched by text through its default serving
config as the lesson's notebook does, each extractive segment - or the snippet, when the store serves none - a chunk
of the contract through its version's row, the caller's `doc_type` as a filter expression on the store's structData,
media from the kit's index, the Firestore rung on no store or an error (`vertex_search_fallback`). `RETRIEVAL_BACKEND`
is the deployment's default: a
tenant's `tenant_settings.retrieval_backend` pins its store, and its `data_region` decides whether a managed store
may serve it at all - a managed backend for an `in` tenant is the kit's own index with `policy_fallback=1` on the
usage row (never the store, never a 500), and the row's `retrieval_backend` is the one that served. `make ablate
ABLATE_ARGS="--arms all"` measures it
from outside the API; `make candidate RETRIEVAL_BACKEND=rag_engine` judges it. The data store's backend is next.

---

## Tier B — live rehearsal (the day before)

Run on a **disposable** project so nothing real is touched. Teardown is not total: `make down` deletes the
Cloud Run services, the candidate tag and the context caches (`make down-services`) and then runs
`terraform destroy`, but it cannot remove Firestore (delete protection), a bucket that holds objects
(`force_destroy = false`), protected managed search stores (`prevent_destroy = true`), a tuned endpoint (`make tune`) or the BigQuery views (`make bq-views`) - it names
them and `gcloud projects delete` is what removes them. With `AUDIT_LOCK=true` the audit bucket's retention
policy is LOCKED (`storage.tf`, `is_locked = var.audit_lock`), which blocks even the project's deletion for five
years, by design; a lab keeps the five-year term without the lock, so its project can go.

```bash
# 0. one-time: a throwaway project + billing + the tf-state bucket
gcloud projects create documind-ai-live-0901 --set-as-default
gcloud billing projects link documind-ai-live-0901 --billing-account=XXXXXX-XXXXXX-XXXXXX
gsutil mb -l asia-south1 -b on gs://documind-tfstate && gsutil versioning set on gs://documind-tfstate

# 1. Initialize the chosen state once, select the new CI identity, then plan.
#    For existing resources use their original bucket, prefix and workspace.
cd deploy
terraform -chdir=terraform init -input=false \
  -backend-config="bucket=documind-tfstate" \
  -backend-config="prefix=documind/documind-ai-live-0901"
python commands/infrastructure.py prepare --project documind-ai-live-0901 --region us-central1 \
  --github-repository netsetos/agentic-ai-weekend-gcp \
  --github-repository-id 1358872052 --deploy-ref refs/heads/main
make plan PROJECT=documind-ai-live-0901 ADMIN_EMAILS=you@example.com

# 2. bring it up: terraform apply, a first cookie-secret version, Cloud Build for every image
#    make up deploys, the deploy scripts (IAP on the UI, one accessor per ADMIN_EMAILS),
#    the vector indexes READY, ADMIN_EMAILS on TENANT's roster and the UI's service account
#    on the three golden tenants'. Review the selected plan first; create a new plan after a partial apply.
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

`make up` applies the selected checked plan. After a partial apply or a state change,
run `make plan`, review the new plan, then run `make up` again. Deployment time depends
on live provisioning, image builds and service readiness. The exact `gcloud run deploy` invocations (image, service
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

Regeneration with `python deploy/extract_documind.py` is available only in the
separate curriculum checkout. On `prod_agent`, maintain the committed files directly.

## Who explains what

[`INDEX.md`](INDEX.md) records the kit's curriculum provenance: **A** owned (extracted
verbatim from a heredoc in a Module 12, 7.1, 7.2 or 8.4 notebook), **B** pasted (a block the notebooks carry verbatim:
the corpus loader), **C** used (imported, read, run or excerpted from the clone by a notebook's code), **D** named in
prose only, **E** unreferenced. Its generator and curriculum gates live in the
separate authoring repository and are not available on `prod_agent`; this index
describes the inherited kit rather than files required to run this branch.
