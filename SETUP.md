# Setup

Two ways to run the notebooks: **Colab** (nothing to install) or **local Jupyter**. Both authenticate to GCP with
Application Default Credentials; there are no API keys to manage anywhere in the course.

## Step 0 - the GCP project, once

This is lesson 1.1, condensed. Do it once, before Module 1.

```bash
# 1. The project
gcloud projects create documind-ai-YOUR-ID --name="DocuMind AI Capstone"
gcloud config set project documind-ai-YOUR-ID

# 2. Billing (required even for free-tier services)
gcloud billing projects link documind-ai-YOUR-ID \
  --billing-account=$(gcloud billing accounts list --format="value(name)" --limit=1)
```

```bash
# Enable the same 40 APIs as deploy/commands/lesson-12.1.sh.
# Service Usage accepts at most twenty services per call.
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

# Wait for propagation (IAM can take up to 60s)
echo "Waiting 60s for API propagation..."
sleep 60

# Verify all APIs are enabled
gcloud services list --enabled --format="table(name)"
```

```bash
# 4. Default regions for the course (generation is global; these are for the regional services)
gcloud config set compute/region us-central1
gcloud config set run/region us-central1
```

Set a budget alert before you run anything (lesson 1.1, step 3). The kit's `make preflight` (from `deploy/` in this
repo) checks all of this read-only.

## Colab

Every notebook starts with the same two cells: the pinned installs, then

```python
from google.colab import auth
auth.authenticate_user()
```

Change `PROJECT_ID = "documind-ai-YOUR-ID"` to your project id and run the rest in order. Notebooks from 2.3 on clone this
repo (`/content/agentic-ai-weekend-gcp-learners`) the first time they need the kit under `deploy/`.

## Local Jupyter

```bash
git clone --branch rag-production-hardening --single-branch https://github.com/netsetos/agentic-ai-weekend-gcp-learners.git
cd agentic-ai-weekend-gcp-learners
python -m venv .venv && source .venv/bin/activate     # Windows: .venv\Scripts\Activate.ps1
pip install -r requirements.txt
cp .env.example .env                                   # PROJECT_ID, REGION
gcloud auth application-default login
jupyter lab
```

Locally the `auth.authenticate_user()` cell is a no-op outside Colab; ADC from `gcloud auth application-default login`
is what the clients use. The kit is already beside you: the notebooks look for `deploy/evals` above their own folder
before they clone anything, and this repo has it.

## What costs money

Every notebook prints what its calls cost, in USD and INR. The expensive lessons say so at the top (Document AI pages,
tuning jobs, GPU services in Module 11) and the kit's `make off` turns the deployed lane off at the end of a day.
