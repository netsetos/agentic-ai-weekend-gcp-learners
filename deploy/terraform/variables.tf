variable "project_id" { type = string }
variable "region" {
  type    = string
  default = "us-central1"
}
variable "india_region" {
  type    = string
  default = "asia-south1"
}
variable "env" {
  type    = string
  default = "dev"
} # dev | staging | prod

# ONE SHAPE (15 September 2026). Until then the kit had two profiles - `lean`, the Module 4 lane, and `full`,
# everything Module 12 teaches on top of it, count-gated in the same files behind `local.full`. The course
# runs the full shape only now, so the gate is gone and every resource below is declared unconditionally:
# Vector Search and its endpoint (the ANN tier, the one line item that bills by the hour), the Spanner
# Graph trial, the Cloud SQL checkpointer, the Autopilot cluster, the BigQuery mirror and Dataplex scan,
# the log sink, Cloud Deploy, the gateway's database. What the gate used to switch that is NOT a resource
# count stays a switch of its own, named for what it does: `audit_lock` and `gemini_quota_override` below.

# The audit bucket's retention policy is five years either way (storage.tf); LOCKING it is a separate
# decision. A locked policy is irreversible - the bucket cannot be deleted until its last object ages out,
# and Google liens the project so the project cannot be deleted either. That is what a production account
# wants and what a throwaway lab must never do by accident, so it is false here and true on purpose
# (`make up AUDIT_LOCK=true`).
variable "audit_lock" {
  type    = bool
  default = false
}

# quota.tf's Gemini consumer quota override. The metric and limit names there are the shape the API
# documents, not names verified on a project - an unknown metric fails the whole apply - so the override is
# applied only when asked for (`make up GEMINI_QUOTA_OVERRIDE=true`), after the names are checked with
# `gcloud alpha services quota list --service=aiplatform.googleapis.com`.
variable "gemini_quota_override" {
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
  default = "india" # india | us
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
