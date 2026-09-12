#!/usr/bin/env sh
# Read-only. Everything `make up` will need, checked before anything is created:
#     make preflight PROJECT=documind-ai-live-0910 TFSTATE_BUCKET=documind-ai-live-0910-tfstate
# Prints one line per check and exits non-zero if any is missing. Creates nothing.
set -u
PROJECT="${PROJECT:?set PROJECT}"
TFSTATE_BUCKET="${TFSTATE_BUCKET:-documind-tfstate}"
REGION="${REGION:-us-central1}"
bad=0
ok()   { printf '  ok    %s\n' "$1"; }
miss() { printf '  MISS  %s\n' "$1"; bad=1; }

for tool in gcloud terraform make python; do
  command -v "$tool" >/dev/null 2>&1 && ok "$tool on PATH" || miss "$tool on PATH (Cloud Shell has all four)"
done
# backend.tf requires >= 1.9.0. Cloud Shell's preinstalled terraform is often older; a newer
# binary in ~/bin takes precedence once PATH says so (the MISS line below is the whole recipe).
tfv=$(terraform version 2>/dev/null | head -1 | sed 's/^Terraform v//')
tfmaj=${tfv%%.*}; tfrest=${tfv#*.}; tfmin=${tfrest%%.*}
if [ -n "$tfv" ] && [ "${tfmaj:-0}" -ge 1 ] 2>/dev/null && { [ "${tfmaj:-0}" -gt 1 ] || [ "${tfmin:-0}" -ge 9 ]; } 2>/dev/null; then
  ok "terraform $tfv (backend.tf wants >= 1.9)"
else
  miss "terraform >= 1.9, found '${tfv:-none}'. Cloud Shell: curl -sLo /tmp/tf.zip https://releases.hashicorp.com/terraform/1.9.8/terraform_1.9.8_linux_amd64.zip && mkdir -p ~/bin && unzip -o -q -d ~/bin /tmp/tf.zip && export PATH=\$HOME/bin:\$PATH && hash -r && terraform version"
fi

acct=$(gcloud auth list --filter=status:ACTIVE --format='value(account)' 2>/dev/null)
[ -n "$acct" ] && ok "signed in as $acct" || miss "gcloud auth login"

if gcloud projects describe "$PROJECT" --format='value(projectId)' >/dev/null 2>&1; then
  ok "project $PROJECT exists"
else
  miss "project $PROJECT (gcloud projects create $PROJECT --set-as-default)"
fi
billing=$(gcloud billing projects describe "$PROJECT" --format='value(billingEnabled)' 2>/dev/null)
[ "$billing" = "True" ] && ok "billing linked" || miss "billing (gcloud billing projects link $PROJECT --billing-account=...)"

if gcloud storage buckets describe "gs://$TFSTATE_BUCKET" >/dev/null 2>&1; then
  ok "state bucket gs://$TFSTATE_BUCKET"
else
  miss "state bucket gs://$TFSTATE_BUCKET (gcloud storage buckets create gs://$TFSTATE_BUCKET --location=asia-south1 --uniform-bucket-level-access)"
fi

enabled=$(gcloud services list --enabled --project="$PROJECT" --format='value(config.name)' 2>/dev/null)
missing=""
for api in run compute vpcaccess pubsub artifactregistry secretmanager firestore storage aiplatform \
           documentai dlp iap iamcredentials cloudbuild monitoring logging billingbudgets \
           discoveryengine modelarmor cloudbilling cloudresourcemanager serviceusage; do
  echo "$enabled" | grep -q "^$api.googleapis.com$" || missing="$missing $api"
done
[ -z "$missing" ] && ok "APIs enabled" || miss "APIs not enabled:$missing (make apis PROJECT=$PROJECT)"

python -c 'import google.cloud.firestore' 2>/dev/null && ok "google-cloud-firestore for make roster" \
  || miss "pip install --user google-cloud-firestore==2.30.0 (make roster runs shared/tenancy.py)"

echo
if [ "$bad" = 0 ]; then
  echo "preflight clean - next: make plan, then make up"
else
  echo "preflight: fix the MISS lines above, then re-run"
fi
echo "not checkable from here: the OAuth consent screen (Google Auth Platform > Branding) - IAP needs it"
exit $bad
