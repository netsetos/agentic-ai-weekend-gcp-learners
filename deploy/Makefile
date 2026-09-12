# DocuMind AI — deploy kit
#
# Tier A (offline, no GCP, free) — run anywhere, runs in CI:
#     make dryrun          extract check + full offline validation
#     make extract         (re)generate the deploy tree from the notebooks
#     make validate        run the Tier-A checks only
#
# Tier B (live, needs gcloud + terraform + a throwaway project — YOU run these on GCP):
#     make plan  PROJECT=documind-ai-live-0910     terraform dry run (creates nothing)
#     make up    PROJECT=...                        apply, build + deploy the services, seed the roster
#     make media [MEDIA_ARGS=--video]              the corpus's figures (and the town hall video) under evals/corpus/
#     make ingest-corpus PROJECT=...                the corpus into the uploads bucket, one prefix per tenant
#     make smoke DOCUMIND_API_URL=https://...       end-to-end smoke test
#     make trainset / cache / judge / tune / candidate   Module 10: the dataset, the tenant cache, the second judge,
#                                                   the managed tuning job, a no-traffic revision for the A/B
#     make eval-live PROJECT=...                    the golden set against the deployed API
#     make down  PROJECT=...                        destroy everything (no cost bleed)
#
# PROFILE=lean (the default) is the Module 4 lane: the API, the UI and the ingest worker on
# Cloud Run, Firestore with its vector indexes, one Document AI processor, the uploads bucket -
# plus, since Modules 7 and 8 joined it, the MCP server, the chat service on an in-memory
# checkpointer (no Cloud SQL) and the A2A peer, all scale-to-zero. PROFILE=full adds what Module 12 teaches - Vector Search, the Spanner
# Graph trial, the Cloud SQL checkpointer, GKE, the BigQuery mirror and Dataplex scan, the
# log sink, Cloud Deploy - and the admin and chat services. See README.md.
#
# On Windows without `make`, run the python commands directly, e.g.
#     python deploy/validate.py
#
# Tier B targets require terraform + gcloud and a real PROJECT; they intentionally
# refuse to run without it. See README.md for the full live-day runbook.

PY      ?= python
# Every python the kit runs prints as it goes: under nohup or a pipe, python buffers stdout and a
# twenty-minute make trainset showed nothing until it exited (the Module 10 phase, 10 September).
export PYTHONUNBUFFERED = 1
TF_DIR   = terraform
PLANFILE = tfplan.dryrun

# REGION is where the SERVICES run - Cloud Run, the VPC connector, Artifact Registry - and
# the deploy scripts under commands/ build their image paths and connector paths from it.
# Data at rest (Firestore, the buckets, Model Armor) follows variables.tf's india_region.
#
# No comment on the same line as a value: Make keeps the blanks before a `#` as part of the
# value, so `PROFILE ?= lean   # ...` made $(SERVICES_$(PROFILE)) name a variable that does not
# exist, and `make up` built and deployed nothing without a word - the first live run.
REGION        ?= us-central1
# lean | full
PROFILE       ?= lean
# us: Layout Parser in the US | india: Enterprise OCR in Mumbai
RESIDENCY     ?= us
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
# stand-in until 10.5's GGUF is in the bucket; GATEWAY_CONFIG picks the profile's LiteLLM config.
MODEL_BACKEND   ?= vertex
SLM_STOCK       ?=
GATEWAY_CONFIG  ?= config.lean.yaml
ROUTER_ENFORCE  ?= 1
# Module 12: the guard is a switch (12.6) and the pipeline's ref is a variable (12.7)
ARMOR           ?= off
DEPLOY_REF      ?= refs/heads/main
# 12.5's ledger (11 September 2026): the API retrieves only chunks the ledger marks current - after the second
# vector index is built and make backfill-current has run; RECONCILE_JOB=true schedules the nightly reconcile
RETRIEVAL_CURRENT_ONLY ?= off
RECONCILE_JOB   ?= false

