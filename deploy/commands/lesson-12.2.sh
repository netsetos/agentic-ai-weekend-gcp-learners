# Reference commands extracted from 12.2 (GCP_Capstone_12.2_RAGBackend.ipynb).
# NOT run automatically. Review, set $PROJECT / $GIT_SHA, then run by hand
# or via the Makefile live-tier targets. See deploy/README.md.

# ---- DEPLOY ----
# --ingress=all, and the boundary is identity, not the network: no unauthenticated calls, a
# bearer token from the UI's service account on every request (IAM invoker), and the person's
# own IAP assertion forwarded inside it (shared/iap.py). With internal-and-cloud-load-balancing
# the UI's call - which leaves through the public run.app hostname - and the eval gate's, from
# a GitHub runner, were both refused before any token was read.
#
# The env delimiter is ^|^ because the values carry ':' (every URL) and ',' (two audiences).
# RETRIEVAL_BACKEND is firestore in the lean profile and vector in the full one, where the two
# VECTOR_ variables come from `terraform output`; SELF_URL is the deterministic run.app URL.
# DEMO_MODE=1 (Module 9): /v1/media/generate serves a cached image for a prompt it has drawn before,
# so a demo re-running one prompt is one bill; DEMO_MODE=0 make deploy-services turns it off. The two
# bucket names are storage.tf's: generated assets in media, 9.4's signed PUTs into uploads.
# AUDIT_BUCKET: the media route writes the audit row through shared/audit_log.py, which refuses to drop an
# event without a bucket - and refuses BEFORE the spend since the first live generate went 500 after it.
# Module 10: the model is a SETTING. GENERATOR_MODEL is a name (global) or a tuned endpoint path (regional,
# generator.py picks the client); RAG_MODEL_BASE prices an endpoint at its base; ROUTING=on puts router.py and
# the budget breaker on the request path; BUDGET_USD is the month's cap; SPEND_PCT overrides the reading.
# 12.5's ledger (11 September 2026): RETRIEVAL_CURRENT_ONLY=on retrieves only chunks the ledger marks current -
# after the second vector index is built and make backfill-current has run; a candidate first, like every switch.
# 12 September 2026 (deploy/INDEXING.md): EMBEDDING_MODEL / EMBEDDING_VERSION are the one declared embedding - the same
# pair the ingest worker stamps on every chunk row, so the query vector and the document vectors come from one model.
# Module 12 (12.6): THE GUARD IS A SWITCH. ARMOR=on screens the prompt before retrieval and the buffered answer after
# it against the Model Armor template (asia-south1, with the data it inspects); off on the lane, on for the candidate
# 12.6 judges - make candidate ARMOR=on.
# Module 11: THE BACKEND IS A SETTING. MODEL_BACKEND=vertex is google.genai; gateway sends the same prompt to 11.3's
# LiteLLM gateway (LITELLM_URL, the deterministic URL, behind IAM) where GENERATOR_MODEL names a route - documind-slm
# is 11.4's self-hosted model. The lane runs vertex; make candidate MODEL_BACKEND=gateway is the A/B.
# 13 September 2026: RETRIEVAL_GRAPH=off|on|auto is 4.6's graph on the lane (shared/documind_graph.py, built by make graph):
# on walks the tenant's graph for every question, auto only for a relational question with a seed entity, and the walk's
# chunks go in front of the dense pool. Off on the lane; make candidate RETRIEVAL_GRAPH=auto is where it is judged.
gcloud run deploy documind-api \
  --image=${REGION:-us-central1}-docker.pkg.dev/$PROJECT/documind/api:$GIT_SHA \
  --region=${REGION:-us-central1} --platform=managed \
  --no-allow-unauthenticated \
  --ingress=all \
  --memory=1Gi --cpu=2 --concurrency=40 --timeout=120 \
  --min-instances=0 --max-instances=20 \
  --cpu-boost --execution-environment=gen2 \
  --service-account=documind-api-sa@$PROJECT.iam.gserviceaccount.com \
  --set-env-vars="^|^GOOGLE_CLOUD_PROJECT=$PROJECT|RETRIEVAL_BACKEND=${RETRIEVAL_BACKEND:-firestore}|VECTOR_INDEX_ENDPOINT=$VECTOR_INDEX_ENDPOINT|VECTOR_DEPLOYED_INDEX_ID=$VECTOR_DEPLOYED_INDEX_ID|SELF_URL=https://documind-api-$PROJECT_NUMBER.${REGION:-us-central1}.run.app|IAP_AUDIENCE=/projects/$PROJECT_NUMBER/locations/${REGION:-us-central1}/services/documind-ui,/projects/$PROJECT_NUMBER/locations/${REGION:-us-central1}/services/documind-chat|DEMO_MODE=${DEMO_MODE-1}|UPLOAD_BUCKET=$PROJECT-uploads|MEDIA_BUCKET=$PROJECT-media|AUDIT_BUCKET=$PROJECT-audit|GENERATOR_MODEL=${GENERATOR_MODEL-gemini-3.6-flash}|RAG_MODEL_BASE=${RAG_MODEL_BASE-gemini-3.6-flash}|ROUTING=${ROUTING-off}|BUDGET_USD=${BUDGET_USD-100}${SPEND_PCT:+|SPEND_PCT=$SPEND_PCT}|MODEL_BACKEND=${MODEL_BACKEND-vertex}|LITELLM_URL=https://documind-gateway-$PROJECT_NUMBER.${REGION:-us-central1}.run.app|ARMOR=${ARMOR-off}|ARMOR_LOCATION=${ARMOR_LOCATION-asia-south1}|ARMOR_TEMPLATE=${ARMOR_TEMPLATE-documind-guard}|SEMANTIC_CACHE=${SEMANTIC_CACHE-off}|RETRIEVAL_CURRENT_ONLY=${RETRIEVAL_CURRENT_ONLY-off}|RETRIEVAL_GRAPH=${RETRIEVAL_GRAPH-off}|EMBEDDING_MODEL=${EMBEDDING_MODEL-text-embedding-005}|EMBEDDING_VERSION=${EMBEDDING_VERSION-1}|GIT_SHA=$GIT_SHA" \
  --vpc-connector=projects/$PROJECT/locations/${REGION:-us-central1}/connectors/documind-vpc \
  --vpc-egress=private-ranges-only

