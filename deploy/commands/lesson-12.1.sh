# Reference commands extracted from 12.1 (GCP_Capstone_12.1_InfraSetup.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- ENABLE_APIS ----
gcloud config set project $PROJECT

# Two calls, not one: the Service Usage API takes at most 20 services per request
# (SU_MAX_BATCH_SIZE_EXCEEDED), and this list is 40 - Vision, Natural Language and
# Translation joined it with Module 9 (9.3), Vector Search with the managed mirror
# (P9, 13 September 2026: it backs 4.3's serverless RAG Engine corpora), and Spanner, GKE,
# Cloud Deploy and Org Policy with the one shape (15 September 2026: spanner.tf, gke.tf,
# clouddeploy.tf and org_policy.tf declare resources those APIs serve). Both are idempotent.
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
  cloudbuild.googleapis.com \
  orgpolicy.googleapis.com

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
  vectorsearch.googleapis.com \
  spanner.googleapis.com \
  container.googleapis.com \
  clouddeploy.googleapis.com

# ---- APPLY ----
# Run from deploy/ (or deploy_module_rag), using the intended existing PROJECT/REGION.
# First enable APIs above and initialize terraform/ with the original state bucket,
# prefix and workspace. See INFRASTRUCTURE.md for new-project setup and recovery.
# A new CI deployment also needs an explicit repository name, numeric ID and branch
# via infrastructure.py prepare. Existing deployments preserve their current CI trust.

# Read-only planning: reads linked billing, persists confirmed inputs, and refuses
# deletion/replacement or a CI trust migration before selecting the saved plan.
python commands/infrastructure.py plan \
  --project="${PROJECT:?Set the intended project ID}" \
  --region="${REGION:?Set the deployment region}"

# Only after reviewing the complete successful plan, run this separate command.
python commands/infrastructure.py apply --project="$PROJECT" --region="$REGION"

# Smoke tests (read-only)
gcloud iam service-accounts list --project="$PROJECT" --filter="email~documind-.*-sa"
gcloud artifacts repositories describe documind --project="$PROJECT" --location="$REGION"
gcloud firestore databases list --project="$PROJECT"
gcloud storage buckets describe "gs://$PROJECT-uploads"
gcloud secrets list --project="$PROJECT" --filter=name~litellm

