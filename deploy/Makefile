# DocuMind AI — deploy kit
#
# Tier A (offline, no GCP, free) — run anywhere, runs in CI:
#     make dryrun          extract check + full offline validation
#     make extract         (re)generate the deploy tree from the notebooks
#     make validate        run the Tier-A checks only
#
# Tier B (live, needs gcloud + terraform + a throwaway project — YOU run these on GCP):
#     make plan  PROJECT=documind-ai-live-0910     prepare verified inputs and save a checked plan
#     make up    PROJECT=...                        apply that reviewed plan, then build/deploy and seed
#     Initialize the intended Terraform backend first; plan/up never reconfigure it.
#     make tenant-policy TENANT= DATA_REGION=in|any   where a tenant's text may be held (13 September 2026): in = the kit's rows only; any = the managed stores too
#     make tenant-backend TENANT= RETRIEVAL_BACKEND=  which store answers one tenant (vector | firestore | rag_engine | vertex_search | default); make up pins acme and zeta
#     make media [MEDIA_ARGS=--video]              the corpus's figures (and the town hall video) under evals/corpus/
#     make ingest-corpus PROJECT=...                the corpus into the uploads bucket, one prefix per tenant
#     make smoke DOCUMIND_API_URL=https://...       end-to-end smoke test
#     make trainset / cache / judge / tune / candidate   Module 10: the dataset, the tenant cache, the second judge,
#                                                   the managed tuning job, a no-traffic revision for the A/B
#     make eval-live PROJECT=... [SOURCE=<doc>]     the golden set against the deployed API (scoped to one document's rows)
#     make reindex FILE= NAME= / sources / purge     the document lifecycle (deploy/INDEXING.md); smoke-reindex smokes it
#     make retire / restore SOURCE=                 withdraw a document (a tombstone the nightly walk honours), bring it back
#     make batch / queued / batch-job            the batch lane (13 September 2026): drain the queue now, list it, declare the job
#     make graph TENANT=acme                     build the tenant's knowledge graph (4.6) for RETRIEVAL_GRAPH=on|auto
#     make managed-status / rag-corpus TENANT=   the managed mirror (P9, 4.3 + 4.4): the stores against the ledger; a tenant's RAG Engine corpus
#     make candidate RETRIEVAL_BACKEND=rag_engine  4.3's corpus as the API's retrieval stage (P9.4), judged on a candidate; make ablate ABLATE_ARGS="--arms all" first
#     make rag-engine-enable                     once per project: RAG Engine in serverless mode + vectorsearch (4.3's one-time prep)
#     make down  PROJECT=...                        the services, then terraform destroy; names what only project deletion removes
#
# ONE SHAPE (15 September 2026; the lean | full switch is gone): seven Cloud Run services - the ingest worker, the
# API, the admin console, the UI, the chat service on its Cloud SQL checkpointer, the MCP server and the A2A peer -
# Firestore with its vector indexes, Vector Search and its endpoint, Spanner Graph, the GKE lab cluster,
# the BigQuery mirror and Dataplex scan, the log sink, Cloud Deploy, the managed stores of 4.3 and 4.4, one Document
# AI processor, the buckets, the accounts, secrets, budget and alerts. See README.md.
#
# On Windows without `make`, run the python commands directly, e.g.
#     python deploy/validate.py
#     python commands/lane.py sources          (the lane's records: mk/*.mk targets as direct Python calls)
#
# Tier B targets require terraform + gcloud and a real PROJECT; they intentionally
# refuse to run without it. See README.md for the full live-day runbook.

PY      ?= python
# Every python the kit runs prints as it goes: under nohup or a pipe, python buffers stdout and a
# twenty-minute make trainset showed nothing until it exited (the Module 10 phase, 10 September).
export PYTHONUNBUFFERED = 1
TF_DIR   = terraform
# Optional saved-plan path for up. Empty uses infrastructure.py's recorded plan.
PLANFILE ?=

# REGION is where the SERVICES run - Cloud Run, the VPC connector, Artifact Registry - and
# the deploy scripts under commands/ build their image paths and connector paths from it.
# Data at rest (Firestore, the buckets, Model Armor) follows variables.tf's india_region.
#
# No comment on the same line as a value: Make keeps the blanks before a `#` as part of the
# value, so a `?=` with a trailing comment once made the services list name a variable that did
# not exist, and `make up` built and deployed nothing without a word - the first live run.
REGION        ?= us-central1
# us: Layout Parser in the US | india: Enterprise OCR in Mumbai
RESIDENCY     ?= us
# The two production knobs that replaced the profile (variables.tf, 15 September 2026): lock the audit bucket's
# five-year retention (irreversible; it liens the project) and apply quota.tf's Gemini override (its metric names
# are unverified; an unknown one fails the apply). Both false for a lab, both true on purpose.
AUDIT_LOCK            ?= false
GEMINI_QUOTA_OVERRIDE ?= false
# the UI's floor: 1 on a session day, 0 after
MIN_INSTANCES ?= 0
# comma-separated: IAP accessors, the admin allowlist
ADMIN_EMAILS  ?= admin@documind.example.com
ADMIN_DOMAINS ?=
# the alert e-mail channel (alerts.tf): the admins, unless ADMIN_EMAILS is still the placeholder
comma := ,
ALERT_EMAILS  ?= $(if $(filter admin@documind.example.com,$(ADMIN_EMAILS)),,$(ADMIN_EMAILS))
ALERT_EMAILS_JSON = $(if $(ALERT_EMAILS),["$(subst $(comma),"$(comma)",$(ALERT_EMAILS))"],[])
TENANT        ?= acme
# comma-separated; `make roster` puts them on TENANT's roster
MEMBERS       ?= $(ADMIN_EMAILS)
GIT_SHA       ?= $(shell git rev-parse --short HEAD 2>/dev/null || echo dev)
# Module 10: the API's model is a setting. GENERATOR_MODEL is a name (global) or a tuned endpoint path
# (regional); RAG_MODEL_BASE prices an endpoint at its base; ROUTING=on puts router.py and the budget
# breaker on the request path; BUDGET_USD is the month's cap the breaker reads; SPEND_PCT overrides
# the reading for a replay. All of them reach 12.2's DEPLOY block through deploy-services.
GENERATOR_MODEL ?= gemini-3.6-flash
RAG_MODEL_BASE  ?= gemini-3.6-flash
# empty: the endpoint path's own /locations/<x>/; set to force one (make candidate only)
GENERATOR_LOCATION ?=
ROUTING         ?= off
BUDGET_USD      ?= 100
SPEND_PCT       ?=
TUNE_BASE       ?= gemini-3.1-flash-lite
TUNE_EPOCHS     ?= 3
TUNE_ADAPTER    ?= 4
# Module 11: THE BACKEND IS A SETTING. vertex = google.genai (the lane); gateway = 11.3's LiteLLM gateway, where
# GENERATOR_MODEL names a route (documind-slm is 11.4's self-hosted model). SLM_STOCK names a stock model to serve as the
# stand-in until 10.5's GGUF is in the bucket.
MODEL_BACKEND   ?= vertex
SLM_STOCK       ?=
ROUTER_ENFORCE  ?= 1
# Module 12: the guard is a switch (12.6). CI trust is saved by infrastructure.py
# from existing state, or explicitly configured for a new project; no branch default here.
ARMOR           ?= off
SEMANTIC_CACHE  ?= off
# 12.5's ledger (11 September 2026): the API retrieves only chunks the ledger marks current - after the second
# vector index is built and make backfill-current has run; RECONCILE_JOB=true schedules the nightly reconcile
RETRIEVAL_CURRENT_ONLY ?= off
RECONCILE_JOB   ?= false
# 13 September 2026: the batch lane's consumer (batch.tf, on the ingest image RECONCILE_IMAGE names) and 4.6's graph on the
# API (shared/documind_graph.py; make graph builds it): both off until asked for, the graph judged on a candidate first
BATCH_JOB       ?= false
RETRIEVAL_GRAPH ?= off
# Where the graph lives (16 September 2026): firestore (beside the chunks, seeded by a name the question contains) or
# spanner (spanner.tf's DocuMindGraph, seeded BY MEANING - the question's embedding against the node names').
# make graph GRAPH_BACKEND=spanner builds it there; make candidate GRAPH_BACKEND=spanner RETRIEVAL_GRAPH=auto judges it.
GRAPH_BACKEND    ?= firestore
SPANNER_INSTANCE ?= documind-graph
SPANNER_DATABASE ?= documind
# P9 (13 September 2026, managed-retrieval-plan-2026-09-13.md): the managed mirror - the ledger's current versions copied
# into a RAG Engine corpus (4.3) and / or a Vertex AI Search data store (4.4) per tenant, for the rag_engine and
# vertex_search backends. MANAGED_MIRROR is the worker's, the batch job's and the walk's switch (off | rag_engine |
# vertex_search | both) - the deployment's capability list; which TENANTS may be mirrored is each tenant's data_region
# (make tenant-policy, 13 September 2026 evening). MANAGED_SEARCH=true declares the data stores (managed.tf);
# RAG_LOCATION is the corpora's region (serverless: us-central1 only). Both ON (13 September 2026, evening; the one
# shape since 15 September): both stores, the corpora created by make up (managed-stores), acme pinned to rag_engine
# and zeta to vertex_search - every managed piece the course teaches runs, and a candidate is still how a backend is
# judged before it becomes a deployment's default.
MANAGED_MIRROR  ?= both
MANAGED_SEARCH  ?= true
# the tenants whose data_region is `any` (make roster): a RAG Engine corpus each, the pins,
# and managed.tf's `tenants` default (the data stores declared) names the same two - globex's text stays home
MANAGED_TENANTS ?= acme zeta
RAG_LOCATION    ?= us-central1
# 12 September 2026 (deploy/INDEXING.md): the retention window a retired chunk row lives for (the TTL policy's
# number, variables.tf), the ONE declared embedding both the worker and the API run (variables.tf), and the ingest
# image the nightly job runs (reconcile.tf declares the job; make reconcile-job is an apply with the switch on).
RETENTION_DAYS    ?= 30
EMBEDDING_MODEL   ?= text-embedding-005
EMBEDDING_VERSION ?= 1
RECONCILE_IMAGE   ?= $(IMAGE_REPO)/ingest:$(GIT_SHA)