# Three variables are declared with no default - project_id, billing_account_id and
# github_repository_id - so terraform stops and PROMPTS for any one that is not passed.
# That is what made `make plan` fail unattended. Every one of them is supplied below;
# override any on the command line.
#
# github_repository_id arrived with 12.7 and deliberately has no default in variables.tf ("a
# wrong id fails closed"). That is right for terraform and wrong for a dry run, which needs to
# plan without a GitHub repo at all - so the placeholder lives HERE, where it is obviously a
# placeholder, instead of in the variable where it would look like a real default.
#   Real value: gh api repos/OWNER/REPO --jq .id   (netsetos/agentic-ai-weekend-gcp: 1358872052)
# In the billing account's currency (an Indian account is INR): 5,000 a month is a rehearsal's
# worth of Document AI and Gemini with room to spare. The lesson's own figure was 500 USD.
BUDGET_AMOUNT      ?= 5000
PAGERDUTY_KEY      ?= unset-in-dryrun
BILLING_ACCOUNT_ID ?= 000000-000000-000000
GITHUB_REPO_ID     ?= 000000000
# The state bucket is the one resource that cannot be Terraformed (chicken and egg), so the
# backend is configured at init time rather than hard-coded into backend.tf.
TFSTATE_BUCKET     ?= documind-tfstate
TFSTATE_PREFIX     ?= documind/$(PROJECT)

TF_VARS = -var project_id=$(PROJECT) \
          -var region=$(REGION) \
          -var profile=$(PROFILE) \
          -var residency=$(RESIDENCY) \
          -var budget_amount=$(BUDGET_AMOUNT) \
          -var pagerduty_key=$(PAGERDUTY_KEY) \
          -var billing_account_id=$(BILLING_ACCOUNT_ID) \
          -var github_repository_id=$(GITHUB_REPO_ID) \
          -var 'alert_emails=$(ALERT_EMAILS_JSON)' \
          -var deploy_ref=$(DEPLOY_REF) \
          -var reconcile_job=$(RECONCILE_JOB)
TF_INIT = terraform init -input=false -reconfigure \
          -backend-config="bucket=$(TFSTATE_BUCKET)" \
          -backend-config="prefix=$(TFSTATE_PREFIX)"

# The services each profile builds and deploys, in the order they must come up: the worker
# first (the push subscription already points at its URL), then the API, then the surfaces
# that call it. The flags live in commands/lesson-12.*.sh, extracted from the notebooks, so
# the thing that runs is the thing the lesson taught and there is no second copy to drift.
IMAGE_REPO    = $(REGION)-docker.pkg.dev/$(PROJECT)/documind
SERVICES_lean = ingest api ui mcp chat agent
SERVICES_full = ingest api admin ui chat mcp agent
SCRIPTS_lean  = commands/lesson-12.5.sh commands/lesson-12.2.sh commands/lesson-12.4.sh \
                commands/lesson-7.2.sh commands/lesson-12.8.sh commands/lesson-8.4.sh
SCRIPTS_full  = commands/lesson-12.5.sh commands/lesson-12.2.sh commands/lesson-12.3.sh \
                commands/lesson-12.4.sh commands/lesson-12.8.sh \
                commands/lesson-7.2.sh commands/lesson-8.4.sh

.PHONY: dryrun extract check validate eval eval-live plan up secrets build deploy-services \
        wait-index roster ingest-corpus smoke down guard-project bq-views features make-evalset \
        chat-local deploy-slm slm-off ablate smoke-mcp smoke-chat smoke-agent \
        trainset cache judge tune candidate \
        deploy-gateway gateway-off smoke-gateway smoke-slm build-vllm deploy-vllm vllm-off gke-up gke-down compare \
        gpu-quota gpu-cap gpu-cap-off off off-now \
        release-candidate promote rollback smoke-all usage ingest-one poison dlq \
        reindex retire reconcile reconcile-job backfill-current

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

