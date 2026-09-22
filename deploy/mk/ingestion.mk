# ---------- Ingestion (the v5 course's Module 3): the roster and the tenant pins, the vector tier, the ingest drills ----------
# Included by the Makefile (`include mk/*.mk`); every variable it uses is declared there. The drills that upload
# and wait are scripts under commands/ (the same operation without make); the roster is commands/lane.py roster.

# The roster is the tenancy boundary the surfaces enforce (shared/tenancy.py): a verified
# person on no roster is a 403. Needs google-cloud-firestore locally
# (pip install -r services/rag-api/requirements.txt once).
roster: guard-project
	@GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) commands/lane.py roster --project $(PROJECT) --tenant $(TENANT) --members "$(MEMBERS)"

# Where ONE tenant's text may be held: tenant_settings/{TENANT}.data_region (shared/tenancy.py). `in` keeps it on
# the kit's rows (asia-south1); `any` lets the managed mirror copy its current versions into RAG Engine (us-central1)
# and Vertex AI Search (global) and the API read them (RETRIEVAL_BACKEND, or the tenant's own retrieval_backend
# pin). No service can write this field; without DATA_REGION the target prints the tenant's current policy.
tenant-policy: guard-project
	@test -n "$(TENANT)" || (echo "TENANT=<id> is required"; exit 2); \
	GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy policy $(TENANT) $(DATA_REGION)

# Which store answers ONE tenant's questions: tenant_settings/{TENANT}.retrieval_backend (shared/tenancy.py), the
# way 11.4 pins a model. vector | firestore | rag_engine | vertex_search; `default` clears the pin; empty prints it.
# Held against the tenant's data_region on every request (rag-api main.py: an `in` tenant pinned to a managed
# store is served from the kit's index with policy_fallback=1 on the row), so a pin cannot move text the policy
# keeps home. Read once a minute per tenant - no redeploy.
tenant-backend: guard-project
	@test -n "$(TENANT)" || (echo "TENANT=<id> is required"; exit 2); \
	GOOGLE_CLOUD_PROJECT=$(PROJECT) $(PY) -m shared.tenancy backend $(TENANT) $(RETRIEVAL_BACKEND)

# `make up` returns with the index created and EMPTY - correct, vector.tf names no batch input and
# the worker fills it - so this is the line to run after make ingest-corpus, and the line to run
# before claiming a Vector Search demonstration. vectorsCount is the index's own number; a streamed
# upsert takes a few minutes to appear in it. The answer's stages.vector_chunks is the other half
# of the proof: 0 while stages.retrieval_backend is `vector` means the Firestore rung answered.
vector-status: guard-project
	@PROJECT=$(PROJECT) REGION=$(REGION) TF_DIR=$(TF_DIR) bash commands/vector-status.sh

# Block until the tier holds vectors, or say why not. WANT= the floor (default 1); 20 minutes at most.
wait-vectors: guard-project
	@PROJECT=$(PROJECT) REGION=$(REGION) TF_DIR=$(TF_DIR) WANT="$(WANT)" bash commands/wait-vectors.sh

# The ANN tier from the rows. Firestore holds every embedding - that is what the chaos rung reads - so a
# tier that is empty when the corpus is not costs one pass over the rows and no model call. The state
# this repairs: a worker deployed before vector.tf existed, or an apply that lost its index and succeeded
# on the second run. Prints the count; APPLY=1 acts. TENANT_ONLY= one tenant.
backfill-vectors: guard-project
	PYTHONPATH=.:services/ingest GOOGLE_CLOUD_PROJECT=$(PROJECT) \
	VECTOR_INDEX_NAME=$$(cd $(TF_DIR) && terraform output -raw vector_index_name) \
	  $(PY) services/ingest/reconcile.py --project $(PROJECT) --backfill-vectors \
	  $(if $(TENANT_ONLY),--tenant $(TENANT_ONLY),) $(if $(APPLY),--apply,)

# 12.5's live cells. ingest-one puts ONE object under the tenant's prefix and waits for the worker's ingest_ok line;
# poison puts a zero-byte object in (the worker's 400 and its ingest_poison line); dlq peeks at the dead-letter
# subscription without acking - a poison message lands there after twelve failed deliveries (eventarc.tf), about an hour later.
ingest-one: guard-project
	@PROJECT=$(PROJECT) TENANT=$(TENANT) FILE="$(FILE)" bash commands/ingest-one.sh

poison: guard-project
	@PROJECT=$(PROJECT) TENANT=$(TENANT) bash commands/poison.sh

dlq: guard-project
	gcloud pubsub subscriptions pull ingest-dlq-sub --project $(PROJECT) --limit 5 --format='table(message.messageId,message.attributes.objectId,message.attributes.eventTime,deliveryAttempt)'
