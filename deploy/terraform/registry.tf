resource "google_artifact_registry_repository" "docker" {
  repository_id = "documind"
  format        = "DOCKER"
  location      = var.region
  description   = "DocuMind container images"
  cleanup_policies {
    id     = "keep-recent"
    action = "KEEP"
    most_recent_versions { keep_count = 20 }
  }
  cleanup_policies {
    id     = "delete-old"
    action = "DELETE"
    condition { older_than = "2592000s" }  # 30 days
  }
}