# terraform plan == the safe dry run: previews every resource, creates nothing.
# -input=false makes a missing variable an ERROR instead of a prompt, which is what you want
# in CI: a hang at 3am is worse than a failure at 3am.
plan: guard-project extract
	cd $(TF_DIR) && $(TF_INIT) && \
	  terraform plan -input=false $(TF_VARS) -out=$(PLANFILE)

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

# One command, in the order the pieces depend on each other: the infrastructure, a first
# version of the one secret the UI mounts, the images, the services, the vector indexes
# READY, the roster. Idempotent - re-run it and you are back in a few minutes.
up: guard-project extract adopt-firestore
	cd $(TF_DIR) && $(TF_INIT) && \
	  terraform apply -input=false -auto-approve $(TF_VARS)
	$(MAKE) drift secrets build deploy-services wait-index roster
	@if [ "$(PROFILE)" = full ]; then $(MAKE) bq-views; fi

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

# Cloud Build every image this profile deploys. api, ingest, admin and chat import shared/, so
# they build from the deploy/ context through cloudbuild.yaml with the Dockerfile named; the
# UI is self-contained and builds from its own directory.
build: guard-project
	@[ -n "$(SERVICES_$(PROFILE))" ] || (echo "ERROR: PROFILE='$(PROFILE)' names no services (lean | full)"; exit 1)
	@set -e; for svc in $(SERVICES_$(PROFILE)); do \
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
# deterministic run.app URLs and the IAP audiences, the processor terraform created, and -
# in the full profile - the Vector Search names. In the lean profile RETRIEVAL_BACKEND is
# firestore and the vector variables are empty on purpose.
# After the API's DEPLOY block, traffic goes to the LATEST revision explicitly: make candidate's --no-traffic
# pins the service to a named revision, and Cloud Run then leaves every later deploy at 0% - the routing
# flip on 10 September ran on the old revision for an hour before anyone read status.traffic (F44).
deploy-services: guard-project
	@[ -n "$(SCRIPTS_$(PROFILE))" ] || (echo "ERROR: PROFILE='$(PROFILE)' names no deploy scripts (lean | full)"; exit 1)
	@set -e; \
	PROJECT_NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	DOCAI_PROCESSOR_ID=$$(cd $(TF_DIR) && terraform output -raw docai_processor_id); \
	DOCAI_LOCATION=$$(cd $(TF_DIR) && terraform output -raw docai_location); \
	if [ "$(PROFILE)" = full ]; then \
	  RETRIEVAL_BACKEND=vector; \
	  VECTOR_INDEX_ENDPOINT=$$(cd $(TF_DIR) && terraform output -raw vector_index_endpoint); \
	  VECTOR_DEPLOYED_INDEX_ID=$$(cd $(TF_DIR) && terraform output -raw vector_deployed_index_id); \
	  VECTOR_INDEX_NAME=$$(cd $(TF_DIR) && terraform output -raw vector_index_name); \
	  BQ_CHUNK_TABLE=$(PROJECT).rag_data.chunk_source; \
	  CHAT_SQL_FLAGS="--add-cloudsql-instances=$(PROJECT):$(REGION):documind-checkpoint --set-secrets=CHECKPOINT_DSN=documind-checkpoint-dsn:latest"; \
	  CHAT_EXTRA_ENV=; \
	else \
	  RETRIEVAL_BACKEND=firestore; VECTOR_INDEX_ENDPOINT=; VECTOR_DEPLOYED_INDEX_ID=; \
	  VECTOR_INDEX_NAME=; BQ_CHUNK_TABLE=; \
	  CHAT_SQL_FLAGS=; CHAT_EXTRA_ENV='|CHECKPOINT_DSN=memory'; \
	fi; \
	CHAT_URL=https://documind-chat-$$PROJECT_NUMBER.$(REGION).run.app; \
	for f in $(SCRIPTS_$(PROFILE)); do \
	  echo ">> $$f (DEPLOY block)"; \
	  PROJECT=$(PROJECT) GIT_SHA=$(GIT_SHA) REGION=$(REGION) PROFILE=$(PROFILE) \
	  RESIDENCY=$(RESIDENCY) MIN_INSTANCES=$(MIN_INSTANCES) ADMIN_EMAILS=$(ADMIN_EMAILS) \
	  ADMIN_DOMAINS=$(ADMIN_DOMAINS) PROJECT_NUMBER=$$PROJECT_NUMBER \
	  DOCAI_PROCESSOR_ID=$$DOCAI_PROCESSOR_ID DOCAI_LOCATION=$$DOCAI_LOCATION \
	  RETRIEVAL_BACKEND=$$RETRIEVAL_BACKEND VECTOR_INDEX_ENDPOINT=$$VECTOR_INDEX_ENDPOINT \
	  VECTOR_DEPLOYED_INDEX_ID=$$VECTOR_DEPLOYED_INDEX_ID VECTOR_INDEX_NAME=$$VECTOR_INDEX_NAME \
	  BQ_CHUNK_TABLE=$$BQ_CHUNK_TABLE CHAT_URL=$$CHAT_URL \
	  CHAT_SQL_FLAGS="$$CHAT_SQL_FLAGS" CHAT_EXTRA_ENV="$$CHAT_EXTRA_ENV" \
	  GENERATOR_MODEL=$(GENERATOR_MODEL) RAG_MODEL_BASE=$(RAG_MODEL_BASE) ROUTING=$(ROUTING) MODEL_BACKEND=$(MODEL_BACKEND) ARMOR=$(ARMOR) \
  RETRIEVAL_CURRENT_ONLY=$(RETRIEVAL_CURRENT_ONLY) \
	  BUDGET_USD=$(BUDGET_USD) SPEND_PCT=$(SPEND_PCT) \
	  sh -ec "$$(sed -n '/^# ---- DEPLOY ----/,/^# ---- [A-Z_]* ----$$/p' $$f | grep -v '^# ----')"; \
	done
	@if echo "$(SCRIPTS_$(PROFILE))" | grep -q lesson-12.2.sh; then \
	  gcloud run services update-traffic documind-api --region $(REGION) --project $(PROJECT) --to-latest --quiet >/dev/null 2>&1 \
	    && echo ">> documind-api: 100% of traffic to the latest revision (a candidate tag had pinned it - F44)"; fi
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

