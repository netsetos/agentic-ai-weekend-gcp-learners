# Three service accounts, one per service, least-privilege from day zero.
resource "google_service_account" "ui" {
  account_id   = "documind-ui-sa"
  display_name = "DocuMind UI (Streamlit on Cloud Run)"
}
resource "google_service_account" "api" {
  account_id   = "documind-api-sa"
  display_name = "DocuMind API (FastAPI backend)"
}
# The ingest worker (12.5). A fourth identity rather than a reuse of api-sa:
# it reads uploads, writes Firestore, and calls Doc AI and Vertex. It has no
# business reading a secret or invoking another Cloud Run service, and the
# cheapest time to say so is before it exists.
resource "google_service_account" "ingest" {
  account_id   = "documind-ingest-sa"
  display_name = "DocuMind ingest worker"
}

resource "google_service_account" "admin" {
  account_id   = "documind-admin-sa"
  display_name = "DocuMind Admin + Observability"
}

# The chat service (12.8). Its own identity, not a reuse of ui-sa: it invokes rag-api,
# reads the roster, calls Gemini and - since the checkpointer (cloudsql.tf, 8.5) - opens
# one Cloud SQL connection and reads one secret. Nothing else, and the list is here to
# be read. Until 2026-09-05 this service had no account at all and no deploy command.
# It sits on the three golden rosters (deploy/Makefile `roster`) like the UI's account: a
# surface calls the API as itself and forwards the person's assertion when there is one.
# Until 2026-09-09 this account doubled as the eval gate's outsider, and the service's first
# live smoke found its every retrieve refused by the API - correctly. See `outsider` below.
resource "google_service_account" "chat" {
  account_id   = "documind-chat-sa"
  display_name = "DocuMind chat (LangChain agent on Cloud Run)"
}

# The MCP server (7.1-7.2): the lane's agent surface, the way the UI is its human surface.
# Its own identity, like the UI's: it invokes rag-api through the ONE retrieve(), reads the
# roster and the ingest claims, and writes audit rows. Not chat-sa and not ui-sa: two
# services under one identity blur 12.3's audit log.
resource "google_service_account" "mcp" {
  account_id   = "documind-mcp-sa"
  display_name = "DocuMind MCP server (agent surface on Cloud Run)"
}

# The A2A peer (8.4): the agent OUTSIDE the kit. It reaches the lane only through the MCP
# server, as itself, so it needs to invoke documind-mcp and to call Gemini - and nothing else.
# No Firestore, no storage, no rag-api. If this list ever grows, the peer has stopped being
# a peer and become a fifth brain without the chat service's identity rules.
resource "google_service_account" "agent" {
  account_id   = "documind-agent-sa"
  display_name = "DocuMind A2A peer (ADK agent over the MCP server, on Cloud Run)"
}

# The eval gate's OUTSIDER (4.8's run_eval.check_isolation, and the last check of every smoke
# test): an account IAM admits - roles/run.invoker on the services that put a ROSTER between the
# door and the data, bound per service since 12 September 2026 (the caller graph below) - that
# sits on no roster, so a refusal comes from the roster and not from the network. A fixture, not
# a service. A service account that plays this part cannot also run a service, which is how the
# chat service's first live smoke (2026-09-09) found every retrieve refused, as designed. It is
# never admitted to the peer (documind-agent): the peer has no roster to refuse it with.
resource "google_service_account" "outsider" {
  account_id   = "documind-outsider-sa"
  display_name = "DocuMind eval outsider (IAM admits it, no roster does)"
}

# Signed URLs on Cloud Run need self-impersonation: generate_signed_url signs through the IAM
# signBlob API with the service's own account, and "you need a private key to sign credentials"
# is what the call says without this grant. The UI signs the figure and segment URLs its
# citations open (9.6); the API signs the upload URLs 9.4's Studio hands out - it had no grant
# until Module 9 joined the lane (9 September 2026), so /v1/media/upload-url could never work.
resource "google_service_account_iam_member" "ui_self_impersonate" {
  service_account_id = google_service_account.ui.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.ui.email}"
}
resource "google_service_account_iam_member" "api_self_impersonate" {
  service_account_id = google_service_account.api.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:${google_service_account.api.email}"
}

