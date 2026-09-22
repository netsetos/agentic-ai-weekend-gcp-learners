# ---------- Lifecycle (Module 4): 12.5's ledger - one current version per document - and the batch lane's consumer ----------
# Included by the Makefile (`include mk/*.mk`). reindex is commands/reindex.sh; the rest call the kit's own
# Python (services/ingest/reconcile.py, batch.py) and run the same way without make: commands/lane.py.

# reindex re-issues ONE document under an object name (NAME= overrides the basename: the shape-A demo re-uploads
# evals/demo/hr_policy_2026_v2.md AS hr_policy_2026.md) and waits for the worker's line - ingest_ok with the
# retired count, or ingest_reactivated when the bytes are a version the ledger had retired (the undo).
reindex: guard-project
	@PROJECT=$(PROJECT) TENANT=$(TENANT) FILE="$(FILE)" NAME="$(NAME)" PY="$(PY)" bash commands/reindex.sh

# withdraw a source by hand (SOURCE=acme/old.pdf or a gs:// URI): every current chunk of it is flagged with an
# expire_at and the ledger row says WITHDRAWN (12 September 2026) - a tombstone, not `retired`. The object stays in
# the bucket, the nightly reconcile leaves it there ("withdrawn, object kept"), the same bytes again are acked
# (ingest_withdrawn) and never reactivated, the corpus fingerprint moves so the caches follow. Before this the row
# said retired, and the next night's walk read "retired but back in the bucket" and resurrected what a person had
# just taken down. make restore is the way back.
retire: guard-project
	@test -n "$(SOURCE)" || (echo "SOURCE=<tenant/name or gs://...> is required"; exit 2); \
	PYTHONPATH=. MANAGED_MIRROR=$(MANAGED_MIRROR) RAG_LOCATION=$(RAG_LOCATION) AUDIT_BUCKET=$(PROJECT)-audit GOOGLE_CLOUD_PROJECT=$(PROJECT) \
	  $(PY) services/ingest/reconcile.py --project $(PROJECT) --retire "$(SOURCE)" --apply

# bring a withdrawn source back (SOURCE= as for retire): the tombstone is cleared and the object rewritten onto
# itself, so the worker's own path returns it - ingest_reactivated inside the RETENTION_DAYS undo window (nothing
# embedded), ingest_ok as a fresh version after it. Refused, with the reason, for a source that is not withdrawn or
# whose object is gone from the bucket.
restore: guard-project
	@test -n "$(SOURCE)" || (echo "SOURCE=<tenant/name or gs://...> is required"; exit 2); \
	$(PY) services/ingest/reconcile.py --project $(PROJECT) --restore "$(SOURCE)" --apply

# the full reconciliation: the bucket's current generations against sources/. Prints the plan; APPLY=1 acts.
reconcile: guard-project
	PYTHONPATH=. MANAGED_MIRROR=$(MANAGED_MIRROR) RAG_LOCATION=$(RAG_LOCATION) AUDIT_BUCKET=$(PROJECT)-audit GOOGLE_CLOUD_PROJECT=$(PROJECT) \
	  $(PY) services/ingest/reconcile.py --project $(PROJECT) $(if $(TENANT_ONLY),--tenant $(TENANT_ONLY),) $(if $(APPLY),--apply,)

# once, after the ledger ships onto a lane that predates it: chunks and documents get current=true, a doc_key and
# a sources/ row each; then RETRIEVAL_CURRENT_ONLY=on is safe to flip (on a candidate first).
backfill-current: guard-project
	$(PY) services/ingest/reconcile.py --project $(PROJECT) --backfill --apply

# the nightly job, from the ingest image the lane already runs; then RECONCILE_JOB=true on the next make plan
# schedules it (reconcile.tf) at 23:30 IST, after documind-off.
reconcile-job: RECONCILE_JOB = true
reconcile-job: guard-project
	cd $(TF_DIR) && $(TF_INIT) && terraform apply -input=false -auto-approve $(TF_VARS)
	@echo ">> documind-reconcile declared on $(RECONCILE_IMAGE) and scheduled 23:30 IST (reconcile.tf); gcloud run jobs execute documind-reconcile --region $(REGION) --project $(PROJECT) runs it now"
	@echo ">> keep RECONCILE_JOB=true on every later make plan / make up, or the next apply removes the job and its schedule"

# the versions view from the shell: every source's current version, generation, what the last reindex cost, the date
# it declares, the corpus fingerprint - the rows GET /v1/sources serves and the UI's Documents page renders.
sources: guard-project
	$(PY) services/ingest/reconcile.py --project $(PROJECT) --report $(if $(TENANT_ONLY),--tenant $(TENANT_ONLY),) $(if $(JSON),--json,)

# the manual twin of the TTL policy (firestore_indexes.tf): retired rows past expire_at. Prints; APPLY=1 deletes. On a
# lane with the policy applied it finds nothing to do, which is the point of running it once.
purge: guard-project
	$(PY) services/ingest/reconcile.py --project $(PROJECT) --purge $(if $(TENANT_ONLY),--tenant $(TENANT_ONLY),) $(if $(APPLY),--apply,)

# the document lifecycle, smoked like every surface: a small fixture in twice under one name (reindexed with the
# carry-over counts, the answer moved), then version 1 again (reactivated, nothing embedded, the answer back).
smoke-reindex: guard-project
	@NUMBER=$$(gcloud projects describe $(PROJECT) --format='value(projectNumber)'); \
	DOCUMIND_PROJECT=$(PROJECT) DOCUMIND_API_URL=$${DOCUMIND_API_URL:-https://documind-api-$$NUMBER.$(REGION).run.app} DOCUMIND_TENANT=$(TENANT) \
	DOCUMIND_IMPERSONATE_SA=$${DOCUMIND_IMPERSONATE_SA:-documind-ui-sa@$(PROJECT).iam.gserviceaccount.com} $(PY) smoke/smoke_reindex.py

# A document over MAX_INLINE_PAGES is queued by the worker (ingest_batch/{doc_key}, the claim says queued) and indexed by
# documind-ingest-batch: a Cloud Run job on the ingest image (batch.tf), which the worker starts as it queues (BATCH_JOB
# in its environment, set by deploy-services once BATCH_JOB=true) and an hourly schedule backstops. batch-job is an apply
# with the switch on, like reconcile-job; batch starts the job now and waits; queued lists the queue from the shell
# (needs google-cloud-firestore, like make roster). Keep BATCH_JOB=true on every later make plan / make up.
batch-job: BATCH_JOB = true
batch-job: guard-project
	cd $(TF_DIR) && $(TF_INIT) && terraform apply -input=false -auto-approve $(TF_VARS)
	@echo ">> documind-ingest-batch declared on $(RECONCILE_IMAGE) and scheduled hourly (batch.tf); the worker starts it as it queues once"
	@echo ">> make deploy-services BATCH_JOB=true tells the worker the job's name (BATCH_JOB in commands/lesson-12.5.sh)"

batch: guard-project
	gcloud run jobs execute documind-ingest-batch --region $(REGION) --project $(PROJECT) --wait

queued: guard-project
	cd services/ingest && GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) batch.py --project $(PROJECT) --queued