# The roster is the tenancy boundary the surfaces enforce (shared/tenancy.py): a verified
# person on no roster is a 403. Needs google-cloud-firestore locally
# (pip install -r services/rag-api/requirements.txt once).
roster: guard-project
	@for m in $$(echo "$(MEMBERS)" | tr ',' ' '); do \
	  GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy add $(TENANT) $$m; \
	done
	@# The UI's service account is the bearer identity of `make smoke` and `make eval-live`
	@# (shared/iap.py's other leg; 12.2's SMOKE note), so it sits on the three golden tenants.
	@for t in acme zeta globex; do \
	  GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy add $$t \
	    documind-ui-sa@$(PROJECT).iam.gserviceaccount.com; \
	done
	@# The MCP server's account (7.2) sits on the same three rosters: it retrieves for the agent
	@# that called it, and the API checks ITS email on the tenant's roster like any other caller.
	@for t in acme zeta globex; do \
	  GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy add $$t \
	    documind-mcp-sa@$(PROJECT).iam.gserviceaccount.com; \
	done
	@# The chat service's account (12.8) is a surface like the UI's: it calls the API as itself and
	@# forwards the person's assertion when there is one. On the bearer leg - 8.7's notebook, make
	@# smoke-chat - there is no assertion, and the API checks THIS account on the tenant's roster.
	@for t in acme zeta globex; do \
	  GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy add $$t \
	    documind-chat-sa@$(PROJECT).iam.gserviceaccount.com; \
	done
	@# The A2A peer's account (8.4) sits on ONE roster, acme: a peer scoped to one tenant needs no
	@# tenant argument, and a task naming another tenant is the MCP server's refusal - the replay.
	@GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy add acme \
	    documind-agent-sa@$(PROJECT).iam.gserviceaccount.com

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
	  $(PY) evals/run_eval.py --api-url $$API

