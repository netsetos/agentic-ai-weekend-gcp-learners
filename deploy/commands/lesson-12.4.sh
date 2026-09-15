# Reference commands extracted from 12.4 (GCP_Capstone_12.4_StreamlitFrontend.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- DEPLOY ----
# IAP on the service itself (--iap): the run.app URL is the front door, IAP signs people in,
# and nothing reaches the container without an assertion. That takes --ingress=all (IAP
# fronts every path) and --no-allow-unauthenticated (IAP's own service agent is the only
# invoker, granted below). MIN_INSTANCES is 1 on a session day and 0 after.
#
# The env delimiter is ^|^: the values carry ':' (URLs) and ',' (ADMIN_EMAILS). RAG_API_URL
# is the API's deterministic run.app URL; CHAT_URL is the chat service's (make deploy-services sets it;
# empty, and chat.py hides the surface); the VECTOR_ pair comes from the state. IAP_AUDIENCE is THIS service's:
# /projects/NUMBER/locations/REGION/services/documind-ui, project number, leading slash.
gcloud run deploy documind-ui \
  --image=${REGION:-us-central1}-docker.pkg.dev/$PROJECT/documind/ui:$GIT_SHA \
  --region=${REGION:-us-central1} --platform=managed \
  --no-allow-unauthenticated --iap \
  --ingress=all \
  --memory=2Gi --cpu=2 --concurrency=80 --timeout=3600 \
  --min-instances=${MIN_INSTANCES:-0} --max-instances=10 \
  --cpu-boost --session-affinity --execution-environment=gen2 \
  --service-account=documind-ui-sa@$PROJECT.iam.gserviceaccount.com \
  --set-env-vars="^|^AUTH_MODE=iap|RAG_API_URL=https://documind-api-$PROJECT_NUMBER.${REGION:-us-central1}.run.app|CHAT_URL=$CHAT_URL|SPEECH_REGION=asia-south1|GOOGLE_CLOUD_PROJECT=$PROJECT|GOOGLE_CLOUD_LOCATION=${REGION:-us-central1}|UPLOAD_BUCKET=$PROJECT-uploads|LAYOUT_PROCESSOR=projects/$PROJECT/locations/$DOCAI_LOCATION/processors/$DOCAI_PROCESSOR_ID|TTS_CACHE_BUCKET=$PROJECT-tts-cache|VECTOR_INDEX_ENDPOINT=$VECTOR_INDEX_ENDPOINT|DEPLOYED_INDEX_ID=$VECTOR_DEPLOYED_INDEX_ID|CHUNKS_COLLECTION=chunks|AUDIT_COLLECTION=audit_events|OBSERVABILITY_DATASET=documind_observability|RAG_MODEL=gemini-3.6-flash|ADMIN_EMAILS=$ADMIN_EMAILS|ADMIN_DOMAINS=$ADMIN_DOMAINS|IAP_AUDIENCE=/projects/$PROJECT_NUMBER/locations/${REGION:-us-central1}/services/documind-ui" \
  --set-secrets="COOKIE_SECRET=cookie-secret:latest" \
  --vpc-connector=projects/$PROJECT/locations/${REGION:-us-central1}/connectors/documind-vpc \
  --vpc-egress=private-ranges-only

# Grant self-impersonation for V4 signed URLs
gcloud iam service-accounts add-iam-policy-binding \
  documind-ui-sa@$PROJECT.iam.gserviceaccount.com \
  --member="serviceAccount:documind-ui-sa@$PROJECT.iam.gserviceaccount.com" \
  --role="roles/iam.serviceAccountTokenCreator"

# IAP's service agent is what invokes the service once IAP is on; gcloud does not grant it.
gcloud run services add-iam-policy-binding documind-ui \
  --region=${REGION:-us-central1} --project=$PROJECT \
  --member="serviceAccount:service-$PROJECT_NUMBER@gcp-sa-iap.iam.gserviceaccount.com" \
  --role=roles/run.invoker

# Who may sign in: one grant per address in ADMIN_EMAILS (comma-separated). Signing in is
# not membership - the roster (make roster) decides which tenant each person sees.
for who in $(echo "$ADMIN_EMAILS" | tr ',' ' '); do
  gcloud iap web add-iam-policy-binding --project=$PROJECT \
    --resource-type=cloud-run --service=documind-ui --region=${REGION:-us-central1} \
    --member="user:$who" --role=roles/iap.httpsResourceAccessor
done