# Billing and GitHub trust are project-specific inputs, never placeholders.
# infrastructure.py prepare obtains the linked billing account and preserves the
# existing state's CI identity. For a new project, configure its explicit GitHub
# identity with the helper before make plan (see README.md). All direct Terraform
# calls load this file LAST so shell defaults cannot replace these critical values.
TF_PROJECT_VARS = $(abspath $(TF_DIR)/runbook-project.auto.tfvars.json)
# In the billing account's currency (an Indian account is INR).
BUDGET_AMOUNT      ?= 5000
PAGERDUTY_KEY      ?= unset-in-dryrun
# Explicit legacy import, teardown, and feature-job targets use this init recipe.
# The safe plan/up path requires an initialized backend and never switches its bucket.
TFSTATE_BUCKET     ?= documind-tfstate
TFSTATE_PREFIX     ?= documind/$(PROJECT)

TF_EXTRA_VARS = -var audit_lock=$(AUDIT_LOCK) \
                -var gemini_quota_override=$(GEMINI_QUOTA_OVERRIDE) \
                -var residency=$(RESIDENCY) \
                -var budget_amount=$(BUDGET_AMOUNT) \
                -var pagerduty_key=$(PAGERDUTY_KEY) \
                -var 'alert_emails=$(ALERT_EMAILS_JSON)' \
                -var reconcile_job=$(RECONCILE_JOB) \
                -var batch_job=$(BATCH_JOB) \
                -var managed_search=$(MANAGED_SEARCH) \
                -var managed_mirror=$(MANAGED_MIRROR) \
                -var reconcile_image=$(RECONCILE_IMAGE) \
                -var retention_days=$(RETENTION_DAYS) \
                -var embedding_model=$(EMBEDDING_MODEL) \
                -var embedding_version=$(EMBEDDING_VERSION)
TF_VARS = -var project_id=$(PROJECT) -var region=$(REGION) $(TF_EXTRA_VARS) \
          -var-file="$(TF_PROJECT_VARS)"
INFRA = $(PY) commands/infrastructure.py
INFRA_FLAGS = --project "$(PROJECT)" --region "$(REGION)" --terraform-dir "$(TF_DIR)"
INFRA_VARS = $(subst -var ,--var ,$(TF_EXTRA_VARS))
INFRA_PLAN = $(if $(strip $(PLANFILE)),--plan "$(PLANFILE)",)
TF_INIT = terraform init -input=false -reconfigure \
          -backend-config="bucket=$(TFSTATE_BUCKET)" \
          -backend-config="prefix=$(TFSTATE_PREFIX)"

# The services make up builds and deploys, in the order they must come up: the worker first (the push
# subscription already points at its URL), then the API, then the surfaces that call it. The flags live in
# commands/lesson-12.*.sh, extracted from the notebooks, so the thing that runs is the thing the lesson taught
# and there is no second copy to drift. One of them again: make build deploy-services SERVICES=api
# SCRIPTS=commands/lesson-12.2.sh.
IMAGE_REPO = $(REGION)-docker.pkg.dev/$(PROJECT)/documind
SERVICES   = ingest api admin ui chat mcp agent
SCRIPTS    = commands/lesson-12.5.sh commands/lesson-12.2.sh commands/lesson-12.3.sh \
             commands/lesson-12.4.sh commands/lesson-12.8.sh \
             commands/lesson-7.2.sh commands/lesson-8.4.sh

.PHONY: dryrun extract check validate eval eval-live plan up secrets build deploy-services \
        wait-index roster ingest-corpus smoke down guard-project bq-views features make-evalset \
        chat-local deploy-slm slm-off ablate smoke-mcp smoke-chat smoke-agent \
        trainset cache judge tune candidate \
        deploy-gateway gateway-off smoke-gateway smoke-slm build-vllm deploy-vllm vllm-off gke-up gke-down compare \
        gpu-quota gpu-cap gpu-cap-off off off-now \
        release-candidate record-candidate promote rollback smoke-all usage ingest-one poison dlq down-services \
        reindex retire restore reconcile reconcile-job backfill-current sources purge smoke-reindex \
        batch queued batch-job graph managed-status rag-corpus rag-engine-enable tenant-policy tenant-backend \
        managed-stores managed-stores-down vector-status wait-vectors backfill-vectors

