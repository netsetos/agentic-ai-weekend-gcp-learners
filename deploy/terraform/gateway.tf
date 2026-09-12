# The LiteLLM gateway (11.3) and the self-hosting identities. Module 11, 10 September 2026.
#
# On the lean profile the gateway is a CPU-only Cloud Run service behind IAM with no database: the API and the UI's
# account may invoke it (make deploy-gateway grants that with gcloud, because the services do not exist at plan time),
# and the caller's ID token is the door. It calls Gemini on the global endpoint as itself (aiplatform.user), inspects
# prompts with Sensitive Data Protection when the audit sampler is on (dlp.user), and reaches the SLM and the vLLM engine
# through its token proxy with its own ID token - so those two services grant it run.invoker when they are deployed.
resource "google_service_account" "gateway" {
  account_id   = "documind-gateway-sa"
  display_name = "DocuMind gateway (LiteLLM on Cloud Run)"
}

resource "google_project_iam_member" "gateway_vertex" {
  project = var.project_id
  role    = "roles/aiplatform.user"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

# dlp_audit.py inspects a sample of prompts that cross the gateway. Without dlp.user the guardrail raises on the first
# sampled request and the router treats it as a dead backend - so a missing IAM role presents as a model outage.
resource "google_project_iam_member" "gateway_dlp" {
  project = var.project_id
  role    = "roles/dlp.user"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

# The master key is the FULL profile's door (virtual keys, budgets); the secret exists on both so the grant can too.
resource "google_secret_manager_secret_iam_member" "gateway_master_key" {
  secret_id = google_secret_manager_secret.s["litellm-master-key"].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.gateway.email}"
}

# 11.1 / 11.2's vLLM engine runs as its own account: it serves a model and needs nothing else. Optional (D3); the
# account costs nothing to exist and make deploy-vllm needs it to be there.
resource "google_service_account" "vllm" {
  account_id   = "documind-vllm-sa"
  display_name = "DocuMind vLLM engine (Gemma on a Cloud Run L4)"
}

# ---- full profile: the Postgres LiteLLM's virtual keys, tag budgets and spend logs need ----------------------------
resource "random_password" "gateway_db" {
  count   = local.full ? 1 : 0
  length  = 24
  special = false
}

resource "google_sql_database_instance" "gateway" {
  count               = local.full ? 1 : 0
  name                = "documind-gateway"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = false

  settings {
    tier              = "db-f1-micro"
    availability_type = "ZONAL"
    disk_size         = 10
    ip_configuration {
      ipv4_enabled = true # reached only through the Cloud SQL connector; no authorized_networks, on purpose
    }
    backup_configuration {
      enabled = false # keys and spend rows, not customer documents
    }
  }
}

resource "google_sql_database" "gateway" {
  count    = local.full ? 1 : 0
  name     = "litellm"
  instance = google_sql_database_instance.gateway[0].name
}

resource "google_sql_user" "gateway" {
  count    = local.full ? 1 : 0
  name     = "litellm"
  instance = google_sql_database_instance.gateway[0].name
  password = random_password.gateway_db[0].result
}

output "gateway_sa" { value = google_service_account.gateway.email }
output "vllm_sa" { value = google_service_account.vllm.email }
output "gateway_database_url" {
  value     = local.full ? format("postgresql://litellm:%s@/litellm?host=/cloudsql/%s", random_password.gateway_db[0].result, google_sql_database_instance.gateway[0].connection_name) : ""
  sensitive = true
}
