# Kit artifacts with no lesson behind them

Everything here is real, deployable, and **taught nowhere**. `extract_documind.py` reads the
Module 12 notebooks only, so these files are not regenerated from anything and
`extract --check` will never notice if they drift.

This file exists because that had already happened silently three times before anyone wrote it
down. When the owning lesson is authored, it must **adopt** the file — move it into the
notebook's heredoc and let the extractor own it — not write a second copy beside it.

Last reviewed: 2026-09-05 (twice: adoption, then an adversarial audit of the adoption).

## Lean profile, 2026-09-06

`terraform/gke.tf` and `terraform/spanner.tf` stay unowned and are now gated like the other
full-profile files (`count = local.full ? 1 : 0`, `variables.tf`'s `profile`). The lean
profile's own pieces are all owned: `variables.tf` and `storage.tf` (12.1), `eventarc.tf`,
the worker's `parser.py` / `contracts.py` / `main.py` and the `documind-ingest` deploy
command (12.5), `config.py` / `retriever.py` (12.2), `tenancy.py`'s write side (12.8),
`run_eval.py`'s outsider token (12.7). The `Makefile` is hand-written, as before.

## Adopted since the last review

| File | Adopted by | What adoption changed |
|---|---|---|
| `terraform/wif.tf` | **12.7** | Now a heredoc in 12.7's notebook. Also repaired: the condition pinned only the MUTABLE `assertion.repository`, so a released repo name could be re-registered to satisfy it, and no branch was pinned at all. Now `repository_id` + `repository` + `refs/heads/main`, with the `principalSet` keyed on the immutable id. |
| `terraform/clouddeploy.tf` | **12.7** | Now a heredoc in 12.7's notebook, unchanged in content. It names `cicd` as the RENDER/DEPLOY/VERIFY execution account, which is what surfaced the missing execution roles in `sa.tf`. |
| `.github/workflows/documind-cd.yml` | **12.7** | Now a heredoc in 12.7's notebook — possible only because `dest_for()` grew a `.github/workflows/` destination; before that the heredoc was found, dropped, and `extract --check` stayed green. Still `workflow_dispatch` only, deliberately, and the header shows the `on:` block the application repo uses instead. Gained an `eval-gate` job, a staging-rollout wait, the live eval, and the promote-to-prod step. |
| `terraform/org_policy.tf` | **12.7** | NEW. `iam.disableServiceAccountKeyCreation`, gated behind `enforce_no_sa_keys` (default **false**, because organization policy needs the project to belong to an organization and a learner's throwaway project does not). `wif.tf` removes the *need* for a key; this removes the *ability* to create one. |
| `run-service.yaml` | **12.7** | NEW. The Cloud Run manifest `gcloud deploy releases create --from-run-manifest` renders. Without it the release is never cut: Cloud Deploy needs a skaffold configuration, this kit has none, and `--from-run-manifest` makes gcloud generate one. Deliberately has **no `traffic:` stanza** — Cloud Deploy owns traffic during a canary. `dest_for()` gained a `DEPLOY_ROOT_FILES` destination for it. |
| `evals/run_eval.py` | **12.7** | NEW, and the one file under `evals/` a lesson owns (`dest_for()` gained an `EVAL_FILES` destination for it). Implements the anchor-matching rule `evals/README.md` left open, and is wired into `make eval`, `make dryrun` and `documind-dryrun.yml`. The rest of `evals/` remains hand-written. |
| `cloudbuild.yaml` | **12.7** | NEW. Builds `rag-api` from the **`deploy/` context** (`docker build -f services/rag-api/Dockerfile .`) so the service can import `shared/`; `documind-cd.yml` runs it with `--config=cloudbuild.yaml` from `deploy/`. Routed by `DEPLOY_ROOT_FILES`. |
| `shared/documind_schemas.py` | **12.2** | NEW. The answer contract — `Citation`, `RAGAnswer`, `DraftCitation`, `ModelDraft`, `resolve()` — as a heredoc in 12.2's notebook, routed by `SHARED_FILES`. `services/rag-api/schemas.py` imports it and adds only the transport envelope `RAGResponse(RAGAnswer)`. 3.2, 4.2, 4.5 and 4.6 paste the class text verbatim; `tools/check_contract.py` proves containment. No `from __future__ import annotations` — the text is exec'd as a notebook cell, where a deferred `List` never resolves. |
| `terraform/cloudsql.tf` | **12.8** | NEW (gap G5). The chat checkpointer's Cloud SQL (`db-f1-micro`, zonal, `deletion_protection = false`), a `random_password` that lives only in state and Secret Manager, the `documind-checkpoint-dsn` secret in 8.5's DSN shape, `secretAccessor` + `cloudsql.client` for `documind-chat-sa` (which `sa.tf`, 12.1, now defines — the service had no account). Needs the `random` provider `backend.tf` now declares. |
| `services/chat/brains.py` | **12.8** | NEW (gap G6). Four brains over the one `tools.py` — langchain, langgraph (with a refuse node), adk, direct — `DOCUMIND_BRAIN` default, `brain` per request; every usage row names the brain. `tools/check_auth_wiring.py` proves the LangGraph loop, refuse path and checkpoint offline. |
| 12.8 `DEPLOY` (→ `commands/lesson-12.8.sh`) | **12.8** | NEW. The chat service's deploy command, which never existed: build from the shared `cloudbuild.yaml` (`_DOCKERFILE`), `--add-cloudsql-instances`, `--set-secrets=CHECKPOINT_DSN`, both IAP audiences, the one-off `documind-checkpoint-setup` job (`migrate.py`), then `--iap`. In `make deploy-services`. |
| `terraform/dataplex.tf` + `terraform/sql/chunk_metadata.sql` | **12.3** | NEW (gap G9). Module 5's production lane: the `rag_data` dataset (5.5's own name, so its SQL runs unchanged), four declared tables, the three-rule Dataplex scan on `index_feed`; the SQL is the feature job (`make features`). `services/ingest/indexer.py` streams `chunk_source` rows with the worker's own `pii_flag` when `BQ_CHUNK_TABLE` is set. `evals/make_evalset.py` (unowned, live) writes PII-scanned eval *candidates* — `run_eval.py` never reads them. Also found: `sql/tenant_daily.sql` was applied by nothing; `make bq-views` (in `make up`) now applies it. |
| `services/rag-api/media.py` | **12.2** | NEW (gap G8). 9.4's Media Studio router, which 9.4 wrote *"so 12.x can adopt it"* and nothing did — now with the tenant roster check and an `event=media` usage row in `tenant_daily`'s shape; 9.4's cell 25 is the same text. Needs the `-media` bucket `storage.tf` (12.1) now provisions and the `media.*` actions `shared/audit_log.py` now registers. Also fixed on the way: `services/ingest/main.py` called `_parse`, `_chunk` and `_enqueue_batch` and none existed anywhere — every message would have raised `NameError`. |

`sa.tf` was already owned by 12.1 and is not listed above, but 12.7 changed it: `roles/iam.serviceAccountUser` was granted at **project** scope, which is actAs on every service account in the project including future ones — so with `cloudbuild.builds.editor` the deploy identity could submit a build as `documind-api-sa` and read Secret Manager, Firestore and BigQuery. It is now granted per service account, as a **static-keyed map** (`for_each` over computed `google_service_account.*.name` fails `terraform plan` outright: "The keys of the map or all values in a set of strings must be known values"), covering the four runtime identities **and `cicd` itself** — Cloud Deploy requires actAs on its own execution account. Two missing execution roles were added, `clouddeploy.jobRunner` and `run.developer`; the first repair also added `storage.objectAdmin` and `logging.logWriter`, and **both were removed again** — Google's documented set is three roles, jobRunner already carries the bucket access, and objectAdmin at project scope reaches the customer-document and retention-locked audit buckets. Over-granting inside the lesson about blast radius is the defect that reviews it.

---

## Stood up for the masterclass, ahead of their lessons

The Flagship Plan's P1 rows are *"stand-up/verification only — build hours sit in the Phase 1
roadmap"*. The 12–26 September demos need infrastructure that exists; the lessons that teach it
are authored in November and December.

| File | Owning lesson | Ships in | Notes for whoever adopts it |
|---|---|---|---|
| `terraform/spanner.tf` | **4.6** (built) | v1.1 | Module 4 is outside the extractor's map. Instance `documind-graph`, database `documind`, `regional-asia-south1`, and the DDL is byte-for-byte 4.6's. **Trial expires Fri 4 Dec 2026** — the `pricing` row re-runs 4.6 before then or moves it to the Neo4j Free mirror. |
| `terraform/gke.tf` + `gke/` | **11.5** | v2.0 | Autopilot, GPU requested per pod. `vllm-deployment.yaml` pins `vllm/vllm-openai:v0.28.0`, matching 11.1/11.2. |
| `services/slm/` | **11.4** | v2.0 | `Modelfile` template and stop tokens must be regenerated from the tokenizer, not copied. `compare_backends.py` reads `evals/golden.jsonl` — the comparison only means anything if the questions are identical. |

## Older orphans, predating this file

Nothing points at these and no lesson creates them. A learner following the notebooks alone
never provisions the Firestore database that `firestore_indexes.tf` attaches its vector indexes
to — worth closing when Module 12's infra lesson is next opened.

| File | Should belong to |
|---|---|
| `terraform/firestore.tf` | **12.1** (infra) |
| `terraform/network.tf` | **12.1** |
| `terraform/registry.tf` | **12.1** |

## Shared modules — owned, but by more than one lesson

Not orphans; listed so nobody "tidies" them back into a service.

| File | Why it is shared |
|---|---|
| `shared/documind_tools.py` | the single `retrieve()`. `tools/check_one_retrieval.py` enforces it. |
| `shared/pii.py` | one India info-type list, used by the ingest worker **and** the admin dashboard. Two lists that drift is the worst kind of compliance bug: the scan misses a type, the dashboard reports zero findings, and both look correct. |
| `shared/audit_log.py` | one event shape into one retention-locked bucket, written by ingest and admin. |
| `shared/documind_schemas.py` | the one answer contract (owned by 12.2, see the adoption table). Imported by rag-api; pasted verbatim by 3.2, 4.2, 4.5, 4.6. A second copy that drifts is exactly the M03 gate failure it closed. |
| `shared/profile.py` | 6.4's step-7 cell as a module: `DOCUMIND_PROFILE`, `build_llm()`, `build_store()`. Read by `chat/agent.py` (the model) and by `documind_tools.retrieve()` (the store). **Unowned** — 6.4 is outside the extractor; 6.4's notebook and page name it. Gap G3. |
| `shared/local_corpus.py` | DocuMind's corpus — `evals/corpus/` through `shared/documind_corpus.py`, the same chunks and ids 2.3 and 4.2 put in Firestore, thirteen real documents included — seeded per tenant into the local Chroma store (`make chat-local`, `python -m shared.local_corpus [tenant]`). Unowned, same reason. Until 2026-09-05 it typed the six Module 6–8 passages. |
| `shared/documind_corpus.py` | the one loader and chunker: `find_kit()`, `load_documents()`, `chunk_document()`, `seed()`. Pasted verbatim (its kit block) by 2.3, 4.2, 4.5 and the 4.2/4.5 practice labs — `tools/check_contract.py` holds them to it — and imported by `local_corpus.py` and `tools/check_real_corpus.py`. Unowned. Gap G20. |
| `services/chat/requirements-local.txt` | 6.4's pins for the local lane (`langchain-chroma`, `chromadb`, `langchain-ollama`, `langgraph-checkpoint-sqlite`), installed on top of `requirements.txt` and never in the image. Unowned; `services/chat/tools.py`, `requirements.txt` and `Dockerfile` are unowned too — 12.8's notebook holds `agent.py`, `cloudsql.tf` and the chat `DEPLOY` command. |
| `services/chat/migrate.py` | `PostgresSaver.setup()` once, as a Cloud Run job (`commands/lesson-12.8.sh` step 3) — 8.5's rule that migrations taking exclusive locks never run at startup. Unowned. Gap G5. |

Services that can see `shared/` build from the `deploy/` context: **chat, ingest, admin, rag-api**
(rag-api since 2026-09-05, through `cloudbuild.yaml`). `frontend`, `gemma-vllm` and `litellm`
still build from their own directory — move them to the `deploy/` context before importing
anything from `shared/` there.