# The ablation harness (4.8, Part 5): retrieval only, no model in the loop, one knob per arm -
# the lane's dense 20 -> rerank 5 against the same without the reranker, a deeper candidate
# list, and 4.5's hybrid leg. Prints recall at the candidate depth beside every reranked
# number. ABLATE_ARGS="--limit 5" is a wiring check; --ledger ~/ablate.jsonl keeps the runs.
# Needs: pip install --user google-genai==2.21.0 google-cloud-firestore==2.30.0 #        google-cloud-discoveryengine==0.13.11 rank-bm25==0.2.2
ablate: guard-project
	$(PY) evals/ablate.py --project $(PROJECT) --region $(REGION) $(ABLATE_ARGS)

# The SQL that terraform does not own. tenant_daily is a VIEW over the log sink (12.3) and
# was, until 2026-09-05, applied by nobody - the file existed and the view did not.
bq-views: guard-project
	bq --project_id=$(PROJECT) query --use_legacy_sql=false < terraform/sql/tenant_daily.sql

# Module 5's production lane (5.5, gap G9): the feature job over the REAL chunks the ingest
# worker streams into rag_data.chunk_source, then the Dataplex quality scan on what ships.
# The scan is the gate: read its result the way 5.5's dq_gate() does before rebuilding the index.
# Full profile only - the lean one has no rag_data dataset.
features: guard-project
	@[ "$(PROFILE)" = full ] || (echo "ERROR: features needs PROFILE=full (rag_data, Dataplex)"; exit 1)
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
	  --update-env-vars "^|^GENERATOR_MODEL=$(GENERATOR_MODEL)|RAG_MODEL_BASE=$(RAG_MODEL_BASE)|ROUTING=$(ROUTING)|MODEL_BACKEND=$(MODEL_BACKEND)|ARMOR=$(ARMOR)|RETRIEVAL_CURRENT_ONLY=$(RETRIEVAL_CURRENT_ONLY)$(if $(GENERATOR_LOCATION),|GENERATOR_LOCATION=$(GENERATOR_LOCATION),)" $(if $(GENERATOR_LOCATION),,--remove-env-vars GENERATOR_LOCATION)
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	echo ">> candidate: https://candidate---documind-api-$$NUMBER.$(REGION).run.app (no traffic; remove with gcloud run services update-traffic documind-api --remove-tags candidate)"

# Eval CANDIDATES from the feed, PII-scanned, into evals/golden_generated.jsonl (5.5 step 11).
# Not a golden set: review, cut, merge the survivors into golden.jsonl by hand.
make-evalset: guard-project
	$(PY) evals/make_evalset.py --project $(PROJECT) --tenant $(TENANT) --rows $${ROWS:-200}

smoke:
	$(PY) smoke/smoke.py

# The MCP server's smoke test (7.2): health, the four tools, a cited answer as a roster member,
# and the outsider account refused by the roster. Needs `pip install --user fastmcp==3.4.7`.
smoke-mcp: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
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
down: guard-project
	cd $(TF_DIR) && $(TF_INIT) && \
	  terraform destroy -input=false -auto-approve $(TF_VARS)
	@echo ">> Also delete the throwaway project to stop all billing:"
	@echo ">>   gcloud projects delete $(PROJECT)"

# SLM_REGION is deliberately NOT $(REGION)-bound to Mumbai: Cloud Run L4 in asia-south1 is
# INVITATION-ONLY (11.1). us-central1 is the self-serve default; asia-south2 (Delhi) has
# RTX PRO 6000 GA if the data must stay in India, at a different price.
SLM_REGION ?= us-central1

# ---------- Module 11: the backend is a setting, the gateway is a route, the GPU is a bill ----------

