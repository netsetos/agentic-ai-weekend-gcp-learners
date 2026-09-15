# Reference commands extracted from 12.1 (GCP_Capstone_12.1_InfraSetup.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- ENABLE_APIS ----
gcloud config set project $PROJECT

# Two calls, not one: the Service Usage API takes at most 20 services per request
# (SU_MAX_BATCH_SIZE_EXCEEDED), and this list is 36 - Vision, Natural Language and
# Translation joined it with Module 9 (9.3), Vector Search with the managed mirror
# (P9, 13 September 2026: it backs 4.3's serverless RAG Engine corpora). Both are idempotent.
gcloud services enable \
  run.googleapis.com \
  compute.googleapis.com \
  vpcaccess.googleapis.com \
  pubsub.googleapis.com \
  artifactregistry.googleapis.com \
  secretmanager.googleapis.com \
  firestore.googleapis.com \
  storage.googleapis.com \
  aiplatform.googleapis.com \
  documentai.googleapis.com \
  vision.googleapis.com \
  language.googleapis.com \
  translate.googleapis.com \
  speech.googleapis.com \
  texttospeech.googleapis.com \
  dlp.googleapis.com \
  iap.googleapis.com \
  iamcredentials.googleapis.com \
  cloudbuild.googleapis.com

gcloud services enable \
  cloudtrace.googleapis.com \
  monitoring.googleapis.com \
  logging.googleapis.com \
  billingbudgets.googleapis.com \
  bigquery.googleapis.com \
  discoveryengine.googleapis.com \
  dataplex.googleapis.com \
  sqladmin.googleapis.com \
  eventarc.googleapis.com \
  workflows.googleapis.com \
  cloudscheduler.googleapis.com \
  cloudfunctions.googleapis.com \
  modelarmor.googleapis.com \
  cloudbilling.googleapis.com \
  cloudresourcemanager.googleapis.com \
  serviceusage.googleapis.com \
  vectorsearch.googleapis.com

# ---- APPLY ----
# Dry-run first
terraform init -reconfigure -backend-config="bucket=documind-ai-YOUR-ID-tfstate"
terraform plan -out=tfplan \
  -var=project_id=documind-ai-YOUR-ID \
  -var=billing_account_id=YOUR-BILLING-ID \
  -var=github_repository_id=1358872052

# Apply when plan is green
terraform apply tfplan

# Smoke tests
gcloud iam service-accounts list --filter="email~documind-.*-sa"
gcloud artifacts repositories describe documind --location=us-central1
gcloud firestore databases list
gsutil ls -b gs://documind-ai-YOUR-ID-uploads
gcloud secrets list --filter=name~litellm
gcloud billing budgets list --billing-account=YOUR-BILLING-ID

