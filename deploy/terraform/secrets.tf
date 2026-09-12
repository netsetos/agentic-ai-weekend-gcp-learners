variable "secret_names" {
  type    = set(string)
  default = [
    "litellm-master-key",
    "cookie-secret",
    "oauth-client-id",
    "oauth-client-secret",
    "openai-fallback-key",
    # 10.5 pushes the tuned Gemma to a private Hub repo with this token, read from Secret Manager in the
    # notebook - never typed into a cell. Created empty; the owner adds a version when there is a token.
    "hf-token",
  ]
}

resource "google_secret_manager_secret" "s" {
  for_each  = var.secret_names
  secret_id = each.value
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "ui_access" {
  for_each  = google_secret_manager_secret.s
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.ui.email}"
}
resource "google_secret_manager_secret_iam_member" "api_access" {
  for_each  = google_secret_manager_secret.s
  secret_id = each.value.id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.api.email}"
}
