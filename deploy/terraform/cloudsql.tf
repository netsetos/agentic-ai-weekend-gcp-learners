# Cloud SQL for the chat checkpointer. Lessons 8.5 and 12.8 - gap G5.
#
# WHY. A conversation that survives a restart is Module 8's gate, and 8.5 was precise about the
# three lanes: InMemorySaver dies with the process, SqliteSaver is a file on ONE instance, and
# only a database outlives a deploy, a scale-to-zero and an instance dying. The chat service
# reaches it as PostgresSaver over a DSN it never sees in the image or the repo: the DSN is a
# Secret Manager secret, mounted as CHECKPOINT_DSN by `--set-secrets` (commands/lesson-12.8.sh).
#
# WHAT THIS COSTS. db-f1-micro, zonal, 10 GB, no backups: roughly $8-10 (Rs 700-850) a month
# while it exists - the cheapest Cloud SQL there is, and enough for checkpoints. `make down`
# destroys it; deletion_protection is off on purpose, because this is a learner's throwaway
# project and a destroy that stops at the database is a bill that keeps running.
#
# HOW IT IS REACHED. Public IP with NO authorized networks: nothing on the internet can open a
# connection. Cloud Run connects through the Cloud SQL connector (`--add-cloudsql-instances`),
# which authenticates the SERVICE ACCOUNT with IAM and exposes a unix socket at
# /cloudsql/PROJECT:REGION:INSTANCE - the `host=` in the DSN below, exactly as 8.5 wrote it.
# Private IP needs service-networking peering on documind-vpc; add it when 12.1's network
# lesson wants it, and nothing else here changes.

resource "random_password" "checkpoint" {
  length  = 24
  special = false
}

resource "google_sql_database_instance" "checkpoint" {
  name                = "documind-checkpoint"
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
      ipv4_enabled = true # reached only through the connector; no authorized_networks, on purpose
    }
    backup_configuration {
      enabled = false # checkpoints, not customer documents
    }
  }
}

resource "google_sql_database" "checkpoint" {
  name     = "documind"
  instance = google_sql_database_instance.checkpoint.name
}

resource "google_sql_user" "chat" {
  name     = "chat"
  instance = google_sql_database_instance.checkpoint.name
  password = random_password.checkpoint.result
}

# The DSN is the secret, in the shape 8.5 wrote: the socket directory rides in the query string.
resource "google_secret_manager_secret" "checkpoint_dsn" {
  secret_id = "documind-checkpoint-dsn"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "checkpoint_dsn" {
  secret      = google_secret_manager_secret.checkpoint_dsn.id
  secret_data = "postgresql://chat:${random_password.checkpoint.result}@/documind?host=/cloudsql/${google_sql_database_instance.checkpoint.connection_name}"
}

resource "google_secret_manager_secret_iam_member" "chat_dsn" {
  secret_id = google_secret_manager_secret.checkpoint_dsn.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.chat.email}"
}

# The connector authenticates the service account, and this is the role it checks.
resource "google_project_iam_member" "chat_sql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.chat.email}"
}

output "checkpoint_instance" {
  description = "Pass to --add-cloudsql-instances on documind-chat and to the migration job"
  value       = google_sql_database_instance.checkpoint.connection_name
}
