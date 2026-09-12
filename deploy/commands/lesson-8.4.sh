# Reference commands extracted from 8.4 (GCP_Capstone_8.4_A2A.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- DEPLOY ----
# 1. The image, from the deploy/ context; the Dockerfile copies ONLY services/agent - no shared/.
gcloud builds submit --config=cloudbuild.yaml \
  --substitutions=_IMAGE=${REGION:-us-central1}-docker.pkg.dev/$PROJECT/documind/agent:$GIT_SHA,_DOCKERFILE=services/agent/Dockerfile .

# 2. The service: private (IAM), its own account, the MCP server's url, and its own url for the card.
#    No RAG_API_URL and no roster: the peer does not know the API exists. GOOGLE_CLOUD_LOCATION=global
#    is for Gemini 3.x; SELF_URL is what makes the agent card say a reachable address.
gcloud run deploy documind-agent \
  --image=${REGION:-us-central1}-docker.pkg.dev/$PROJECT/documind/agent:$GIT_SHA \
  --region=${REGION:-us-central1} --platform=managed \
  --no-allow-unauthenticated --ingress=all \
  --memory=1Gi --cpu=1 --concurrency=20 --timeout=300 \
  --min-instances=${MIN_INSTANCES:-0} --max-instances=5 --cpu-boost \
  --execution-environment=gen2 \
  --service-account=documind-agent-sa@$PROJECT.iam.gserviceaccount.com \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=$PROJECT|GOOGLE_GENAI_USE_VERTEXAI=1|GOOGLE_CLOUD_LOCATION=global|MCP_URL=https://documind-mcp-$PROJECT_NUMBER.${REGION:-us-central1}.run.app|SELF_URL=https://documind-agent-$PROJECT_NUMBER.${REGION:-us-central1}.run.app|AGENT_MODEL=gemini-3.6-flash"

# 3. Who may call the peer: the UI's account (this notebook and make smoke-agent mint as it) and the
#    chat account. A2A carries no identity of its own - IAM is the peer's front door, and the peer
#    speaks to the lane as itself whoever called it (sa.tf: documind-agent-sa, on acme's roster only).
for sa in documind-ui-sa documind-chat-sa; do
  gcloud run services add-iam-policy-binding documind-agent \
    --region=${REGION:-us-central1} --project=$PROJECT \
    --member="serviceAccount:$sa@$PROJECT.iam.gserviceaccount.com" --role=roles/run.invoker --quiet
done

# ---- SMOKE ----
# From deploy/ on a machine with gcloud (Cloud Shell): the card refused without a token, the card
# with one, a task answered through documind-mcp, a task naming zeta refused by the roster and relayed.
make smoke-agent PROJECT=$PROJECT

# By hand: the card, as a caller IAM admits
NUMBER=$(gcloud projects describe $PROJECT --format='value(projectNumber)')
AGENT=https://documind-agent-$NUMBER.${REGION:-us-central1}.run.app
TOKEN=$(gcloud auth print-identity-token --include-email \
  --impersonate-service-account=documind-ui-sa@$PROJECT.iam.gserviceaccount.com --audiences=$AGENT)
curl -s -H "Authorization: Bearer $TOKEN" $AGENT/.well-known/agent-card.json | head -c 400; echo
curl -s -o /dev/null -w "without a token: %{http_code}\n" $AGENT/.well-known/agent-card.json

