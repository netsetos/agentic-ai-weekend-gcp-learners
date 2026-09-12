# The ledger's nightly full reconciliation (12.5, 11 September 2026): the bucket's current generations against
# sources/. Objects gone from the bucket are retired, objects newer than their ledger row are re-ingested through
# the worker's own path. The JOB is created from the ingest image by `make reconcile-job` (the image tag is the
# Makefile's, not Terraform's); this file schedules it once it exists: RECONCILE_JOB=true on make plan / the apply.
variable "reconcile_job" {
  type        = bool
  default     = false
  description = "schedule documind-reconcile nightly (the job itself comes from make reconcile-job)"
}

resource "google_cloud_run_v2_job_iam_member" "reconcile_invoker" {
  count    = var.reconcile_job ? 1 : 0
  name     = "documind-reconcile"
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.ingest.email}"
}

# 23:30 IST, after documind-off has floored the lane: the worker's own account runs the job.
resource "google_cloud_scheduler_job" "reconcile" {
  count       = var.reconcile_job ? 1 : 0
  name        = "documind-reconcile-nightly"
  description = "DocuMind: reconcile the ledger against the uploads bucket (retire what is gone, re-ingest what changed)"
  schedule    = "30 23 * * *"
  time_zone   = "Asia/Kolkata"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/documind-reconcile:run"
    oauth_token {
      service_account_email = google_service_account.ingest.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.reconcile_invoker]
}
