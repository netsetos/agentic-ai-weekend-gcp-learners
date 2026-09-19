# The LiteLLM gateway (11.3) and the self-hosting identities. Module 11, 10 September 2026.
#
# The gateway is a CPU-only Cloud Run service behind IAM: the API and the UI's account may invoke it (make deploy-gateway
# grants that with gcloud, because the services do not exist at plan time), and the caller's ID token is the door. Its
# spend logs and tag budgets live in the Cloud SQL instance below, which make deploy-gateway mounts as DATABASE_URL
# through the connector (15 September 2026: one shape, the database always declared). It calls Gemini on the global endpoint as itself (aiplatform.user), inspects
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

# The master key secret exists (secrets.tf) and the gateway may read it; the lane does not set one - Cloud Run IAM is the
# door, and a master key would make every caller's ID token an invalid virtual key. Kept for a deployment that opens the
# gateway to keys of its own.
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

# ---- the Postgres LiteLLM's spend logs and tag budgets need (make deploy-gateway mounts it as DATABASE_URL) ----------
resource "random_password" "gateway_db" {
  length  = 24
  special = false
}

resource "google_sql_database_instance" "gateway" {
  name                = "documind-gateway"
  database_version    = "POSTGRES_16"
  region              = var.region
  deletion_protection = false

  settings {
    # PostgreSQL 16 defaults to Enterprise Plus; shared-core tiers require Enterprise.
    edition           = "ENTERPRISE"
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
  name     = "litellm"
  instance = google_sql_database_instance.gateway.name
}

resource "google_sql_user" "gateway" {
  name     = "litellm"
  instance = google_sql_database_instance.gateway.name
  password = random_password.gateway_db.result
}

# The connector authenticates the service account, and this is the role it checks.
resource "google_project_iam_member" "gateway_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

output "gateway_sa" { value = google_service_account.gateway.email }
output "vllm_sa" { value = google_service_account.vllm.email }
output "gateway_database_url" {
  value     = format("postgresql://litellm:%s@/litellm?host=/cloudsql/%s", random_password.gateway_db.result, google_sql_database_instance.gateway.connection_name)
  sensitive = true
}