# ---------------------------------------------------------------------------------------------
# Who may invoke whom: the caller graph (12 September 2026, P2.1 of the production plan).
#
# roles/run.invoker is not granted in this file any more. It was - at PROJECT scope, to ui, chat,
# mcp, outsider and agent - and the comment on each of those lines already said the right answer:
# narrow it per service. The services are created by `gcloud run deploy` in commands/lesson-*.sh,
# not by Terraform, so the binding that tells the truth is the callee's own, in the script that
# deploys it, for exactly the callers that reach it:
#
#     gcloud run services add-iam-policy-binding <service> --member=serviceAccount:<caller> \
#       --role=roles/run.invoker
#
# (the shape lesson-8.4.sh and the Makefile's deploy-gateway already used). What project scope
# cost: every one of those identities could knock on every service, and documind-agent verifies
# nobody itself - IAM is its door, and it speaks to the lane as documind-agent-sa, on acme's
# roster - so the eval gate's outsider, admitted "everywhere", could read acme's documents through
# the peer. The gate check_authz.py parses the five lines below and the five scripts, and fails
# when they disagree.
#
#   documind-api    <- documind-ui-sa, documind-chat-sa, documind-mcp-sa, documind-outsider-sa  (lesson-12.2.sh)
#   documind-chat   <- documind-ui-sa, documind-outsider-sa                                     (lesson-12.8.sh)
#   documind-mcp    <- documind-ui-sa, documind-agent-sa, documind-outsider-sa                  (lesson-7.2.sh)
#   documind-agent  <- documind-ui-sa, documind-chat-sa                                         (lesson-8.4.sh)
#   documind-ui     <- IAP's service agent, service-NUMBER@gcp-sa-iap                           (lesson-12.4.sh)
#
# Each edge, from the code. The UI calls the API (frontend/chat.py, documents.py, studio.py:
# RAG_API_URL) and the chat service's brains (chat.py: CHAT_URL). chat-sa and mcp-sa reach the API
# through the ONE retrieve() (shared/documind_tools, RAG_API_URL) and nothing else - the chat
# service has no MCP_URL. The peer knows one URL (agent/agent.py: MCP_URL) and never the API's.
# ui-sa is on the MCP server and the peer because the operators mint as it for every smoke and for
# 7.3's and 8.4's notebooks (make smoke-mcp, make smoke-agent); chat-sa on the peer is 8.4's. The
# outsider is bound where a ROSTER answers - the API (run_eval.check_isolation, make eval-live), the
# chat service (make smoke-chat) and the MCP server (7.2's outsider call, make smoke-mcp): each
# verifies the token with shared/iap.identity and then refuses it by the roster, which is the
# refusal the gate is testing for. It is never bound on the peer, which has no roster to refuse it
# with. A person reaches the UI through IAP alone (lesson-12.4.sh binds IAP's service agent, and
# nothing else invokes it).
# ---------------------------------------------------------------------------------------------
locals {
  ui_roles = [
    "roles/aiplatform.user",
    "roles/documentai.apiUser",
    "roles/secretmanager.secretAccessor",
    "roles/datastore.user",
    "roles/speech.editor",
    # NOT roles/run.invoker (12 September 2026). It was here, project-wide, with a note to narrow
    # it per service - and project-wide meant the UI's account could knock on every service in the
    # project. The API and the chat service bind it themselves (lesson-12.2.sh, lesson-12.8.sh),
    # and so do the MCP server and the peer, for the operators who mint as this account
    # (lesson-7.2.sh, lesson-8.4.sh). The caller graph above is the list.
    # NO BigQuery roles here, deliberately. The frontend used to carry a second
    # admin dashboard that queried the warehouse directly; it is now a link to the
    # admin service, which runs under admin-sa. Granting ui-sa bigquery.dataViewer
    # would put warehouse credentials in the process that renders user chat, and
    # an XSS there would reach the warehouse - the blast radius this split exists
    # to prevent.
  ]
  api_roles = [
    "roles/aiplatform.user",
    # 12.6: guard.py sanitises prompts and answers against the Model Armor template when ARMOR=on. The role costs
    # nothing while the switch is off, and a revision switched on without it fails closed on every request.
    "roles/modelarmor.user",
    "roles/datastore.user",
    "roles/secretmanager.secretAccessor",
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    # retriever.rerank() calls the Discovery Engine semantic ranker
    # (semantic-ranker-fast-004). Without this the rerank step fails and
    # retrieval quietly degrades to unranked vector hits.
    "roles/discoveryengine.viewer",
    # guard.py sanitises every prompt and every answer (12.6). Without this the
    # first sanitize call 403s and the guard fails closed - so the service refuses
    # every question, and the logs blame Model Armor rather than IAM.
    "roles/modelarmor.user",
  ]
  ingest_roles = [
    "roles/storage.objectViewer", # read the uploaded object, not write it
    "roles/datastore.user",       # documents/ claims + chunks/
    "roles/documentai.apiUser",
    "roles/aiplatform.user", # embeddings + streaming upserts
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    # 12.5's worker scans every chunk before indexing it. Without this the
    # scan 403s, the message is nacked, and every upload lands in the DLQ.
    "roles/dlp.user",
    # ...and writes doc.upload / dlp.finding events to the audit bucket.
    "roles/storage.objectCreator",
    # The managed mirror (P9.2, 13 September 2026): a version's text into the tenant's Vertex AI Search data store
    # (managed.tf) and out again on retirement. RAG Engine's corpus rides roles/aiplatform.user above. Nothing
    # here while MANAGED_MIRROR=off; without it the first mirror_failed names the 403.
    "roles/discoveryengine.editor",
  ]
  cicd_roles = [
    "roles/cloudbuild.builds.editor",
    "roles/artifactregistry.writer",
    "roles/clouddeploy.releaser",
    # Cloud Deploy names this SA as the EXECUTION account for RENDER, DEPLOY and
    # VERIFY (clouddeploy.tf). releaser only lets it CREATE a release; without
    # jobRunner the release is cut and then dies at rollout.
    "roles/clouddeploy.jobRunner",
    # ...and the deploy job actuates a Cloud Run service. Google's Cloud Deploy
    # service-account page names exactly this pair for a Cloud Run target:
    # jobRunner plus the runtime's developer role (plus actAs, granted per
    # account below).
    "roles/run.developer",
    # NOT roles/storage.objectAdmin. Cloud Deploy does stage rendered manifests in
    # a GCS bucket, and jobRunner already carries that access. objectAdmin at
    # PROJECT scope would be full object control over every bucket here - which
    # includes the uploads bucket of customer documents and the retention-locked
    # audit bucket. Adding it "because Cloud Deploy touches storage" is exactly
    # the reflex this file exists to argue against.
    #
    # NOT roles/logging.logWriter either: it is not in the documented set, and the
    # execution jobs log through the Cloud Deploy service agent, not through this
    # identity.
    # NOTE: roles/iam.serviceAccountUser is deliberately NOT here. At project
    # scope it would confer actAs on every service account in the project,
    # including ones created after this file. It is granted per-account below.
  ]

  # The identities this pipeline may act as - a MAP, keyed on static strings.
  #
  # It was a list, and that broke `terraform plan` outright: for_each over
  # google_service_account.*.name keys the resource on a value Terraform cannot
  # know until apply, and Terraform refuses ("The keys of the map or all values
  # in a set of strings must be known values"). The keys below are literals; the
  # unknown part rides in each.value, which is allowed.
  chat_roles = [
    "roles/aiplatform.user", # Gemini through langchain-google-genai (shared/profile.py)
    "roles/datastore.user",  # the tenant roster (shared/tenancy.py)
    # rag-api, through the ONE retrieve(): roles/run.invoker on documind-api, bound in lesson-12.2.sh
    # since 12 September 2026 - not here, not project-wide (the caller graph above).
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    # secretmanager.secretAccessor and cloudsql.client are granted in cloudsql.tf, on the one
    # secret and for the one instance the checkpointer needs - not project-wide here.
  ]

  mcp_roles = [
    # rag-api, through the ONE retrieve(): roles/run.invoker on documind-api, bound in lesson-12.2.sh
    # since 12 September 2026 - not here, not project-wide (the caller graph above).
    "roles/datastore.viewer", # the roster (tenancy), the ingest claims and chunk counts - reads only
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
    "roles/storage.objectCreator", # audit rows, when AUDIT_BUCKET is set
  ]

  # Nothing at project scope (12 September 2026). "May knock on every service" was the one role
  # here, and every service included the peer, which cannot refuse anybody by a roster. The
  # outsider's invoker is bound on the three services it is meant to knock on (lesson-12.2.sh,
  # lesson-12.8.sh, lesson-7.2.sh - the caller graph above). The empty list keeps the resource
  # below honest: `terraform apply` removes the project grant it used to manage.
  outsider_roles = []

  agent_roles = [
    # documind-mcp, and only that: roles/run.invoker bound on the MCP server in lesson-7.2.sh since
    # 12 September 2026 - not here, where "only that" was a comment and the grant was project-wide.
    "roles/aiplatform.user", # Gemini, through ADK
    "roles/logging.logWriter",
    "roles/cloudtrace.agent",
  ]

  cicd_actas = {
    api    = google_service_account.api.name
    ui     = google_service_account.ui.name
    ingest = google_service_account.ingest.name
    admin  = google_service_account.admin.name
    chat   = google_service_account.chat.name
    mcp    = google_service_account.mcp.name
    agent  = google_service_account.agent.name
    # ...and itself. clouddeploy.tf names THIS account as the RENDER/DEPLOY/VERIFY
    # execution account, and Cloud Deploy requires actAs on the execution account
    # even when it is the caller. Without this the release is cut and then dies at
    # rollout, naming the account the operator deliberately chose - which reads as
    # "wrong account" and is really "missing self-actAs".
    cicd = google_service_account.cicd.name
  }
  admin_roles = [
    "roles/monitoring.viewer",
    "roles/logging.viewer",
    "roles/datastore.user",
    "roles/bigquery.jobUser",
    "roles/run.developer", # budget-guard fn calls run_v2.update_service to set min_instances=0
    # bigquery.jobUser lets admin-sa RUN a query; it does not let it READ a
    # table. Both are needed, and the failure without dataViewer is a
    # permission error on the first SELECT rather than at deploy time.
    "roles/bigquery.dataViewer",
    # CSV/PDF exports from the admin dashboard land in the audit bucket.
    "roles/storage.objectCreator",
    # admin/dlp.py calls inspect_content and deidentify_content. Without this the
    # audit page raises 403 on the first scan - and the failure looks like a bad
    # request rather than a missing role, because DLP reports it per-item.
    "roles/dlp.user",
  ]
}

