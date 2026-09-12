# The ledger's nightly full reconciliation (12.5, 11 September 2026): the bucket's current generations against
# sources/. Objects gone from the bucket are retired, objects newer than their ledger row are re-ingested through
# the worker's own path, and the walk ends with one number - drift - that alerts.tf turns into a metric.
#
# 12 September 2026: the whole night is one apply. The JOB is declared here, on the ingest image the lane already
# runs (var.reconcile_image: make reconcile-job passes REGION-docker.pkg.dev/PROJECT/documind/ingest:SHA), beside
# the IAM grant and the schedule. Nothing operational is created by hand: RECONCILE_JOB=true on make plan / make up
# once an ingest image exists (the job cannot be created before its image is pushed), and `make reconcile-job`
# is an apply with the switch on - an image bump, not a creator.
variable "reconcile_job" {
  type        = bool
  default     = false
  description = "declare and schedule documind-reconcile nightly (needs an ingest image: make build first)"
}

variable "reconcile_image" {
  type        = string
  default     = ""
  description = "the ingest image the nightly job runs (REGION-docker.pkg.dev/PROJECT/documind/ingest:SHA)"
}

locals {
  reconcile = var.reconcile_job && var.reconcile_image != ""
}

resource "google_cloud_run_v2_job" "reconcile" {
  count    = local.reconcile ? 1 : 0
  name     = "documind-reconcile"
  location = var.region

  template {
    task_count = 1
    template {
      service_account = google_service_account.ingest.email
      max_retries     = 0
      timeout         = "1200s"
      containers {
        image   = var.reconcile_image
        command = ["python"]
        args    = ["reconcile.py", "--project", var.project_id, "--apply"]
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "AUDIT_BUCKET"
          value = "${var.project_id}-audit"
        }
        env {
          name  = "RETENTION_DAYS"
          value = tostring(var.retention_days)
        }
      }
    }
  }

  # A new image tag is a new job revision; the schedule below runs whatever is declared. The scheduler's
  # oauth token is minted as the same account that runs the task.
  lifecycle {
    ignore_changes = [client, client_version]
  }
}

resource "google_cloud_run_v2_job_iam_member" "reconcile_invoker" {
  count    = local.reconcile ? 1 : 0
  name     = google_cloud_run_v2_job.reconcile[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.ingest.email}"
}

# 23:30 IST, after documind-off has floored the lane: the worker's own account runs the job.
resource "google_cloud_scheduler_job" "reconcile" {
  count       = local.reconcile ? 1 : 0
  name        = "documind-reconcile-nightly"
  description = "DocuMind: reconcile the ledger against the uploads bucket (retire what is gone, re-ingest what changed, measure the drift)"
  schedule    = "30 23 * * *"
  time_zone   = "Asia/Kolkata"
  region      = var.region

  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.reconcile[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.ingest.email
    }
  }

  depends_on = [google_cloud_run_v2_job_iam_member.reconcile_invoker]
}
