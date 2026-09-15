# Reference commands extracted from 12.5 (GCP_Capstone_12.5_Ingestion.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- DEPLOY ----
# The ingest worker. Reached by nothing but the push subscription in eventarc.tf, which
# delivers as documind-ingest-sa with an OIDC token - hence internal ingress, no
# unauthenticated calls, and the invoker grant to that account below. Concurrency 1 (see the
# Dockerfile), and a 600-second request ceiling to match the subscription's ack deadline:
# a hundred-page Act is a dozen Document AI calls and fits. Thirty instances, so a corpus
# upload of thirty objects is not a queue of cold-start refusals.
#
# RESIDENCY picks the processor the way docai.tf did (us: Layout Parser; india: OCR in Mumbai).
# VECTOR_INDEX_NAME and BQ_CHUNK_TABLE come from the state (vector.tf, dataplex.tf): the datapoint
# upsert and the BigQuery mirror run on every ingest, and the Firestore index is always written too.
# 12 September 2026 (deploy/INDEXING.md): RETENTION_DAYS stamps expire_at on every retired row - the TTL policy's
# number, variables.tf's retention_days; EMBEDDING_MODEL / EMBEDDING_VERSION are the ONE declared embedding, stamped
# on every chunk row - the API embeds its queries with the same pair. make deploy-services passes all three.
# 13 September 2026: REGION and BATCH_JOB are the batch lane's consumer - the Cloud Run job batch.tf declares on this
# image (make batch-job), which the worker starts by name the moment it queues a document over MAX_INLINE_PAGES;
# empty until make deploy-services BATCH_JOB=true, and then the hourly schedule alone drains the queue.
# P9 (13 September 2026): MANAGED_MIRROR is the managed mirror - the version the worker swaps current copied into
# the tenant's RAG Engine corpus (4.3) and / or Vertex AI Search data store (4.4), managed.py; off on the lane.
# Which TENANTS it copies is each tenant's data_region (tenant_settings, make tenant-policy), not this env.
# RAG_LOCATION is the corpora's region (serverless corpora: us-central1 only).
gcloud run deploy documind-ingest \
  --image=${REGION:-us-central1}-docker.pkg.dev/$PROJECT/documind/ingest:$GIT_SHA \
  --region=${REGION:-us-central1} --platform=managed \
  --no-allow-unauthenticated \
  --ingress=internal \
  --memory=2Gi --cpu=2 --concurrency=1 --timeout=600 \
  --min-instances=0 --max-instances=30 \
  --execution-environment=gen2 \
  --service-account=documind-ingest-sa@$PROJECT.iam.gserviceaccount.com \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=$PROJECT|RESIDENCY=${RESIDENCY:-us}|DOCAI_PROCESSOR_ID=$DOCAI_PROCESSOR_ID|AUDIT_BUCKET=$PROJECT-audit|VECTOR_INDEX_NAME=$VECTOR_INDEX_NAME|BQ_CHUNK_TABLE=$BQ_CHUNK_TABLE|RETENTION_DAYS=${RETENTION_DAYS-30}|EMBEDDING_MODEL=${EMBEDDING_MODEL-text-embedding-005}|EMBEDDING_VERSION=${EMBEDDING_VERSION-1}|REGION=${REGION:-us-central1}|BATCH_JOB=${BATCH_JOB_NAME-}|MANAGED_MIRROR=${MANAGED_MIRROR-off}|RAG_LOCATION=${RAG_LOCATION-us-central1}"

# Pub/Sub calls the worker AS the ingest service account; the account has to be allowed in.
# The subscription already exists, pointing at this service's deterministic URL.
gcloud run services add-iam-policy-binding documind-ingest \
  --region=${REGION:-us-central1} --project=$PROJECT \
  --member="serviceAccount:documind-ingest-sa@$PROJECT.iam.gserviceaccount.com" \
  --role=roles/run.invoker

