variable "project_id"  { type = string }
variable "region"      {
  type    = string
  default = "us-central1"
}
variable "india_region"{
  type    = string
  default = "asia-south1"
}
variable "env"         {
  type    = string
  default = "dev"
}  # dev | staging | prod

# Which of the kit's two shapes this project gets. `lean` is the Module 4 lane and nothing
# else: Cloud Run, Firestore with its vector indexes, one Document AI processor, the uploads
# bucket, the service accounts, secrets, budget and alerts - close to nothing while idle.
# `full` adds what Module 12 teaches on top: Vector Search and its endpoint (the ANN tier,
# the one line item that bills by the hour), the Spanner Graph trial, the Cloud SQL
# checkpointer, GKE, the BigQuery mirror and Dataplex scan, the log sink and Cloud Deploy.
# Same files, `count` on the difference; flip it and apply again, nothing is thrown away.
variable "profile" {
  type    = string
  default = "lean"           # lean | full
  validation {
    condition     = contains(["lean", "full"], var.profile)
    error_message = "profile must be lean or full."
  }
}

locals {
  full = var.profile == "full"
}

# 11.5's Autopilot cluster on the lean lane, for one hour: make gke-up sets it, make gke-down clears it. Off by default
# because an empty cluster still bills its fee (Rs 6,000 a month for nothing).
variable "gke_cluster" {
  type    = bool
  default = false
}

# The one repository allowed to impersonate the deploy identity. Without this the
# WIF provider trusts every repository on GitHub, which is the whole internet.
variable "github_repository" {
  type        = string
  default     = "netsetos/agentic-ai-weekend-gcp"
  description = "owner/repo permitted through Workload Identity Federation"
}

variable "github_repository_id" {
  description = "IMMUTABLE numeric id of the GitHub repo. `gh api repos/OWNER/REPO --jq .id`"
  type        = string
  # No default on purpose. A wrong id fails closed - no workflow can authenticate -
  # whereas a wrong NAME can fail open if somebody else owns that name.
  validation {
    condition     = can(regex("^[0-9]+$", var.github_repository_id))
    error_message = "github_repository_id must be the numeric id, not owner/repo."
  }
}

# Which data-residency story this deployment follows. 12.5 reads it to pick the
# Doc AI processor - Layout Parser is US-only, Enterprise OCR runs in asia-south1 -
# and it is the switch that keeps PII in-country without editing code.
variable "residency" {
  type    = string
  default = "india"          # india | us
  validation {
    condition     = contains(["india", "us"], var.residency)
    error_message = "residency must be india or us."
  }
}

# The document lifecycle (12 September 2026, deploy/INDEXING.md). ONE embedding, declared once: the ingest
# worker stamps the pair on every chunk row and rag-api embeds every query with it. make deploy-services reads
# the two outputs below into both services' environments (EMBEDDING_MODEL, EMBEDDING_VERSION), so a query
# vector from one model against document vectors from another cannot happen by drift - a bump is a plan, an
# apply, a rebuild and a reindex (make reembed, the strategy's next phase).
variable "embedding_model" {
  type    = string
  default = "text-embedding-005"
}

variable "embedding_version" {
  type    = string
  default = "1"
}

# How long a retired chunk row stays before the TTL policy in firestore_indexes.tf removes it: the audit window
# and the undo window. The worker stamps expire_at from it (RETENTION_DAYS, read from the output by
# make deploy-services); nothing else on the lane deletes a chunk.
variable "retention_days" {
  type    = number
  default = 30
  validation {
    condition     = var.retention_days >= 1 && var.retention_days <= 3650
    error_message = "retention_days must be between 1 and 3650."
  }
}

output "embedding_model" {
  description = "EMBEDDING_MODEL for the ingest worker and rag-api"
  value       = var.embedding_model
}

output "embedding_version" {
  description = "EMBEDDING_VERSION for the ingest worker and rag-api"
  value       = var.embedding_version
}

output "retention_days" {
  description = "RETENTION_DAYS for the ingest worker: expire_at = superseded_at + this"
  value       = var.retention_days
}
