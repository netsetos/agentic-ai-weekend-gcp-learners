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