# ---------- the module files (22 September 2026): one .mk per lane; the Makefile keeps the variables and the core ----------
# mk/ingestion.mk  the roster and the tenant pins, the vector tier's status and repair, the ingest drills (the v5 course's Module 3)
# mk/lifecycle.mk  the ledger (reindex, retire, restore, reconcile, sources, purge) and the batch lane (Module 4)
# A recipe longer than a few lines is a script under commands/ or a subcommand of commands/lane.py, so the same
# operation runs without make; the .mk target is its one-line entry. tools/check_*.py read the Makefile and
# mk/*.mk as one text. Add a module: one file here, its targets in .PHONY above.
include mk/*.mk

# ---------- Tier A: offline ----------
dryrun: check validate eval

extract:
	$(PY) extract_documind.py

check:
	$(PY) extract_documind.py --check

validate:
	$(PY) validate.py

# The eval gate, offline half: no credentials, no cost, no network. It judges the
# GOLDEN SET, not the model - that every assertion could still fail, that every
# must_retrieve anchor resolves, and that the isolation rows have not been quietly
# deleted. Part of `dryrun`, so it runs on every PR that touches deploy/.
eval:
	$(PY) evals/run_eval.py

# ---------- Tier B: live (guarded) ----------

guard-project:
	@[ -n "$(PROJECT)" ] || (echo "ERROR: set PROJECT=documind-ai-live-<date>"; exit 1)

# Read-only: is everything `make up` needs in place? Tools, sign-in, project, billing, the state
# bucket, the APIs, the one local Python package. Creates nothing; says what to run for a MISS.
preflight: guard-project
	@PROJECT=$(PROJECT) TFSTATE_BUCKET=$(TFSTATE_BUCKET) REGION=$(REGION) sh smoke/preflight.sh

# The APIs a fresh project needs, from 12.1's ENABLE_APIS block (commands/lesson-12.1.sh) -
# the one block of that file that is safe to run as it is. Run once per project, before plan.
apis: guard-project
	@PROJECT=$(PROJECT) sh -c "$$(sed -n '/^# ---- ENABLE_APIS ----/,/^# ---- APPLY ----/p' commands/lesson-12.1.sh | grep -v '^# ----')"

# Use the committed deploy files as-is. Notebook extraction remains an explicit
# authoring action (make extract / make check), never a deployment prerequisite.
# The helper saves verified billing/CI inputs, rejects unexpected destruction,
# and records a unique saved plan for review. Initialize the backend beforehand.
plan: guard-project
	$(INFRA) plan $(INFRA_FLAGS) $(INFRA_VARS)

# A project that already has a Firestore database - Module 4's notebooks create `(default)`
# and, in 4.2, the chunks vector index - makes the first apply a 409. Adopt what exists into
# the state instead of creating it twice; safe to re-run, a second import of the same address
# just reports that it is already managed. Firestore lists every composite index with an
# implicit __name__ field, which the match below sets aside.
adopt-firestore: guard-project
	@cd $(TF_DIR) && $(TF_INIT) >/dev/null && \
	  (terraform import -input=false $(TF_VARS) google_firestore_database.main \
	     "projects/$(PROJECT)/databases/(default)" 2>&1 | tail -1 || true)
	@for pair in chunks:chunks_vector answer_cache:answer_cache_vector; do \
	  coll=$${pair%%:*}; res=$${pair#*:}; \
	  IDX=$$(gcloud firestore indexes composite list --project=$(PROJECT) --format=json 2>/dev/null \
	    | $(PY) -c "import json,sys; ix=[i for i in json.load(sys.stdin) if '/collectionGroups/$$coll/' in i['name'] and {f['fieldPath'] for f in i['fields']}-{'__name__'}=={'tenant_id','embedding'}]; print(ix[0]['name'] if ix else '')"); \
	  if [ -n "$$IDX" ]; then \
	    echo ">> $$coll vector index exists: $$IDX"; \
	    (cd $(TF_DIR) && terraform import -input=false $(TF_VARS) google_firestore_index.$$res "$$IDX" 2>&1 | tail -1 || true); \
	  else echo ">> no $$coll vector index yet - terraform will create it"; fi; \
	done

# First run make plan and review its output. up applies only that checked saved
# plan; it never creates a new plan, regenerates source, switches backends, or
# silently imports existing resources. Run adopt-firestore explicitly if needed
# BEFORE planning. Terraform rejects a saved plan if intervening work made it stale.
up: guard-project
	$(INFRA) apply $(INFRA_FLAGS) $(INFRA_PLAN)
	$(MAKE) drift secrets build deploy-services wait-index roster managed-stores bq-views vector-status

# After an apply, a plan should be EMPTY. A resource that is still "to be replaced" is one the
# provider reads back differently from how it was written - which is how both vector indexes
# were destroyed and re-created on every `make up` until __name__ was declared where Firestore
# puts it. This names the drift instead of letting the next apply act on it quietly.
drift: guard-project
	@cd $(TF_DIR) && out=$$(terraform plan -input=false -no-color -detailed-exitcode $(TF_VARS) 2>&1); rc=$$?; \
	if [ $$rc -eq 0 ]; then echo ">> no drift: the plan after apply is empty"; \
	elif [ $$rc -eq 2 ]; then echo ">> DRIFT - the plan after apply still wants:"; \
	  echo "$$out" | grep -E "must be replaced|will be created|will be destroyed|will be updated|forces replacement|^Plan:"; \
	  echo ">> fix the definition before running up again, or the next apply repeats this"; \
	else echo ">> drift check could not plan:"; echo "$$out" | tail -5; fi

# cookie-secret is created empty by secrets.tf (a holder, never a value in terraform); the
# UI's deploy mounts :latest, and a secret with no version fails that deploy outright.
secrets: guard-project
	@if [ -z "$$(gcloud secrets versions list cookie-secret --project=$(PROJECT) \
	      --filter=state=enabled --format='value(name)' --limit=1)" ]; then \
	  echo ">> cookie-secret: adding a random first version"; \
	  $(PY) -c "import secrets; print(secrets.token_hex(32))" \
	    | gcloud secrets versions add cookie-secret --project=$(PROJECT) --data-file=-; \
	else echo ">> cookie-secret: has a version"; fi

# Cloud Build every image make up deploys. api, ingest, admin and chat import shared/, so
# they build from the deploy/ context through cloudbuild.yaml with the Dockerfile named; the
# UI is self-contained and builds from its own directory.
build: guard-project
	@[ -n "$(SERVICES)" ] || (echo "ERROR: SERVICES names no service"; exit 1)
	@set -e; for svc in $(SERVICES); do \
	  case $$svc in api) dir=rag-api ;; ui) dir=frontend ;; *) dir=$$svc ;; esac; \
	  echo ">> building $(IMAGE_REPO)/$$svc:$(GIT_SHA) from services/$$dir"; \
	  if [ "$$svc" = ui ]; then \
	    gcloud builds submit services/frontend --project=$(PROJECT) --quiet \
	      --tag=$(IMAGE_REPO)/ui:$(GIT_SHA); \
	  else \
	    gcloud builds submit . --project=$(PROJECT) --quiet --config=cloudbuild.yaml \
	      --substitutions=_IMAGE=$(IMAGE_REPO)/$$svc:$(GIT_SHA),_DOCKERFILE=services/$$dir/Dockerfile; \
	  fi; \
	done

# The deploy scripts read their inputs from the environment: the project number for the
# deterministic run.app URLs and the IAP audiences, the processor terraform created, the
# Vector Search names from the state, and RETRIEVAL_BACKEND=vector as the deployment's default
# (a tenant's own pin and its data_region still decide per request - make tenant-backend).
# After the API's DEPLOY block, traffic goes to the revision that block just created, explicitly: make candidate's
# --no-traffic pins the service to a named revision, and Cloud Run then leaves every later deploy at 0% - the routing
# flip on 10 September ran on the old revision for an hour before anyone read status.traffic (F44). By NAME, read
# from status.latestCreatedRevisionName, never "the latest revision" (12 September 2026): the flip names what it moved to.
deploy-services: guard-project
	@[ -n "$(SCRIPTS)" ] || (echo "ERROR: SCRIPTS names no deploy script"; exit 1)
	@set -e; \
	PROJECT_NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	DOCAI_PROCESSOR_ID=$$(cd $(TF_DIR) && terraform output -raw docai_processor_id); \
	DOCAI_LOCATION=$$(cd $(TF_DIR) && terraform output -raw docai_location); \
	RETENTION_DAYS=$$(cd $(TF_DIR) && terraform output -raw retention_days 2>/dev/null || echo $(RETENTION_DAYS)); \
	EMBEDDING_MODEL=$$(cd $(TF_DIR) && terraform output -raw embedding_model 2>/dev/null || echo $(EMBEDDING_MODEL)); \
	EMBEDDING_VERSION=$$(cd $(TF_DIR) && terraform output -raw embedding_version 2>/dev/null || echo $(EMBEDDING_VERSION)); \
	RETRIEVAL_BACKEND=vector; \
	VECTOR_INDEX_ENDPOINT=$$(cd $(TF_DIR) && terraform output -raw vector_index_endpoint); \
	VECTOR_DEPLOYED_INDEX_ID=$$(cd $(TF_DIR) && terraform output -raw vector_deployed_index_id); \
	VECTOR_INDEX_NAME=$$(cd $(TF_DIR) && terraform output -raw vector_index_name); \
	BQ_CHUNK_TABLE=$(PROJECT).rag_data.chunk_source; \
	CHAT_SQL_FLAGS="--add-cloudsql-instances=$(PROJECT):$(REGION):documind-checkpoint --set-secrets=CHECKPOINT_DSN=documind-checkpoint-dsn:latest"; \
	CHAT_EXTRA_ENV=; \
	CHAT_URL=https://documind-chat-$$PROJECT_NUMBER.$(REGION).run.app; \
	for f in $(SCRIPTS); do \
	  echo ">> $$f (DEPLOY block)"; \
	  PROJECT=$(PROJECT) GIT_SHA=$(GIT_SHA) REGION=$(REGION) \
	  RESIDENCY=$(RESIDENCY) MIN_INSTANCES=$(MIN_INSTANCES) ADMIN_EMAILS=$(ADMIN_EMAILS) \
	  ADMIN_DOMAINS=$(ADMIN_DOMAINS) PROJECT_NUMBER=$$PROJECT_NUMBER \
	  DOCAI_PROCESSOR_ID=$$DOCAI_PROCESSOR_ID DOCAI_LOCATION=$$DOCAI_LOCATION \
	  RETRIEVAL_BACKEND=$$RETRIEVAL_BACKEND VECTOR_INDEX_ENDPOINT=$$VECTOR_INDEX_ENDPOINT \
	  VECTOR_DEPLOYED_INDEX_ID=$$VECTOR_DEPLOYED_INDEX_ID VECTOR_INDEX_NAME=$$VECTOR_INDEX_NAME \
	  BQ_CHUNK_TABLE=$$BQ_CHUNK_TABLE CHAT_URL=$$CHAT_URL \
	  CHAT_SQL_FLAGS="$$CHAT_SQL_FLAGS" CHAT_EXTRA_ENV="$$CHAT_EXTRA_ENV" \
	  GENERATOR_MODEL=$(GENERATOR_MODEL) RAG_MODEL_BASE=$(RAG_MODEL_BASE) ROUTING=$(ROUTING) MODEL_BACKEND=$(MODEL_BACKEND) ARMOR=$(ARMOR) SEMANTIC_CACHE=$(SEMANTIC_CACHE) \
  RETRIEVAL_CURRENT_ONLY=$(RETRIEVAL_CURRENT_ONLY) RETRIEVAL_GRAPH=$(RETRIEVAL_GRAPH) \
	  GRAPH_BACKEND=$(GRAPH_BACKEND) SPANNER_INSTANCE=$(SPANNER_INSTANCE) SPANNER_DATABASE=$(SPANNER_DATABASE) \
	  BATCH_JOB_NAME=$(if $(filter true,$(BATCH_JOB)),documind-ingest-batch,) \
	  MANAGED_MIRROR=$(MANAGED_MIRROR) RAG_LOCATION=$(RAG_LOCATION) \
	  RETENTION_DAYS=$$RETENTION_DAYS EMBEDDING_MODEL=$$EMBEDDING_MODEL EMBEDDING_VERSION=$$EMBEDDING_VERSION \
	  BUDGET_USD=$(BUDGET_USD) SPEND_PCT=$(SPEND_PCT) \
	  sh -ec "$$(sed -n '/^# ---- DEPLOY ----/,/^# ---- [A-Z_]* ----$$/p' $$f | grep -v '^# ----')"; \
	done
	@if echo "$(SCRIPTS)" | grep -q lesson-12.2.sh; then \
	  REV=$$(gcloud run services describe documind-api --region $(REGION) --project $(PROJECT) --format='value(status.latestCreatedRevisionName)'); \
	  gcloud run services update-traffic documind-api --region $(REGION) --project $(PROJECT) --to-revisions $$REV=100 --quiet >/dev/null 2>&1 \
	    && echo ">> documind-api: 100% of traffic to $$REV, the revision this deploy created (a candidate tag had pinned it - F44)"; fi
	$(MAKE) operators

# The people who operate this deployment mint identity tokens AS the UI's service account
# (make smoke, make eval-live, 12.2's SMOKE block) and as the outsider fixture (the eval's
# refusal rows, every smoke's last check). Project Owner does not carry that permission; this
# grant does. The first live run stopped on exactly this: "Failed to impersonate documind-ui-sa".
operators: guard-project
	@for who in $$(echo "$(ADMIN_EMAILS)" | tr ',' ' '); do \
	  for sa in documind-ui-sa documind-outsider-sa; do \
	    gcloud iam service-accounts add-iam-policy-binding $$sa@$(PROJECT).iam.gserviceaccount.com \
	      --project=$(PROJECT) --member="user:$$who" --role=roles/iam.serviceAccountTokenCreator \
	      --condition=None --quiet >/dev/null && echo ">> $$who may mint tokens as $$sa"; \
	  done; \
	done

# Firestore refuses find_nearest with FAILED_PRECONDITION until the composite vector indexes
# are READY - minutes at this corpus size, and a smoke test that runs before that reads as
# a broken API.
wait-index: guard-project
	@echo ">> waiting for the chunks vector indexes to be READY"; \
	while gcloud firestore indexes composite list --project=$(PROJECT) \
	        --format='value(state)' | grep -q CREATING; do sleep 15; done; \
	echo ">> indexes READY"

# ---------- The ANN tier (16 September 2026): what it holds, and the proof ----------

# The corpus, one prefix per tenant, into the bucket eventarc.tf watches. Every object is one
# ingest: Document AI, embeddings, Firestore. Watch documind-ingest's logs and the DLQ.
ingest-corpus: guard-project
	PROJECT_ID=$(PROJECT) bash evals/upload.sh

# The corpus's media (Module 9): Figure 3 drawn from the annual report's own table, the invoice as
# a page image, page 30 of the real Payment of Bonus Act - into corpus/acme/, committed, skipped
# when present. MEDIA_ARGS=--video also synthesises the town hall MP4 from townhall_2026_q1.md
# (the Text-to-Speech API and ffmpeg), which is gitignored. Then `make ingest-corpus` uploads them
# like any document: media is a document. Needs, once per shell:
#   pip install --user matplotlib==3.11.1 Pillow==12.3.0 pypdfium2==5.13.0 google-cloud-texttospeech==2.37.0
#   sudo apt-get install -y ffmpeg      (Cloud Shell has no ffmpeg; or pip install --user imageio-ffmpeg==0.6.0)
# The three PNGs are committed, so a fresh clone prints "keep" for them; --video is the work.
MEDIA_ARGS ?=
media:
	$(PY) evals/build_media.py $(MEDIA_ARGS)

# The other half of the eval gate. Needs a deployment, which is why it is not in dryrun and
# why 12.7's pipeline had to be keyless. API defaults to the deterministic run.app URL; the
# identity is the UI's service account, which `make roster` must have put on the tenants
# the golden set asks - the same rule the smoke test states.
#   make eval-live PROJECT=...            or   make eval-live API=https://... DOCUMIND_ID_TOKEN=...
# REPORT=evals/reports/<name>.json writes every row's verdict, latency, stages and cache_hit (12 September 2026):
# the demo reads that file, and evals/reports/ is gitignored.
# The token's audience is the SERVICE's canonical URL whichever of its URLs is called - the API verifies
# bearer tokens against SELF_URL, and a token minted for a candidate's tag URL (make candidate) was refused
# with 401 on all 64 rows (F42). API= picks the URL to call; the audience does not move with it.
eval-live: guard-project
	@AUD=https://documind-api-$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)').$(REGION).run.app; \
	API=$${API:-$$AUD}; \
	TOKEN=$${DOCUMIND_ID_TOKEN:-$$(gcloud auth print-identity-token --include-email \
	  --impersonate-service-account=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com --audiences=$$AUD)}; \
	OUTSIDER=$${DOCUMIND_OUTSIDER_TOKEN:-$$(gcloud auth print-identity-token --include-email \
	  --impersonate-service-account=documind-outsider-sa@$(PROJECT).iam.gserviceaccount.com --audiences=$$AUD)}; \
	echo ">> $$API"; DOCUMIND_ID_TOKEN=$$TOKEN DOCUMIND_OUTSIDER_TOKEN=$$OUTSIDER \
	  $(PY) evals/run_eval.py --api-url $$API $(if $(SOURCE),--source $(SOURCE),) $(if $(REPORT),--report $(REPORT),)

# The ablation harness (4.8, Part 5): retrieval only, no model in the loop, one knob per arm -
# the lane's dense 20 -> rerank 5 against the same without the reranker, a deeper candidate
# list, and 4.5's hybrid leg. Prints recall at the candidate depth beside every reranked
# number. ABLATE_ARGS="--limit 5" is a wiring check; --ledger ~/ablate.jsonl keeps the runs.
# Needs: pip install --user google-genai==2.21.0 google-cloud-firestore==2.30.0 #        google-cloud-discoveryengine==0.13.11 rank-bm25==0.2.2
ablate: guard-project
	$(PY) evals/ablate.py --project $(PROJECT) --region $(REGION) $(ABLATE_ARGS)

# The SQL that terraform does not own. tenant_daily is a VIEW over the log sink (12.3) and
# was, until 2026-09-05, applied by nobody - the file existed and the view did not.
# The view reads fields the API logs (12.2's usage_row), and BigQuery types a sink table's jsonPayload
# from the rows it has seen: run this after an API at this version has answered once (make smoke), or
# the CREATE fails on "Field name retrieve_ms does not exist" - a refusal, never a column of NULLs.
bq-views: guard-project
	bq --project_id=$(PROJECT) query --use_legacy_sql=false < terraform/sql/tenant_daily.sql

# Module 5's production lane (5.5, gap G9): the feature job over the REAL chunks the ingest
# worker streams into rag_data.chunk_source, then the Dataplex quality scan on what ships.
# The scan is the gate: read its result the way 5.5's dq_gate() does before rebuilding the index.
features: guard-project
	bq --project_id=$(PROJECT) query --use_legacy_sql=false < terraform/sql/chunk_metadata.sql
	gcloud dataplex datascans run documind-chunk-dq --project=$(PROJECT) --location=asia-south1 --wait
	gcloud dataplex datascans describe documind-chunk-dq --project=$(PROJECT) --location=asia-south1 \
	  --view=FULL --format="value(dataQualityResult.passed)"

# ---------------------------------------------------------------- Module 10: dataset, cache, judge, tune, candidate
# The tuning dataset, as a document (10.1, 10.5): one Q/A per real chunk of the corpus mirrors, in the
# lane's own citation grammar, PII-scanned, golden questions excluded, frozen with a manifest, in both
# the Gemini SFT and the chat formats, uploaded to the datasets bucket storage.tf makes. Review the rows
# before committing evals/sft/. Needs google-genai and google-cloud-dlp in the shell (make ablate's line).
trainset: guard-project
	$(PY) evals/make_trainset.py --project $(PROJECT) --tenant $(TENANT) --rows $${ROWS:-300} \
	  --upload gs://$(PROJECT)-datasets/sft/ $(TRAINSET_ARGS)

# The tenant's explicit cache (10.2): 4.5's cache_manager gets its first caller. The pack is the tenant's
# synthetic documents; the model is GENERATOR_MODEL; a changed corpus is a new cache. CACHE_OP=show|refresh|delete.
cache: guard-project
	cd services/rag-api && GOOGLE_CLOUD_PROJECT=$(PROJECT) GENERATOR_MODEL=$(GENERATOR_MODEL) \
	  $(PY) cache_admin.py $${CACHE_OP:-create} --project $(PROJECT) --tenant $(TENANT)

# The second judge (10.4): the deployed API's answers to the golden rows, scored for groundedness and
# fulfilment by Vertex AI Evaluation in an Experiments run named by the commit. API_B=<candidate url>
# adds the pairwise against the live revision; CHAT_URL=<chat url> adds the three brains' trajectories.
# Needs `pip install --user google-cloud-aiplatform[evaluation]==2.1.0 pandas` once.
judge: guard-project
	@AUD=https://documind-api-$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)').$(REGION).run.app; \
	API=$${API:-$$AUD}; \
	TOKEN=$${DOCUMIND_ID_TOKEN:-$$(gcloud auth print-identity-token --include-email \
	  --impersonate-service-account=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com --audiences=$$AUD)}; \
	TOKEN_B=$$TOKEN; \
	TOKEN_CHAT=; [ -z "$(CHAT_URL)" ] || TOKEN_CHAT=$$(gcloud auth print-identity-token --include-email \
	  --impersonate-service-account=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com --audiences=$(CHAT_URL)); \
	echo ">> $$API"; DOCUMIND_ID_TOKEN=$$TOKEN DOCUMIND_ID_TOKEN_B=$$TOKEN_B DOCUMIND_ID_TOKEN_CHAT=$$TOKEN_CHAT \
	  $(PY) evals/judge.py --api-url $$API --project $(PROJECT) \
	  $(if $(API_B),--api-b $(API_B),) $(if $(CHAT_URL),--chat-url $(CHAT_URL),) $(JUDGE_ARGS)

# The managed tuning job (10.1): an explicit, billed act like deploy-slm. Waits for the job and prints
# the endpoint and the three commands that judge it.
tune: guard-project
	$(PY) evals/tune.py --project $(PROJECT) --dataset gs://$(PROJECT)-datasets/sft/documind_sft_$${VERSION:-v1}.vertex.jsonl \
	  --base $(TUNE_BASE) --epochs $(TUNE_EPOCHS) --adapter $(TUNE_ADAPTER) $(TUNE_ARGS)

# A candidate revision of the API with another model behind it and NO traffic (10.1's A/B, 10.4's
# pairwise): the live revision keeps serving, and the tagged URL answers the gate and the judge.
#   make candidate GENERATOR_MODEL=projects/.../endpoints/123 RAG_MODEL_BASE=gemini-3.1-flash-lite
#   make candidate MODEL_BACKEND=gateway GENERATOR_MODEL=documind-slm      (11.4: the self-hosted model, judged)
# Env vars MERGE across revisions: a re-tag without GENERATOR_LOCATION removes it (F43), so the generator
# goes back to the location the endpoint path names.
#   make eval-live API=https://candidate---documind-api-NUMBER.us-central1.run.app
candidate: guard-project
	gcloud run services update documind-api --region $(REGION) --project $(PROJECT) --no-traffic --tag candidate \
	  --update-env-vars "^|^GENERATOR_MODEL=$(GENERATOR_MODEL)|RAG_MODEL_BASE=$(RAG_MODEL_BASE)|ROUTING=$(ROUTING)|MODEL_BACKEND=$(MODEL_BACKEND)|ARMOR=$(ARMOR)|SEMANTIC_CACHE=$(SEMANTIC_CACHE)|RETRIEVAL_CURRENT_ONLY=$(RETRIEVAL_CURRENT_ONLY)|RETRIEVAL_GRAPH=$(RETRIEVAL_GRAPH)|GRAPH_BACKEND=$(GRAPH_BACKEND)|SPANNER_INSTANCE=$(SPANNER_INSTANCE)|SPANNER_DATABASE=$(SPANNER_DATABASE)$(if $(RETRIEVAL_BACKEND),|RETRIEVAL_BACKEND=$(RETRIEVAL_BACKEND)|RAG_LOCATION=$(RAG_LOCATION),)$(if $(GENERATOR_LOCATION),|GENERATOR_LOCATION=$(GENERATOR_LOCATION),)" $(if $(GENERATOR_LOCATION),,--remove-env-vars GENERATOR_LOCATION)
	@$(MAKE) --no-print-directory record-candidate PROJECT=$(PROJECT) REGION=$(REGION)
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	echo ">> candidate: https://candidate---documind-api-$$NUMBER.$(REGION).run.app (no traffic; remove with gcloud run services update-traffic documind-api --remove-tags candidate)"

# Eval CANDIDATES from the feed, PII-scanned, into evals/golden_generated.jsonl (5.5 step 11).
# Not a golden set: review, cut, merge the survivors into golden.jsonl by hand.
make-evalset: guard-project
	$(PY) evals/make_evalset.py --project $(PROJECT) --tenant $(TENANT) --rows $${ROWS:-200}

smoke:
	$(PY) smoke/smoke.py

# The MCP server's smoke test (7.2): health, the four tools, a cited answer as a roster member,
# and the outsider account refused by the roster. Activate the operator venv, then run: python -m pip install 'fastmcp==3.4.7'.
smoke-mcp: guard-project
	@NUMBER=$$(gcloud projects describe "$(PROJECT)" --format='value(projectNumber)') || exit 1; \
	case "$$NUMBER" in ''|*[!0-9]*) echo 'ERROR: gcloud did not return a project number; no smoke request sent.' >&2; exit 1;; esac; \
	DOCUMIND_MCP_URL=https://documind-mcp-$$NUMBER.$(REGION).run.app \
	DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com \
	DOCUMIND_OUTSIDER_SA=documind-outsider-sa@$(PROJECT).iam.gserviceaccount.com \
	  $(PY) smoke/smoke_mcp.py

# The chat service's smoke test (8.7 / 12.8): four brains, one question, as a roster member;
# the outsider refused by the roster. The first call to each agent brain is a cold import.
smoke-chat: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	DOCUMIND_CHAT_URL=https://documind-chat-$$NUMBER.$(REGION).run.app \
	DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com \
	DOCUMIND_OUTSIDER_SA=documind-outsider-sa@$(PROJECT).iam.gserviceaccount.com \
	  $(PY) smoke/smoke_chat.py

# The A2A peer's smoke test (8.4): the card behind IAM, one task answered with citations through
# documind-mcp, a task naming another tenant refused by the roster. Plain urllib - no a2a-sdk.
smoke-agent: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	DOCUMIND_AGENT_URL=https://documind-agent-$$NUMBER.$(REGION).run.app \
	DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com \
	  $(PY) smoke/smoke_agent.py

# Module 9 live (9.4's Studio, 9.6's citations): an image generated as a roster member and the
# outsider refused, a figure question answered WITH a figure citation, the media documents listed
# through the MCP server, and a signed PUT the worker indexes. Needs fastmcp for the MCP legs.
smoke-media: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	DOCUMIND_API_URL=https://documind-api-$$NUMBER.$(REGION).run.app \
	DOCUMIND_MCP_URL=https://documind-mcp-$$NUMBER.$(REGION).run.app \
	DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com \
	DOCUMIND_OUTSIDER_SA=documind-outsider-sa@$(PROJECT).iam.gserviceaccount.com \
	  $(PY) smoke/smoke_media.py

# The Rs 0 lane (lesson 6.4 step 7, gap G3): the SAME chat service, DOCUMIND_PROFILE=local -
# Ollama gemma3:4b for the model, a Chroma directory for the corpus, no IAP and no roster
# (LOCAL_USER / LOCAL_TENANT stand in). Needs `pip install -r services/chat/requirements.txt
# -r services/chat/requirements-local.txt` once, and `ollama pull gemma3:4b`. Seeds the six
# teaching passages first; Chroma upserts, so re-running is harmless.
chat-local:
	DOCUMIND_PROFILE=local $(PY) -m shared.local_corpus acme
	cd services/chat && DOCUMIND_PROFILE=local PYTHONPATH=../.. \
	  $(PY) -m uvicorn agent:app --host 127.0.0.1 --port 8081

# destroy needs the SAME variables plan/apply need - terraform resolves every declared
# variable before it touches a resource, so passing two of five made `make down` fail at
# variable resolution and destroy nothing. It also has to init: a fresh clone has no
# .terraform/ and no backend, so destroy would have had no state to read.
# down-services goes first (12 September 2026): the services are not in the state, and the VPC connector they sit on
# cannot go while they use it. Then what only deleting the project removes, printed whether or not destroy got
# through - a used lab's buckets hold objects and force_destroy is false, so destroy stops on them and says so.
down: guard-project down-services
	$(MAKE) managed-stores-down
	@rc=0; (cd $(TF_DIR) && $(TF_INIT) && terraform destroy -input=false -auto-approve $(TF_VARS)) || rc=$$?; \
	echo ">> make down cannot remove these; only deleting the project does:"; \
	echo ">>   Firestore (default): delete_protection_state = DELETE_PROTECTION_ENABLED (firestore.tf) - chunks, ledger, rosters stay"; \
	echo ">>   buckets that hold objects: uploads, datasets, audit (force_destroy = false, storage.tf) - and with AUDIT_LOCK=true the audit bucket's LOCKED retention policy blocks even the project's deletion for five years, by design"; \
	echo ">>   a tuned endpoint from make tune (evals/tune.py: Vertex AI, not Terraform)"; \
	echo ">>   a RAG Engine corpus outside MANAGED_TENANTS (make rag-corpus TENANT= by hand; managed.py: Vertex AI, not Terraform) - it bills storage until deleted: managed-stores-down just removed MANAGED_TENANTS' corpora; gcloud ai rag-corpora ... or 4.3's cleanup cell for the rest"; \
	echo ">>   the BigQuery views make bq-views applied (terraform/sql/*.sql, through bq, not Terraform)"; \
	echo ">> Delete the throwaway project to stop all billing:"; \
	echo ">>   gcloud projects delete $(PROJECT)"; exit $$rc

# ---------- Teardown (12 September 2026): what terraform destroy does not own ----------
# The seven services the deploy scripts create with gcloud run deploy (commands/lesson-*.sh; not Terraform resources,
# so make down left them running and billing their floors), the candidate tag, and the tenants' context caches
# (cache_admin.py delete - a cache bills its storage by the hour until it is deleted). Module 11's three, when they
# were deployed (make deploy-gateway / deploy-slm / deploy-vllm), go the same way. Best effort: every resource is its
# own || line, so an absent one never stops the next, and each echo says what happened.
DOWN_SERVICES     = documind-ingest documind-api documind-admin documind-ui documind-chat documind-mcp documind-agent
DOWN_SERVICES_M11 = documind-gateway
DOWN_SERVICES_GPU = documind-slm documind-vllm
down-services: guard-project
	@for t in acme zeta globex; do \
	  (cd services/rag-api && GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) cache_admin.py delete --project $(PROJECT) --tenant $$t 2>/dev/null) \
	    || echo ">> cache $$t: nothing deleted (no cache, or no google-genai / credentials in this shell)"; done
	@gcloud run services update-traffic documind-api --region $(REGION) --project $(PROJECT) --remove-tags candidate --quiet >/dev/null 2>&1 \
	  && echo ">> candidate tag removed" || echo ">> candidate tag: none to remove"
	@for s in $(DOWN_SERVICES) $(DOWN_SERVICES_M11); do \
	  gcloud run services delete $$s --region $(REGION) --project $(PROJECT) --quiet >/dev/null 2>&1 && echo ">> deleted $$s" || echo ">> $$s: absent"; done
	@for s in $(DOWN_SERVICES_GPU); do \
	  gcloud run services delete $$s --region $(SLM_REGION) --project $(PROJECT) --quiet >/dev/null 2>&1 && echo ">> deleted $$s" || echo ">> $$s: absent"; done
	@rm -f .candidate-revision .previous-revision

# SLM_REGION is deliberately NOT $(REGION)-bound to Mumbai: Cloud Run L4 in asia-south1 is
# INVITATION-ONLY (11.1). us-central1 is the self-serve default; asia-south2 (Delhi) has
# RTX PRO 6000 GA if the data must stay in India, at a different price.
SLM_REGION ?= us-central1

# ---------- Module 11: the backend is a setting, the gateway is a route, the GPU is a bill ----------

# The LiteLLM gateway (11.3): CPU-only, scale to zero, behind IAM like every service - the API's and the UI's accounts
# may invoke it, and the caller's ID token is the door (no master key). Its token proxy fronts the SLM (and the vLLM
# engine when built) with an ID token per call; its spend logs and tag budgets live in the Cloud SQL instance
# gateway.tf declares, mounted here as DATABASE_URL through the connector (15 September 2026, with the one shape).
deploy-gateway: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	SLM=https://documind-slm-$$NUMBER.$(SLM_REGION).run.app; VLLM=$${GEMMA_VLLM_URL:-https://documind-vllm-$$NUMBER.$(SLM_REGION).run.app}; \
	DB=$$(cd $(TF_DIR) && terraform output -raw gateway_database_url); \
	gcloud run deploy documind-gateway --source services/litellm --region $(REGION) --project $(PROJECT) \
	  --service-account documind-gateway-sa@$(PROJECT).iam.gserviceaccount.com --no-allow-unauthenticated \
	  --memory 2Gi --cpu 2 --cpu-boost --min-instances 0 --max-instances 3 --concurrency 20 --timeout 300 \
	  --add-cloudsql-instances $(PROJECT):$(REGION):documind-gateway \
	  --set-env-vars "^|^VERTEXAI_PROJECT=$(PROJECT)|GOOGLE_CLOUD_PROJECT=$(PROJECT)|SLM_URL=$$SLM|GEMMA_VLLM_URL=$$VLLM|DATABASE_URL=$$DB|ROUTER_ENFORCE=$(ROUTER_ENFORCE)" --quiet; \
	for sa in documind-api-sa documind-ui-sa; do \
	  gcloud run services add-iam-policy-binding documind-gateway --region $(REGION) --project $(PROJECT) \
	    --member=serviceAccount:$$sa@$(PROJECT).iam.gserviceaccount.com --role=roles/run.invoker --quiet >/dev/null; done; \
	echo ">> gateway: https://documind-gateway-$$NUMBER.$(REGION).run.app (the API and the UI's account may call it)"

gateway-off: guard-project
	gcloud run services update documind-gateway --region $(REGION) --project $(PROJECT) --min-instances 0 --quiet
	@echo "documind-gateway scaled to zero"

smoke-gateway: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	DOCUMIND_GATEWAY_URL=https://documind-gateway-$$NUMBER.$(REGION).run.app \
	DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com \
	  $(PY) smoke/smoke_gateway.py

# The GPU backend from lesson 11.4. Separate from deploy-services on purpose: it costs Rs 86,904/month if
# min-instances is left above zero (11.1: $1.42/hr for the whole 8vCPU/32GiB instance, not the $0.672 GPU line),
# so it is an explicit act and never a side effect of deploying everything else. The build context is staged first:
# 10.5's GGUF and the Modelfile it generated from the tokenizer, from the datasets bucket - or, with SLM_STOCK set,
# a stock model as the stand-in until that file lands (decision D2). The startup probe waits for the model, not
# the process; the gateway's and the UI's accounts may invoke it.
deploy-slm: guard-project
	@mkdir -p services/slm/build; rm -f services/slm/build/documind-slm.gguf services/slm/build/Modelfile services/slm/build/STOCK; \
	if [ -n "$(SLM_STOCK)" ]; then echo "$(SLM_STOCK)" > services/slm/build/STOCK; echo ">> stand-in: $(SLM_STOCK) will be served as documind-slm"; \
	else gcloud storage cp gs://$(PROJECT)-datasets/sft/documind-slm.gguf services/slm/build/ && \
	     gcloud storage cp gs://$(PROJECT)-datasets/sft/documind-slm.Modelfile services/slm/build/Modelfile; fi
	gcloud run deploy documind-slm \
	  --source services/slm --region $(SLM_REGION) --project $(PROJECT) \
	  --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy \
	  --cpu 8 --memory 32Gi \
	  --max-instances 1 --min-instances 0 --timeout 600 --concurrency 4 \
	  --no-allow-unauthenticated --labels slm-source=$(if $(SLM_STOCK),stock,gguf) \
	  --startup-probe httpGet.path=/api/tags,httpGet.port=8080,initialDelaySeconds=10,periodSeconds=5,failureThreshold=30 --quiet
	@for sa in documind-gateway-sa documind-ui-sa; do \
	  gcloud run services add-iam-policy-binding documind-slm --region $(SLM_REGION) --project $(PROJECT) \
	    --member=serviceAccount:$$sa@$(PROJECT).iam.gserviceaccount.com --role=roles/run.invoker --quiet >/dev/null; done; \
	NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	echo ">> slm: https://documind-slm-$$NUMBER.$(SLM_REGION).run.app (min-instances 0; make slm-off after every session anyway)"

# Run this when the demo is over. Forgetting it is the most expensive mistake in
# the course: Rs 86,904/month for a service nobody queries.
slm-off: guard-project
	gcloud run services update documind-slm --region $(SLM_REGION) --project $(PROJECT) --min-instances 0 --quiet
	@echo "documind-slm scaled to zero"

smoke-slm: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	DOCUMIND_SLM_URL=https://documind-slm-$$NUMBER.$(SLM_REGION).run.app \
	DOCUMIND_GATEWAY_URL=https://documind-gateway-$$NUMBER.$(REGION).run.app \
	DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com DOCUMIND_PROJECT=$(PROJECT) SLM_REGION=$(SLM_REGION) \
	  $(PY) smoke/smoke_slm.py

# 11.1 / 11.2's vLLM engine: a 13 GB image with the Gemma weights baked (a Hugging Face token with Gemma access in
# the hf-token secret; 20 to 30 minutes on E2_HIGHCPU_32), then the same six cost flags as the SLM. Optional (D3).
build-vllm: guard-project
	gcloud builds submit services/gemma-vllm --project $(PROJECT) --config services/gemma-vllm/cloudbuild.yaml \
	  --substitutions=_IMAGE=$(IMAGE_REPO)/gemma-vllm:latest

deploy-vllm: guard-project
	gcloud run deploy documind-vllm --image $(IMAGE_REPO)/gemma-vllm:latest --region $(SLM_REGION) --project $(PROJECT) \
	  --service-account documind-vllm-sa@$(PROJECT).iam.gserviceaccount.com \
	  --gpu 1 --gpu-type nvidia-l4 --no-gpu-zonal-redundancy --cpu 8 --memory 32Gi --port 8080 \
	  --max-instances 1 --min-instances 0 --concurrency 32 --timeout 600 --no-cpu-throttling --cpu-boost \
	  --no-allow-unauthenticated --set-env-vars GOOGLE_CLOUD_PROJECT=$(PROJECT) \
	  --startup-probe httpGet.path=/health,httpGet.port=8080,initialDelaySeconds=120,failureThreshold=5,timeoutSeconds=10,periodSeconds=30 --quiet
	@for sa in documind-gateway-sa documind-ui-sa; do \
	  gcloud run services add-iam-policy-binding documind-vllm --region $(SLM_REGION) --project $(PROJECT) \
	    --member=serviceAccount:$$sa@$(PROJECT).iam.gserviceaccount.com --role=roles/run.invoker --quiet >/dev/null; done

vllm-off: guard-project
	gcloud run services update documind-vllm --region $(SLM_REGION) --project $(PROJECT) --min-instances 0 --quiet
	@echo "documind-vllm scaled to zero"

# 11.5's GPU comparison requires gke_autopilot=true and sufficient SSD/GPU quota.
# The default Standard lab has one small CPU node and cannot schedule this GPU pod.
# Changing gke_autopilot replaces the cluster; review/apply that Terraform plan first.
# These targets manage the workload only. The cluster remains until make down.
gke-up: guard-project
	@MODE=$$(gcloud container clusters describe documind-autopilot --region $(REGION) --project $(PROJECT) --format='value(autopilot.enabled)') || exit $$?; \
	  case "$$MODE" in True|true) ;; *) echo "STOP: the Standard CPU lab cannot run the L4 GPU lesson. Set gke_autopilot=true in Terraform, review cluster replacement and quota, and apply before make gke-up." >&2; exit 1 ;; esac
	gcloud container clusters get-credentials documind-autopilot --region $(REGION) --project $(PROJECT)
	IMAGE=$(IMAGE_REPO)/gemma-vllm:latest envsubst < gke/vllm-deployment.yaml | kubectl apply -f -
	@echo ">> kubectl get pods -w   (an accelerator node takes minutes); then: kubectl port-forward svc/documind-vllm 8080:80"

gke-down: guard-project
	-gcloud container clusters get-credentials documind-autopilot --region $(REGION) --project $(PROJECT)
	-IMAGE=$(IMAGE_REPO)/gemma-vllm:latest envsubst < gke/vllm-deployment.yaml | kubectl delete -f - --ignore-not-found
	@echo "vLLM workload removed; the cluster and any Standard lab node remain (gke.tf) - make down removes them"

# The honest comparison (10.6, 11.4): the same golden rows, retrieval once through the one retrieve(), every backend
# answering from the same context through the gateway. The four numbers, and the SLM's rupees are a RATE.
compare: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	GW=https://documind-gateway-$$NUMBER.$(REGION).run.app; \
	TOKEN=$$(gcloud auth print-identity-token --include-email --impersonate-service-account=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com --audiences=$$GW); \
	LITELLM_URL=$$GW LITELLM_ID_TOKEN=$$TOKEN RAG_API_URL=https://documind-api-$$NUMBER.$(REGION).run.app \
	DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com GOOGLE_CLOUD_PROJECT=$(PROJECT) \
	  $(PY) services/slm/compare_backends.py --rows $${ROWS:-20} --backends $${BACKENDS:-documind-general,documind-slm} > compare.csv && \
	  $(PY) services/slm/compare_backends.py --summary compare.csv

# ---------- Cost controls (10 September 2026): the ceiling, the alarm, the night switch ----------
# The ceiling: the region's L4 quota capped at CAP (1) by a consumer quota override - a project-level limit under every
# service's --max-instances 1, so a second GPU service or a typo cannot allocate a second card. Lowering a quota needs
# no approval; raising it does. The metric names come from the project (services/slm/gpu_quota.py reads them), never
# from this file. The alarm is alerts.tf's gpu_left_warm; the switch is `off`, by hand or nightly (terraform/off.tf).
CAP ?= 1
gpu-quota: guard-project
	$(PY) services/slm/gpu_quota.py --project $(PROJECT) --region $(SLM_REGION)

gpu-cap: guard-project
	$(PY) services/slm/gpu_quota.py --project $(PROJECT) --region $(SLM_REGION) --cap $(CAP)

gpu-cap-off: guard-project
	$(PY) services/slm/gpu_quota.py --project $(PROJECT) --region $(SLM_REGION) --remove

# The four switches in one, then the lines that prove it. The documind-off Cloud Run job runs the same thing at 23:00
# IST every night (terraform/off.tf), so a forgotten GPU costs an evening, not a month; off-now runs that job by hand.
off: guard-project
	-$(MAKE) slm-off PROJECT=$(PROJECT)
	-$(MAKE) vllm-off PROJECT=$(PROJECT)
	-$(MAKE) gateway-off PROJECT=$(PROJECT)
	-gcloud run services update documind-ui --region $(REGION) --project $(PROJECT) --min-instances 0 --quiet
	-$(MAKE) gke-down PROJECT=$(PROJECT)
	@for s in documind-slm documind-vllm documind-gateway documind-ui; do \
	  f=$$(gcloud run services describe $$s --region $(SLM_REGION) --project $(PROJECT) --format=json 2>/dev/null | $(PY) -c "import json,sys; j=json.load(sys.stdin); print((j['spec']['template']['metadata'].get('annotations') or {}).get('autoscaling.knative.dev/minScale','0'))" 2>/dev/null || \
	     gcloud run services describe $$s --region $(REGION) --project $(PROJECT) --format=json 2>/dev/null | $(PY) -c "import json,sys; j=json.load(sys.stdin); print((j['spec']['template']['metadata'].get('annotations') or {}).get('autoscaling.knative.dev/minScale','0'))" 2>/dev/null || echo absent); \
	  echo "$$s: min-instances $$f"; done

off-now: guard-project
	gcloud run jobs execute documind-off --region $(REGION) --project $(PROJECT) --wait

# ---------- Module 12 (10 September 2026): the release is a candidate; the smokes as one; the rows as a tool ----------
# 12.7's release unit. make build pushed api:$(GIT_SHA); this puts that image on a no-traffic revision tagged candidate,
# make eval-live API=<its URL> judges it, make promote moves traffic - and make rollback moves it back. Module 10's
# candidate target flips ENV on the current image; this one flips the IMAGE, which is what a release is.
# BY NAME (12 September 2026): the revision this makes is recorded in .candidate-revision (gitignored) and promote
# moves traffic to that name - never "the latest revision", which promotes whatever is newest: two candidates in flight, or a
# make up between the gate and the flip, and it would have promoted a revision the gate never judged. GIT_SHA goes on
# the revision too: env vars merge across revisions (F43), so an image-only update left /version naming the OLD sha,
# and the workflow now refuses a candidate that does not answer as the tag it releases.
release-candidate: guard-project
	gcloud run services update documind-api --region $(REGION) --project $(PROJECT) --image $(IMAGE_REPO)/api:$(GIT_SHA) --no-traffic --tag candidate --update-env-vars GIT_SHA=$(GIT_SHA) --quiet
	@$(MAKE) --no-print-directory record-candidate PROJECT=$(PROJECT) REGION=$(REGION)
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	echo ">> candidate: https://candidate---documind-api-$$NUMBER.$(REGION).run.app  (make eval-live API=that, then make promote)"

# The revision the update just created, by name, into .candidate-revision - the one promote reads. Shared by candidate
# (Module 10's env flip) and release-candidate (12.7's image flip): both make a no-traffic revision the gate judges.
record-candidate: guard-project
	@REV=$$(gcloud run services describe documind-api --region $(REGION) --project $(PROJECT) --format='value(status.latestCreatedRevisionName)'); \
	[ -n "$$REV" ] || { echo "ERROR: could not read the candidate's name (status.latestCreatedRevisionName of documind-api)"; exit 1; }; \
	echo "$$REV" > .candidate-revision; echo ">> candidate revision: $$REV (deploy/.candidate-revision - make promote moves traffic to it by name)"

# Traffic to the recorded candidate, by name. Before the flip, the revision serving 100% goes to .previous-revision -
# the one make rollback returns to. Refuses when the candidate tag has moved on: a newer candidate superseded the one
# this checkout recorded, and the gate judged that one, not this one.
promote: guard-project
	@[ -s .candidate-revision ] || { echo "ERROR: deploy/.candidate-revision is missing - make release-candidate (or make candidate) records the revision it makes, and promote moves traffic to THAT revision by name, never to whatever is newest"; exit 1; }
	@CAND=$$(cat .candidate-revision); \
	STATE=$$(gcloud run services describe documind-api --region $(REGION) --project $(PROJECT) --format=json | $(PY) -c "import json,sys; t=json.load(sys.stdin).get('status',{}).get('traffic',[]); s=[x for x in t if x.get('percent')]; s=max(s, key=lambda x: x['percent']) if s else {}; c=[x for x in t if x.get('tag')=='candidate']; print((s.get('revisionName') or '')+'|'+((c[0].get('revisionName') or '') if c else ''))"); \
	[ -n "$$STATE" ] || { echo "ERROR: could not read documind-api's traffic (gcloud run services describe)"; exit 1; }; \
	PREV=$${STATE%%|*}; TAGGED=$${STATE##*|}; \
	if [ -n "$$TAGGED" ] && [ "$$TAGGED" != "$$CAND" ]; then echo "ERROR: the candidate tag is on $$TAGGED but deploy/.candidate-revision names $$CAND - a newer candidate superseded this one, and the gate judged that one; make release-candidate again"; exit 1; fi; \
	if [ "$$PREV" = "$$CAND" ]; then echo ">> $$CAND already serves 100%"; exit 0; fi; \
	if [ -n "$$PREV" ]; then echo "$$PREV" > .previous-revision; echo ">> serving before the flip: $$PREV (deploy/.previous-revision - make rollback returns to it)"; fi; \
	gcloud run services update-traffic documind-api --region $(REGION) --project $(PROJECT) --to-revisions $$CAND=100 --quiet; \
	NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); URL=https://documind-api-$$NUMBER.$(REGION).run.app; \
	TOKEN=$$(gcloud auth print-identity-token --include-email --impersonate-service-account=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com --audiences=$$URL); \
	echo ">> live: $$CAND"; curl -s -H "Authorization: Bearer $$TOKEN" $$URL/version; echo

# Back to the revision that was serving before make promote flipped - recorded by name in .previous-revision, not "the
# second newest by creation time", which is the wrong revision the moment a newer candidate exists. Fifteen seconds;
# the fastest rollback there is, because that revision never went away. GET /version afterwards says which sha answers.
rollback: guard-project
	@[ -s .previous-revision ] || { echo "ERROR: deploy/.previous-revision is missing - make promote records the revision it moved traffic off, and nothing was promoted from this checkout. By hand: gcloud run revisions list --service documind-api --region $(REGION) --project $(PROJECT), then gcloud run services update-traffic documind-api --region $(REGION) --project $(PROJECT) --to-revisions <name>=100"; exit 1; }
	@PREV=$$(cat .previous-revision); \
	gcloud run services update-traffic documind-api --region $(REGION) --project $(PROJECT) --to-revisions $$PREV=100 --quiet && echo ">> traffic on $$PREV"

# 12.8's full smoke: the seven smokes in one run, with the exports smoke.py reads. A refusal line is a PASS - a system
# that answers everybody passes a smoke made only of "does it work?".
# The smoke's OWN exit status decides (12 September 2026): a pipeline's status is grep's, and grep is satisfied by a
# FAIL line, so a failing smoke left this target at exit 0 and the release day green. /bin/sh is dash on the runners
# and has no pipefail, so each smoke writes its status to a file inside the pipeline and the loop reads it after.
smoke-all: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); rc=0; ST=$$(mktemp); \
	for t in smoke smoke-mcp smoke-chat smoke-agent smoke-media smoke-reindex smoke-gateway smoke-slm; do echo "== $$t"; \
	  { DOCUMIND_PROJECT=$(PROJECT) DOCUMIND_API_URL=https://documind-api-$$NUMBER.$(REGION).run.app DOCUMIND_TENANT=$(TENANT) \
	    DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com $(MAKE) --no-print-directory $$t PROJECT=$(PROJECT) 2>&1; \
	    echo $$? > $$ST; } | grep -E "PASS|FAIL|pass|fail|not set" || rc=1; \
	  [ "$$(cat $$ST)" = 0 ] || { rc=1; echo "== $$t: FAILED (exit $$(cat $$ST))"; }; done; rm -f $$ST; exit $$rc

# 12.3: tenant_daily's GROUP BY over the usage rows in Cloud Logging (evals/usage_rows.py) - the same figures the
# BigQuery view holds, from the rows themselves.
usage: guard-project
	$(PY) evals/usage_rows.py --project $(PROJECT) --hours $${HOURS:-24}

# ---------- the managed mirror (P9, 13 September 2026): 4.3's RAG Engine and 4.4's Vertex AI Search per tenant ----------
# services/ingest/managed.py. The stores are declared, never created by the worker: MANAGED_SEARCH=true on make plan /
# make up declares a Vertex AI Search data store per tenant (managed.tf, the tenants variable); rag-corpus creates a
# tenant's RAG Engine corpus once, by name, after rag-engine-enable has put the project in serverless mode (4.3's
# one-time prep: the default store is allowlist-only, and vectorsearch.googleapis.com backs the serverless one).
# managed-status reads every store against the ledger. Then make deploy-services MANAGED_MIRROR=both (or one) and
# every version the worker swaps current is mirrored; the same value on make plan / make up gives it to the jobs.
# Needs, on Cloud Shell: pip install --user google-cloud-aiplatform==1.153.1 google-cloud-discoveryengine==0.13.11 google-cloud-firestore==2.30.0
rag-engine-enable: guard-project
	gcloud services enable vectorsearch.googleapis.com aiplatform.googleapis.com --project $(PROJECT)
	curl -sS -X PATCH -H "Authorization: Bearer $$(gcloud auth print-access-token)" -H "Content-Type: application/json" \
	  "https://$(RAG_LOCATION)-aiplatform.googleapis.com/v1beta1/projects/$(PROJECT)/locations/$(RAG_LOCATION)/ragEngineConfig" \
	  -d '{"ragManagedDbConfig": {"serverless": {}}}' | head -c 400; echo
	@echo ">> RAG Engine: serverless mode requested in $(RAG_LOCATION) (eventually consistent: a first make rag-corpus may need a minute)"
rag-corpus: guard-project
	cd services/ingest && GOOGLE_CLOUD_PROJECT=$(PROJECT) RAG_LOCATION=$(RAG_LOCATION) EMBEDDING_MODEL=$(EMBEDDING_MODEL) \
	  $(PY) managed.py --project $(PROJECT) --create-corpus --tenant $(TENANT)
managed-status: guard-project
	cd services/ingest && GOOGLE_CLOUD_PROJECT=$(PROJECT) RAG_LOCATION=$(RAG_LOCATION) AUDIT_BUCKET=$(PROJECT)-audit \
	  $(PY) managed.py --project $(PROJECT) --status --mode $(if $(filter off,$(MANAGED_MIRROR)),both,$(MANAGED_MIRROR)) $(if $(TENANT_ONLY),--tenant $(TENANT_ONLY),)

# The managed stores, all of them (13 September 2026, evening): make up runs this after the
# roster (the policies are set) and before the first ingest - the mirror never creates a store, and a version
# ingested before its corpus exists waits for the walk's repair (the plan's R6). RAG Engine in serverless mode
# once, a corpus per `any` tenant (idempotent by name), the data stores already declared by managed.tf
# (MANAGED_SEARCH=true); then the demo's pins, so one deployment answers from all three stores: acme from
# 4.3's corpus, zeta from 4.4's data store, globex (data_region in) from the kit's own rows. Safe to re-run.
managed-stores: guard-project
	$(MAKE) rag-engine-enable
	@for t in $(MANAGED_TENANTS); do $(MAKE) rag-corpus TENANT=$$t || exit 1; done
	@GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy backend acme rag_engine
	@GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy backend zeta vertex_search
	@echo ">> managed stores ready: corpora for $(MANAGED_TENANTS); acme reads rag_engine, zeta vertex_search, globex the kit's rows; make managed-status after make ingest-corpus"

# The corpora bill storage until deleted and are not Terraform's to destroy: make down runs this first.
# The data stores go with terraform destroy. The pins are cleared so a redeploy of the same project does not
# send acme and zeta to stores that no longer exist.
managed-stores-down: guard-project
	@for t in $(MANAGED_TENANTS); do \
	  (cd services/ingest && GOOGLE_CLOUD_PROJECT=$(PROJECT) RAG_LOCATION=$(RAG_LOCATION) $(PY) managed.py --project $(PROJECT) --delete-corpus --tenant $$t) || exit 1; \
	  GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy backend $$t default; \
	done

# ---------- 4.6's graph, on the lane (13 September 2026) ----------
# services/ingest/graph.py: the tenant's current chunks through the lesson's extraction, resolution and build, written
# with shared/documind_graph.FirestoreGraph - the class the notebook runs - into graph_nodes / graph_edges. Then the API
# reads it: make candidate RETRIEVAL_GRAPH=auto (a relational question with a seed entity) or =on (every question), the
# whole corpus by default; GRAPH_ARGS="--source hr_policy_2026.md" builds one document's graph deterministically (16 September
# 2026: reading order sorts the Acts before the handbook, so a limit never reached the handbook's entities once the corpus was in).
# gate on the candidate, make promote. GRAPH_ARGS="--limit 200" is the bill; --ask "..." walks it for one question.
# GRAPH_BACKEND=spanner writes it to Spanner Graph instead, with each name's embedding, and --ask then seeds by meaning.
# Needs: pip install --user google-genai==2.22.0 google-cloud-firestore==2.30.0 google-cloud-spanner==3.60.0 numpy
GRAPH_ARGS ?=
graph: guard-project
	cd services/ingest && GOOGLE_CLOUD_PROJECT=$(PROJECT) PYTHONPATH=../.. \
	  SPANNER_INSTANCE=$(SPANNER_INSTANCE) SPANNER_DATABASE=$(SPANNER_DATABASE) \
	  $(PY) graph.py --project $(PROJECT) --tenant $(TENANT) --backend $(GRAPH_BACKEND) $(GRAPH_ARGS)
