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

provider "google" {
  project = var.project_id
  region  = var.region
}
provider "google-beta" {
  project = var.project_id
  region  = var.region
}
