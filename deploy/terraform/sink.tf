resource "google_bigquery_dataset" "observability" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  dataset_id    = "documind_observability"
  location      = var.india_region
  description   = "API structured logs, DLP findings, tenant rollups"
  default_table_expiration_ms = 7776000000   # 90 days for raw logs
  delete_contents_on_destroy  = false
}

resource "google_logging_project_sink" "api_to_bq" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  name        = "documind-api-to-bq"
  destination = "bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.observability[0].dataset_id}"
  filter      = <<EOT
    resource.type = "cloud_run_revision"
    (resource.labels.service_name = "documind-api" OR resource.labels.service_name = "documind-chat")
    (jsonPayload.event = "query" OR jsonPayload.event = "stream" OR jsonPayload.event = "chat")
  EOT
  unique_writer_identity = true
  bigquery_options { use_partitioned_tables = true }
}

# Sink identity needs BQ data editor
resource "google_bigquery_dataset_iam_member" "sink_writer" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  dataset_id = google_bigquery_dataset.observability[0].dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = google_logging_project_sink.api_to_bq[0].writer_identity
}
