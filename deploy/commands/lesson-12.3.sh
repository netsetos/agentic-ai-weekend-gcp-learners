# Reference commands extracted from 12.3 (GCP_Capstone_12.3_AdminObservability.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- DEPLOY ----
# documind-admin is its OWN service and its OWN image. It used to deploy ui:$GIT_SHA,
# which is built from services/frontend/ only - so services/admin/*.py was in no
# image at all and this service came up as a second copy of the chat UI. Deployed,
# healthy, reachable, and not the code you wrote.
#
# Build context is deploy/, not services/admin/, so shared/ (pii.py, audit_log.py)
# ships with it - the same modules the ingest worker scans with.
gcloud builds submit \
  --tag=us-central1-docker.pkg.dev/$PROJECT/documind/admin:$GIT_SHA \
  --file=services/admin/Dockerfile .

gcloud run deploy documind-admin \
  --image=us-central1-docker.pkg.dev/$PROJECT/documind/admin:$GIT_SHA \
  --region=us-central1 --no-allow-unauthenticated \
  --ingress=internal-and-cloud-load-balancing \
  --service-account=documind-admin-sa@$PROJECT.iam.gserviceaccount.com \
  --set-env-vars=^:^GOOGLE_CLOUD_PROJECT=$PROJECT:ADMIN_EMAILS=alice@documind.ai,bob@documind.ai:AUDIT_BUCKET=$PROJECT-audit:IAP_AUDIENCE=$IAP_AUDIENCE \
  --set-secrets="COOKIE_SECRET=cookie-secret:latest" \
  --session-affinity --cpu-boost

# IAP group restricted to admin-group@documind.ai
gcloud beta run services update documind-admin --region=us-central1 --iap
gcloud beta iap web add-iam-policy-binding \
  --resource-type=cloud-run --service=documind-admin --region=us-central1 \
  --member="group:admin-group@documind.ai" \
  --role="roles/iap.httpsResourceAccessor"

# Admin SA needs BQ data viewer + Firestore user
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:documind-admin-sa@$PROJECT.iam.gserviceaccount.com" \
  --role="roles/bigquery.dataViewer"
gcloud projects add-iam-policy-binding $PROJECT \
  --member="serviceAccount:documind-admin-sa@$PROJECT.iam.gserviceaccount.com" \
  --role="roles/bigquery.jobUser"

# ---- SMOKE ----
# 1. Sink is writing
bq query --use_legacy_sql=false \
  "SELECT COUNT(*) FROM documind_observability.run_googleapis_com_stdout \
   WHERE timestamp > TIMESTAMP_SUB(CURRENT_TIMESTAMP(), INTERVAL 1 HOUR)"
# -> positive integer = sink healthy

# 2. DLP finds PII in a test doc
python -c "from dlp import inspect_and_log; print(inspect_and_log(\
  'test_chunk', 'tenant-acme', 'Contact raj@example.in PAN ABCDE1234F'))"
# -> {"has_pii": true, "types": ["EMAIL_ADDRESS","INDIA_PAN_INDIVIDUAL"]}

# 3. Audit emit writes to GCS
python -c "from audit import emit; print(emit(\
  "doc.upload", {"email":"alice@acme.in","tenant_id":"tenant-acme"},\
  {"type":"doc","id":"doc_9f23"}))"
gsutil ls gs://$PROJECT-audit/$(date +%Y)/$(date +%m)/$(date +%d)/tenant-acme/

# 4. Non-admin user cannot see admin tab
# Open documind-admin Cloud Run URL as a non-admin user -> IAP 403.

# 5. Alert fires on synthetic latency spike
hey -n 200 -c 20 -m POST -H "Authorization: Bearer $TOK" \
  https://documind-api-xxx.run.app/v1/query
# Wait 6 min, PagerDuty page arrives if p95 > 3000ms

