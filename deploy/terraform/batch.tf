# The batch lane's consumer (12.5, 13 September 2026): the job that indexes the documents the push path handed
# off - anything over MAX_INLINE_PAGES, which cannot finish inside a push request's 600 seconds. Declared here on
# the ingest image the lane already runs (var.reconcile_image: make batch-job passes
# REGION-docker.pkg.dev/PROJECT/documind/ingest:SHA), beside the IAM grant that lets the worker start it and an
# hourly schedule that drains whatever the worker could not start. Like reconcile.tf, one apply: BATCH_JOB=true on
# make plan / make up once an ingest image exists (the job cannot be created before its image is pushed), and
# `make batch-job` is an apply with the switch on. The worker learns the job's name through BATCH_JOB in its own
# environment (commands/lesson-12.5.sh) and starts it the moment it queues a document.
variable "batch_job" {
  type        = bool
  default     = false
  description = "declare and schedule documind-ingest-batch, the batch lane's consumer (needs an ingest image: make build first)"
}
locals {
  batch = var.batch_job && var.reconcile_image != ""
}
resource "google_cloud_run_v2_job" "ingest_batch" {
  count    = local.batch ? 1 : 0
  name     = "documind-ingest-batch"
  location = var.region
  template {
    task_count = 1
    template {
      service_account = google_service_account.ingest.email
      max_retries     = 0       # the claim carries the retry: a failed document says why, the next run does not retake it
      timeout         = "7200s" # a thousand-page Act at a second a page through Document AI, with room
      containers {
        image   = var.reconcile_image
        command = ["python"]
        args    = ["batch.py", "--project", var.project_id]
        # The worker's own environment (commands/lesson-12.5.sh), so index_document() runs the same way from a job.
        env {
          name  = "GOOGLE_CLOUD_PROJECT"
          value = var.project_id
        }
        env {
          name  = "REGION"
          value = var.region
        }
        env {
          name  = "AUDIT_BUCKET"
          value = "${var.project_id}-audit"
        }
        env {
          name  = "RESIDENCY"
          value = var.residency
        }
        env {
          name  = "DOCAI_PROCESSOR_ID"
          value = element(split("/", google_document_ai_processor.documind.id), 5)
        }
        env {
          name  = "RETENTION_DAYS"
          value = tostring(var.retention_days)
        }
        env {
          name  = "EMBEDDING_MODEL"
          value = var.embedding_model
        }
        env {
          name  = "EMBEDDING_VERSION"
          value = tostring(var.embedding_version)
        }
        env {
          name  = "VECTOR_INDEX_NAME"
          value = local.full ? one(google_vertex_ai_index.documind[*].id) : ""
        }
        env {
          name  = "BQ_CHUNK_TABLE"
          value = local.full ? "${var.project_id}.rag_data.chunk_source" : ""
        }
        env {
          name  = "MANAGED_MIRROR" # the managed mirror (P9.2): the same switch the worker runs with
          value = var.managed_mirror
        }
      }
    }
  }
  lifecycle {
    ignore_changes = [client, client_version]
  }
}
# The worker starts the job when it queues a document (main.py: _run_batch_job), and the schedule below runs it
# as the same account: run.invoker on the job is run.jobs.run.
resource "google_cloud_run_v2_job_iam_member" "ingest_batch_invoker" {
  count    = local.batch ? 1 : 0
  name     = google_cloud_run_v2_job.ingest_batch[0].name
  location = var.region
  role     = "roles/run.invoker"
  member   = "serviceAccount:${google_service_account.ingest.email}"
}
# Hourly, at a quarter past: the backstop for a document the worker queued but could not start the job for. An
# empty queue is one log line and no work.
resource "google_cloud_scheduler_job" "ingest_batch" {
  count       = local.batch ? 1 : 0
  name        = "documind-ingest-batch-hourly"
  description = "DocuMind: drain the batch lane (documents over the push path's page ceiling)"
  schedule    = "15 * * * *"
  time_zone   = "Asia/Kolkata"
  region      = var.region
  http_target {
    http_method = "POST"
    uri         = "https://run.googleapis.com/v2/projects/${var.project_id}/locations/${var.region}/jobs/${google_cloud_run_v2_job.ingest_batch[0].name}:run"
    oauth_token {
      service_account_email = google_service_account.ingest.email
    }
  }
  depends_on = [google_cloud_run_v2_job_iam_member.ingest_batch_invoker]
}