# The LiteLLM gateway (11.3) on the lean lane: CPU-only, scale to zero, behind IAM like every service - the API's
# and the UI's accounts may invoke it. No database and no master key on lean (GATEWAY_CONFIG=config.yaml is the
# full profile). Its token proxy fronts the SLM (and the vLLM engine when built) with an ID token per call.
deploy-gateway: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	SLM=https://documind-slm-$$NUMBER.$(SLM_REGION).run.app; VLLM=$${GEMMA_VLLM_URL:-https://documind-vllm-$$NUMBER.$(SLM_REGION).run.app}; \
	gcloud run deploy documind-gateway --source services/litellm --region $(REGION) --project $(PROJECT) \
	  --service-account documind-gateway-sa@$(PROJECT).iam.gserviceaccount.com --no-allow-unauthenticated \
	  --memory 2Gi --cpu 2 --cpu-boost --min-instances 0 --max-instances 3 --concurrency 20 --timeout 300 \
	  --set-env-vars "^|^VERTEXAI_PROJECT=$(PROJECT)|GOOGLE_CLOUD_PROJECT=$(PROJECT)|SLM_URL=$$SLM|GEMMA_VLLM_URL=$$VLLM|LITELLM_CONFIG=$(GATEWAY_CONFIG)|ROUTER_ENFORCE=$(ROUTER_ENFORCE)" --quiet; \
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

# 11.5's one-hour comparison: the Autopilot cluster on the lane's VPC, the vLLM manifest with the image make
# build-vllm pushed, one request through a port-forward - and gke-down, because an empty cluster still bills.
gke-up: guard-project
	cd $(TF_DIR) && $(TF_INIT) && terraform apply -input=false -auto-approve $(TF_VARS) -var gke_cluster=true \
	  -target=google_container_cluster.autopilot
	gcloud container clusters get-credentials documind-autopilot --region $(REGION) --project $(PROJECT)
	IMAGE=$(IMAGE_REPO)/gemma-vllm:latest envsubst < gke/vllm-deployment.yaml | kubectl apply -f -
	@echo ">> kubectl get pods -w   (an accelerator node takes minutes); then: kubectl port-forward svc/documind-vllm 8080:80"

gke-down: guard-project
	-IMAGE=$(IMAGE_REPO)/gemma-vllm:latest envsubst < gke/vllm-deployment.yaml | kubectl delete -f - --ignore-not-found
	cd $(TF_DIR) && $(TF_INIT) && terraform destroy -input=false -auto-approve $(TF_VARS) -var gke_cluster=true \
	  -target=google_container_cluster.autopilot
	@echo "documind-autopilot deleted - the cluster fee stops with it"

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
	@if gcloud container clusters describe documind-autopilot --region $(REGION) --project $(PROJECT) >/dev/null 2>&1; then \
	  $(MAKE) gke-down PROJECT=$(PROJECT); else echo "documind-autopilot: absent"; fi
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
release-candidate: guard-project
	gcloud run services update documind-api --region $(REGION) --project $(PROJECT) --image $(IMAGE_REPO)/api:$(GIT_SHA) --no-traffic --tag candidate --quiet
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	echo ">> candidate: https://candidate---documind-api-$$NUMBER.$(REGION).run.app  (make eval-live API=that, then make promote)"

promote: guard-project
	gcloud run services update-traffic documind-api --region $(REGION) --project $(PROJECT) --to-latest --quiet
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); URL=https://documind-api-$$NUMBER.$(REGION).run.app; \
	TOKEN=$$(gcloud auth print-identity-token --include-email --impersonate-service-account=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com --audiences=$$URL); \
	echo ">> live:"; curl -s -H "Authorization: Bearer $$TOKEN" $$URL/version; echo

# The revision before the newest, back on 100%. Fifteen seconds; the fastest rollback there is, because that revision
# never went away. GET /version afterwards says which sha is answering.
rollback: guard-project
	@PREV=$$(gcloud run revisions list --service documind-api --region $(REGION) --project $(PROJECT) --sort-by=~metadata.creationTimestamp --format='value(name)' --limit 2 | tail -1); \
	gcloud run services update-traffic documind-api --region $(REGION) --project $(PROJECT) --to-revisions $$PREV=100 --quiet && echo ">> traffic on $$PREV"

