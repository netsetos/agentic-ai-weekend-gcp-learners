terraform {
  required_version = ">= 1.9.0"
  required_providers {
    google      = { source = "hashicorp/google",      version = "~> 6.15" }
    google-beta = { source = "hashicorp/google-beta", version = "~> 6.15" }
    # cloudsql.tf (12.8): the checkpointer's database password is generated here and lives
    # only in state and in Secret Manager - never in a variable, a notebook or a shell history.
    random      = { source = "hashicorp/random",      version = "~> 3.6" }
  }
  backend "gcs" {
    prefix = "documind/env"
  }
}

# Client-based APIs (including Billing Budgets) need an explicit quota project
# with user ADC. Both providers send X-Goog-User-Project using the resource
# project, not gcloud's shared OAuth project. Enable the APIs there and grant
# the operator serviceusage.services.use. This does not change the GCS backend.
provider "google" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}
provider "google-beta" {
  project               = var.project_id
  region                = var.region
  user_project_override = true
  billing_project       = var.project_id
}