# Who may KNOCK (12 September 2026): roles/run.invoker on THIS service, for the four identities that reach it,
# and no longer project-wide in sa.tf - project-wide admitted every one of them to every service, the A2A peer
# included. The UI's account streams answers and runs the smoke below; chat-sa and mcp-sa arrive through the
# ONE retrieve(); the outsider is the eval gate's fixture, admitted so that its refusal is the roster's 403 and
# not the network's. sa.tf's caller graph is the list, and the gate check_authz.py compares this loop with it.
for who in documind-ui-sa documind-chat-sa documind-mcp-sa documind-outsider-sa; do
  gcloud run services add-iam-policy-binding documind-api \
    --region=${REGION:-us-central1} --project=$PROJECT \
    --member="serviceAccount:$who@$PROJECT.iam.gserviceaccount.com" --role=roles/run.invoker --quiet
done

# ---- SMOKE ----
# Get an identity token (not your user token) because --no-allow-unauthenticated
TOK=$(gcloud auth print-identity-token --include-email --impersonate-service-account=documind-ui-sa@$PROJECT.iam.gserviceaccount.com --audiences=https://documind-api-$PROJECT_NUMBER.us-central1.run.app)
# The token is also WHO you are to rag-api (shared/iap.py's bearer leg, 12.8): documind-ui-sa must be
# on acme's roster, or the query below is a 403 - a real refusal, not a missing header.

curl -sSf -H "Authorization: Bearer $TOK" https://documind-api-xxx.run.app/health
# -> {"status":"ok"}

curl -sSf -H "Authorization: Bearer $TOK" \
     -H "Content-Type: application/json" \
     -d '{"query":"Which file types are supported?","tenant_id":"acme","user_id":"u_1","top_k":5,"stream":false}' \
     https://documind-api-xxx.run.app/v1/query
# -> {"answer":"...","citations":[...],"confidence":"high","answerable":true,...}

curl -sN -H "Authorization: Bearer $TOK" \
     -H "Content-Type: application/json" \
     -d '{"query":"Same","tenant_id":"acme","user_id":"u_1","top_k":5}' \
     https://documind-api-xxx.run.app/v1/stream
# -> event: citation ... event: token ... event: done

