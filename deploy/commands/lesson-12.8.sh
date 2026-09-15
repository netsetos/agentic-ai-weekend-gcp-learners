# Reference commands extracted from 12.8 (GCP_Capstone_12.8_IntegrateSurfaces.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- DEPLOY ----
# 1. The image, from the deploy/ context - the same config as rag-api, another Dockerfile.
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_IMAGE=us-central1-docker.pkg.dev/$PROJECT/documind/chat:$GIT_SHA,_DOCKERFILE=services/chat/Dockerfile .

# 2. The service. The Makefile fills two variables (deploy-services), and the expansions below are ${VAR-default}
#    rather than ${VAR:-default} so a value set to the empty string stays empty:
#    CHAT_SQL_FLAGS  --add-cloudsql-instances + --set-secrets mount the DSN Terraform wrote (cloudsql.tf) - the
#                    Cloud SQL checkpointer, always (15 September 2026: one shape). CHECKPOINT_DSN=memory is still
#                    a valid value for a laptop - the in-memory checkpointer agent.py logs as "tests only",
#                    because a conversation dies with the instance (8.5) - and no deployment uses it.
#    CHAT_EXTRA_ENV  extra environment for the service, empty on the lane.
#    DOCUMIND_PROFILE=gcp is stated so agent.py's local bypass is unreachable. IAP_AUDIENCE lists
#    this surface AND the UI: the UI forwards the person's assertion when its brain radio calls
#    this service (12.4), and that assertion was minted for the UI's audience. SELF_URL is the
#    bearer leg: an agent, make smoke-chat or 8.7's notebook calls with an ID token minted for this
#    URL and no assertion, shared/iap.identity verifies it, and the roster still decides.
#    RAG_TIMEOUT_S=90 is 7.2's finding - a cold API takes longer than the tool layer's default.
#    DOCUMIND_BRAIN is the default brain; GOOGLE_GENAI_USE_VERTEXAI is for the ADK brain.
gcloud run deploy documind-chat \
  --image=us-central1-docker.pkg.dev/$PROJECT/documind/chat:$GIT_SHA \
  --region=us-central1 --platform=managed \
  --no-allow-unauthenticated \
  --memory=1Gi --cpu=1 --concurrency=20 --timeout=300 \
  --min-instances=0 --max-instances=10 \
  --service-account=documind-chat-sa@$PROJECT.iam.gserviceaccount.com \
  ${CHAT_SQL_FLAGS---add-cloudsql-instances=$PROJECT:us-central1:documind-checkpoint --set-secrets=CHECKPOINT_DSN=documind-checkpoint-dsn:latest} \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=$PROJECT|DOCUMIND_PROFILE=gcp|RAG_API_URL=https://documind-api-$PROJECT_NUMBER.us-central1.run.app|SELF_URL=https://documind-chat-$PROJECT_NUMBER.us-central1.run.app|RAG_TIMEOUT_S=90|DOCUMIND_BRAIN=langchain|GOOGLE_GENAI_USE_VERTEXAI=1|GOOGLE_CLOUD_LOCATION=global|IAP_AUDIENCE=/projects/$PROJECT_NUMBER/locations/us-central1/services/documind-chat,/projects/$PROJECT_NUMBER/locations/us-central1/services/documind-ui${CHAT_EXTRA_ENV-}"

# 2b. Who may call it (12 September 2026): the UI's account - the brain radio on the chat page posts here as
#     ui-sa with the person's assertion (12.4) - and the eval gate's outsider, which make smoke-chat sends to
#     /v1/chat to see the ROSTER's 403 and not the network's. Bound here, on the service, because the
#     project-wide roles/run.invoker both accounts used to carry (sa.tf) admitted them to every service, the
#     A2A peer included. sa.tf's caller graph is the list; the gate check_authz.py compares this loop with it.
for who in documind-ui-sa documind-outsider-sa; do
  gcloud run services add-iam-policy-binding documind-chat \
    --region=us-central1 --project=$PROJECT \
    --member="serviceAccount:$who@$PROJECT.iam.gserviceaccount.com" --role=roles/run.invoker --quiet
done

# 3. The one-time checkpoint migration (8.5: setup() takes exclusive locks - a job, never startup).
gcloud run jobs create documind-checkpoint-setup \
  --image=us-central1-docker.pkg.dev/$PROJECT/documind/chat:$GIT_SHA \
  --region=us-central1 --service-account=documind-chat-sa@$PROJECT.iam.gserviceaccount.com \
  --set-cloudsql-instances=$PROJECT:us-central1:documind-checkpoint \
  --set-secrets=CHECKPOINT_DSN=documind-checkpoint-dsn:latest \
  --command=python --args=migrate.py || echo "job exists - continuing"
gcloud run jobs execute documind-checkpoint-setup --region=us-central1 --wait

# 4. IAP in front of the human surface, AFTER the service exists (step 4 of the runbook above). The UI's brain radio
#    and make smoke-chat still reach it: IAP_AUDIENCE above lists both surfaces, and the bearer leg (SELF_URL) is
#    verified by shared/iap.identity when there is no assertion.
gcloud beta run services update documind-chat --region=us-central1 --iap