# The identity GitHub Actions impersonates through Workload Identity Federation.
# 12.7 builds the pipeline; the account belongs here, with the other four, because
# the interesting part is the SHAPE of its permissions.
#
# Be honest about that shape, because the earlier version of this comment was not.
# It claimed this account "cannot read a secret or a customer document". It could:
# roles/iam.serviceAccountUser was granted at PROJECT scope, which is actAs on
# every service account in the project, so a build submitted as documind-api-sa
# reached Secret Manager and Firestore in one hop.
#
# A deploy identity that deploys a service running as X must be able to act as X -
# that is inherent, not a bug. What is fixable is the BLAST RADIUS: actAs is now
# granted per service account, on the four runtime identities this pipeline
# actually deploys (see cicd_actas), so the list is enumerated, reviewable, and
# does not silently include the next service account somebody adds.
resource "google_service_account" "cicd" {
  account_id   = "sa-documind-cicd"
  display_name = "DocuMind CI/CD (GitHub Actions via WIF)"
}

resource "google_project_iam_member" "cicd" {
  for_each = toset(local.cicd_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.cicd.email}"
}

# actAs, scoped to named accounts - NOT project-wide.
resource "google_service_account_iam_member" "cicd_actas" {
  for_each           = local.cicd_actas
  service_account_id = each.value
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.cicd.email}"
}

resource "google_project_iam_member" "ingest" {
  for_each = toset(local.ingest_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_project_iam_member" "mcp" {
  for_each = toset(local.mcp_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.mcp.email}"
}

resource "google_project_iam_member" "outsider" {
  for_each = toset(local.outsider_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.outsider.email}"
}

resource "google_project_iam_member" "agent" {
  for_each = toset(local.agent_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.agent.email}"
}

resource "google_project_iam_member" "ui" {
  for_each = toset(local.ui_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.ui.email}"
}
resource "google_project_iam_member" "api" {
  for_each = toset(local.api_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.api.email}"
}
resource "google_project_iam_member" "chat" {
  for_each = toset(local.chat_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.chat.email}"
}
resource "google_project_iam_member" "admin" {
  for_each = toset(local.admin_roles)
  project  = var.project_id
  role     = each.value
  member   = "serviceAccount:${google_service_account.admin.email}"
}