# 12.8's full smoke: the seven smokes in one run, with the exports smoke.py reads. A refusal line is a PASS - a system
# that answers everybody passes a smoke made only of "does it work?".
smoke-all: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); rc=0; \
	for t in smoke smoke-mcp smoke-chat smoke-agent smoke-media smoke-gateway smoke-slm; do echo "== $$t"; \
	  DOCUMIND_PROJECT=$(PROJECT) DOCUMIND_API_URL=https://documind-api-$$NUMBER.$(REGION).run.app DOCUMIND_TENANT=$(TENANT) \
	  DOCUMIND_IMPERSONATE_SA=documind-ui-sa@$(PROJECT).iam.gserviceaccount.com $(MAKE) --no-print-directory $$t PROJECT=$(PROJECT) 2>&1 | grep -E "PASS|FAIL|pass|fail|not set" || rc=1; done; exit $$rc

# 12.3 on lean: tenant_daily's GROUP BY over the usage rows in Cloud Logging (evals/usage_rows.py).
usage: guard-project
	$(PY) evals/usage_rows.py --project $(PROJECT) --hours $${HOURS:-24}

# 12.5's live cells. ingest-one puts ONE object under the tenant's prefix and waits for the worker's ingest_ok line;
# poison puts a zero-byte object in (the worker's 400 and its ingest_poison line); dlq peeks at the dead-letter
# subscription without acking - a poison message lands there after five failed deliveries, minutes later.
ingest-one: guard-project
	@test -n "$(FILE)" || (echo "FILE=<path> is required (TENANT=$(TENANT))"; exit 2); SINCE=$$(date -u +%Y-%m-%dT%H:%M:%SZ); NAME=$$(basename "$(FILE)"); \
	gcloud storage cp "$(FILE)" gs://$(PROJECT)-uploads/$(TENANT)/$$NAME && echo ">> gs://$(PROJECT)-uploads/$(TENANT)/$$NAME - waiting for the worker (up to 5 min)"; \
	for i in $$(seq 1 30); do sleep 10; \
	  LINE=$$(gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-ingest\" AND jsonPayload.event=\"ingest_ok\" AND jsonPayload.tenant=\"$(TENANT)\" AND timestamp>=\"$$SINCE\"" --project $(PROJECT) --limit 1 --format='value(jsonPayload.doc_key,jsonPayload.chunks,jsonPayload.pages)'); \
	  if [ -n "$$LINE" ]; then echo ">> indexed: $$LINE"; exit 0; fi; done; echo ">> no ingest_ok in 5 min - gcloud logging read ... service_name=documind-ingest"; exit 1

poison: guard-project
	@SINCE=$$(date -u +%Y-%m-%dT%H:%M:%SZ); NAME=poison-$$(date +%s).pdf; : > /tmp/$$NAME; \
	gcloud storage cp /tmp/$$NAME gs://$(PROJECT)-uploads/$(TENANT)/$$NAME && echo ">> zero-byte object in: $$NAME"; \
	for i in $$(seq 1 12); do sleep 10; \
	  LINE=$$(gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-ingest\" AND jsonPayload.event=\"ingest_poison\" AND timestamp>=\"$$SINCE\"" --project $(PROJECT) --limit 1 --format='value(jsonPayload.error)'); \
	  if [ -n "$$LINE" ]; then echo ">> the worker refused it (400): $$LINE"; echo ">> Pub/Sub retries a non-2xx five times with backoff, then ingest-dlq: make dlq in a few minutes"; exit 0; fi; done; echo ">> no ingest_poison line in 2 min"; exit 1

dlq: guard-project
	gcloud pubsub subscriptions pull ingest-dlq-sub --project $(PROJECT) --limit 5 --format='table(message.messageId,message.attributes.objectId,message.attributes.eventTime,deliveryAttempt)'

