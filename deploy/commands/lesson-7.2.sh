# Reference commands extracted from 7.2 (GCP_Capstone_7.2_CloudRunDeploy.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- DEPLOY ----
# The MCP server (7.1): the lane's agent surface. Its own account (sa.tf), no unauthenticated calls,
# the API's URL and its own URL in the environment (SELF_URL is the audience every caller's token
# must carry), stateless streamable HTTP so nothing is pinned to an instance. No VPC connector: it
# calls the API on the public run.app hostname like the UI does, and the boundary is identity.
# RAG_TIMEOUT_S=90: the shared tool layer waits 20 s by default, and a cold API plus a Gemini answer is
# longer than that - the first live call timed out. 90 s is what the eval gate itself waits.
gcloud run deploy documind-mcp \
  --image=${REGION:-us-central1}-docker.pkg.dev/$PROJECT/documind/mcp:$GIT_SHA \
  --region=${REGION:-us-central1} --platform=managed \
  --no-allow-unauthenticated \
  --ingress=all \
  --memory=1Gi --cpu=1 --concurrency=40 --timeout=120 \
  --min-instances=${MIN_INSTANCES:-0} --max-instances=10 \
  --cpu-boost --execution-environment=gen2 \
  --service-account=documind-mcp-sa@$PROJECT.iam.gserviceaccount.com \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=$PROJECT|DOCUMIND_PROFILE=gcp|RAG_API_URL=https://documind-api-$PROJECT_NUMBER.${REGION:-us-central1}.run.app|SELF_URL=https://documind-mcp-$PROJECT_NUMBER.${REGION:-us-central1}.run.app|FASTMCP_STATELESS_HTTP=true|RAG_TIMEOUT_S=90"

# Who may KNOCK: IAM invoker on this service. The operators' accounts (7.3's notebook mints as the
# UI's), the A2A peer (8.4: documind-agent-sa knows this URL and no other; it carried a project-wide
# invoker until 12 September 2026, and sa.tf's caller graph now names this line instead), and the
# eval gate's outsider - deliberately, so the third call in 7.2 is refused by the ROSTER and not by
# the network. An agent service you add later gets the same line for its account.
for who in documind-ui-sa documind-agent-sa documind-outsider-sa; do
  gcloud run services add-iam-policy-binding documind-mcp \
    --region=${REGION:-us-central1} --project=$PROJECT \
    --member="serviceAccount:$who@$PROJECT.iam.gserviceaccount.com" --role=roles/run.invoker --quiet
done

# ---- SMOKE ----
# From deploy/, with the runbook virtual environment active:
# python -m pip install 'fastmcp==3.4.7'
# python -m pip check
make smoke-mcp PROJECT="$PROJECT" REGION="${REGION:-us-central1}"

# the same by hand: health through the IAM ingress, then tools/list as JSON-RPC over streamable HTTP
TOK=$(gcloud auth print-identity-token --include-email --impersonate-service-account=documind-ui-sa@$PROJECT.iam.gserviceaccount.com --audiences=https://documind-mcp-$PROJECT_NUMBER.${REGION:-us-central1}.run.app)
curl -sSf -H "Authorization: Bearer $TOK" https://documind-mcp-$PROJECT_NUMBER.${REGION:-us-central1}.run.app/health
curl -sS -H "Authorization: Bearer $TOK" -H "Content-Type: application/json" -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"curl","version":"0"}}}' \
  https://documind-mcp-$PROJECT_NUMBER.${REGION:-us-central1}.run.app/mcp