# ---------- 12.5's ledger (11 September 2026): one current version per document ----------
# reindex re-issues ONE document under an object name (NAME= overrides the basename: the shape-A demo re-uploads
# evals/demo/hr_policy_2026_v2.md AS hr_policy_2026.md) and waits for the worker's line - ingest_ok with the
# retired count, or ingest_reactivated when the bytes are a version the ledger had retired (the undo).
reindex: guard-project
	@test -n "$(FILE)" || (echo "FILE=<path> is required (TENANT=$(TENANT), NAME=<object name, default the basename>)"; exit 2); SINCE=$$(date -u +%Y-%m-%dT%H:%M:%SZ); OBJ=$(if $(NAME),$(NAME),$$(basename "$(FILE)")); \
	gcloud storage cp "$(FILE)" gs://$(PROJECT)-uploads/$(TENANT)/$$OBJ && echo ">> gs://$(PROJECT)-uploads/$(TENANT)/$$OBJ - waiting for the worker (up to 5 min)"; \
	for i in $$(seq 1 30); do sleep 10; \
	  LINE=$$(gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-ingest\" AND (jsonPayload.event=\"ingest_ok\" OR jsonPayload.event=\"ingest_reactivated\") AND jsonPayload.tenant=\"$(TENANT)\" AND timestamp>=\"$$SINCE\"" --project $(PROJECT) --limit 1 --format='value(jsonPayload.event,jsonPayload.doc_key,jsonPayload.chunks,jsonPayload.retired,jsonPayload.effective_from)'); \
	  if [ -n "$$LINE" ]; then echo ">> $$LINE"; \
	    gcloud logging read "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-ingest\" AND jsonPayload.event=\"ingest_superseded\" AND jsonPayload.tenant=\"$(TENANT)\" AND timestamp>=\"$$SINCE\"" --project $(PROJECT) --limit 1 --format='value(jsonPayload.retired_doc_keys,jsonPayload.retired_chunks)' | sed 's/^/>> retired: /'; exit 0; fi; done; echo ">> no ingest line in 5 min - gcloud logging read ... service_name=documind-ingest"; exit 1

# retire a source by hand (SOURCE=acme/old.pdf or a gs:// URI): every current chunk of it is flagged, the ledger
# row says retired, the tenant's cache record goes. Uploading the bytes again reactivates them.
retire: guard-project
	@test -n "$(SOURCE)" || (echo "SOURCE=<tenant/name or gs://...> is required"; exit 2); \
	$(PY) services/ingest/reconcile.py --project $(PROJECT) --retire "$(SOURCE)" --apply

# the full reconciliation: the bucket's current generations against sources/. Prints the plan; APPLY=1 acts.
reconcile: guard-project
	$(PY) services/ingest/reconcile.py --project $(PROJECT) $(if $(TENANT_ONLY),--tenant $(TENANT_ONLY),) $(if $(APPLY),--apply,)

# once, after the ledger ships onto a lane that predates it: chunks and documents get current=true, a doc_key and
# a sources/ row each; then RETRIEVAL_CURRENT_ONLY=on is safe to flip (on a candidate first).
backfill-current: guard-project
	$(PY) services/ingest/reconcile.py --project $(PROJECT) --backfill --apply

# the nightly job, from the ingest image the lane already runs; then RECONCILE_JOB=true on the next make plan
# schedules it (reconcile.tf) at 23:30 IST, after documind-off.
reconcile-job: guard-project
	gcloud run jobs deploy documind-reconcile --image $(IMAGE_REPO)/ingest:$(GIT_SHA) --region $(REGION) --project $(PROJECT) \
	  --service-account documind-ingest-sa@$(PROJECT).iam.gserviceaccount.com \
	  --set-env-vars GOOGLE_CLOUD_PROJECT=$(PROJECT),AUDIT_BUCKET=$(PROJECT)-audit \
	  --command python --args reconcile.py,--project,$(PROJECT),--apply --max-retries 0 --task-timeout 1200 --quiet
	@echo ">> documind-reconcile: gcloud run jobs execute documind-reconcile --region $(REGION) to run it now; RECONCILE_JOB=true on make plan to schedule it"
