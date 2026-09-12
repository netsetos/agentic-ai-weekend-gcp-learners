variable "pagerduty_key" {
  type      = string
  sensitive = true
}

resource "google_monitoring_notification_channel" "oncall" {
  # The Makefile passes `unset-in-dryrun` when nobody has a PagerDuty key; a channel with
  # a placeholder key is worse than none, so none: the policies below still exist and show
  # in the console, and a real key on a later apply creates the channel and wires it in.
  count        = var.pagerduty_key == "unset-in-dryrun" ? 0 : 1
  display_name = "DocuMind on-call"
  type         = "pagerduty"
  sensitive_labels {
    service_key = var.pagerduty_key
  }
}

# SLO: p95 /v1/query < 3s over 30-min rolling window
resource "google_monitoring_alert_policy" "api_latency" {
  display_name = "API p95 latency > 3s"
  combiner     = "OR"
  conditions {
    display_name = "p95 > 3s for 5 minutes"
    condition_threshold {
      filter     = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"documind-api\" AND metric.type=\"run.googleapis.com/request_latencies\""
      comparison = "COMPARISON_GT"
      threshold_value = 3000
      duration   = "300s"
      aggregations {
        alignment_period     = "60s"
        per_series_aligner   = "ALIGN_PERCENTILE_95"
        cross_series_reducer = "REDUCE_MEAN"
      }
    }
  }
  notification_channels = local.alert_channel_ids
  alert_strategy { auto_close = "1800s" }
}

# Unanswerable rate spike (product signal, not infra)
# The alert reads two log-based metrics rag-api's query log feeds and alerts on their ratio.
#
# The first version extracted rag-api's 0/1 unanswerable_flag into ONE distribution metric
# and asked Monitoring for ALIGN_MEAN over it. Monitoring refused, on the first live apply
# (the first live run, 6 September 2026): a mean is defined for numeric series, not distributions,
# and an alert filter has to name a resource type. Two counters and a ratio is what the API
# allows, and it is also the honest shape - a rate is a numerator over a denominator.
#
# Before that, NOTHING created the metric the alert read, so terraform applied cleanly, the
# policy showed green in the console, and it could never fire. An alert that cannot fire is
# worse than no alert: it is a promise someone is relying on. The flag is still emitted (0/1
# beside the boolean) because a log filter matches on it directly.
resource "google_logging_metric" "unanswerable" {
  name    = "documind/unanswerable"
  project = var.project_id
  filter  = <<EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="documind-api"
    (jsonPayload.event="query" OR jsonPayload.event="stream")
    jsonPayload.unanswerable_flag=1
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "tenant"
      value_type  = "STRING"
      description = "Tenant the question belonged to"
    }
  }
  label_extractors = {
    tenant = "EXTRACT(jsonPayload.tenant)"
  }
}

resource "google_logging_metric" "queries" {
  name    = "documind/queries"
  project = var.project_id
  filter  = <<EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="documind-api"
    (jsonPayload.event="query" OR jsonPayload.event="stream")
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "tenant"
      value_type  = "STRING"
      description = "Tenant the question belonged to"
    }
  }
  label_extractors = {
    tenant = "EXTRACT(jsonPayload.tenant)"
  }
}

resource "google_monitoring_alert_policy" "unanswerable_rate" {
  display_name = "Unanswerable rate > 20% for a tenant"
  combiner     = "OR"
  conditions {
    display_name = "unanswerable / queries > 0.20 for 30 minutes"
    condition_threshold {
      filter             = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/documind/unanswerable\""
      denominator_filter = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/documind/queries\""
      comparison         = "COMPARISON_GT"
      threshold_value    = 0.20
      duration           = "1800s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant"]
      }
      denominator_aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant"]
      }
    }
  }
  notification_channels = local.alert_channel_ids
  depends_on            = [google_logging_metric.unanswerable, google_logging_metric.queries]
}

# Cost control (10 September 2026): a GPU service left warm. instance_count is a gauge of a service's container
# instances, active or idle; a warm L4 instance is $1.42 an hour (11.1) and looks perfectly healthy for four weeks.
# Two hours above zero is the alarm - the nightly job in off.tf is the switch, this is the earlier warning, and
# make gpu-cap is the ceiling under both. A session runs two hours too, so the alarm fires near the end of one;
# that is the reminder, not a bug.
resource "google_monitoring_alert_policy" "gpu_left_warm" {
  for_each     = toset(["documind-slm", "documind-vllm"])
  display_name = "${each.key} left warm: instances > 0 for 2 hours"
  combiner     = "OR"
  conditions {
    display_name = "container instances > 0 for 2 hours"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND resource.labels.service_name=\"${each.key}\" AND metric.type=\"run.googleapis.com/container/instance_count\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "7200s"
      aggregations {
        alignment_period     = "300s"
        per_series_aligner   = "ALIGN_MAX"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["resource.label.service_name"]
      }
    }
  }
  notification_channels = local.alert_channel_ids
  alert_strategy { auto_close = "1800s" }
  documentation {
    content   = "An L4 instance has been up for two hours. If a session is not running: make off PROJECT=<project> (or wait for the 23:00 IST job), then check with `gcloud run services describe ${each.key}`. A warm instance is Rs 86,904 a month."
    mime_type = "text/markdown"
  }
}

# The document lifecycle, observed (12 September 2026, deploy/INDEXING.md). The ingest worker logs one structured
# line per event - ingest_ok, ingest_superseded, ingest_reactivated, ingest_stale_event, ingest_failed - with the
# counts a reindex cost (reused, embedded, retired); the nightly job ends on reconcile_done with the drift it
# measured. Three metrics read them off the log, and three policies turn them into a pager: a failure within ten
# minutes, drift that stays above zero for two nights, the job itself failing. Before these, every one of those
# lines was a log search somebody had to remember to run.
resource "google_logging_metric" "ingest_events" {
  name    = "documind/ingest_events"
  project = var.project_id
  filter  = <<EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="documind-ingest"
    jsonPayload.event=~"^ingest_(ok|superseded|reactivated|stale_event|failed)$"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "INT64"
    unit        = "1"
    labels {
      key         = "event"
      value_type  = "STRING"
      description = "ingest_ok | ingest_superseded | ingest_reactivated | ingest_stale_event | ingest_failed"
    }
    labels {
      key         = "tenant"
      value_type  = "STRING"
      description = "Tenant the object belonged to"
    }
  }
  label_extractors = {
    event  = "EXTRACT(jsonPayload.event)"
    tenant = "EXTRACT(jsonPayload.tenant)"
  }
}

# What a reindex costs, as a distribution of the embedded count per ingest_ok: the number the carry-over keeps
# small. reused rides beside it; a chart of the two is "pay for what changed" made visible.
resource "google_logging_metric" "ingest_embedded" {
  name    = "documind/ingest_embedded"
  project = var.project_id
  filter  = <<EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="documind-ingest"
    jsonPayload.event="ingest_ok"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "1"
    labels {
      key         = "tenant"
      value_type  = "STRING"
      description = "Tenant the object belonged to"
    }
  }
  value_extractor = "EXTRACT(jsonPayload.embedded)"
  label_extractors = {
    tenant = "EXTRACT(jsonPayload.tenant)"
  }
  bucket_options {
    explicit_buckets {
      bounds = [0, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500]
    }
  }
}

resource "google_logging_metric" "ingest_reused" {
  name    = "documind/ingest_reused"
  project = var.project_id
  filter  = <<EOT
    resource.type="cloud_run_revision"
    resource.labels.service_name="documind-ingest"
    jsonPayload.event="ingest_ok"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "1"
    labels {
      key         = "tenant"
      value_type  = "STRING"
      description = "Tenant the object belonged to"
    }
  }
  value_extractor = "EXTRACT(jsonPayload.reused)"
  label_extractors = {
    tenant = "EXTRACT(jsonPayload.tenant)"
  }
  bucket_options {
    explicit_buckets {
      bounds = [0, 1, 2, 5, 10, 25, 50, 100, 250, 500, 1000, 2500]
    }
  }
}

# The night's number. reconcile.py prints reconcile_done with `drift` from the job (resource.type cloud_run_job);
# a metadata-only change is not drift, a lost event or a deleted object is.
resource "google_logging_metric" "reconcile_drift" {
  name    = "documind/reconcile_drift"
  project = var.project_id
  filter  = <<EOT
    resource.type="cloud_run_job"
    resource.labels.job_name="documind-reconcile"
    jsonPayload.event="reconcile_done"
  EOT
  metric_descriptor {
    metric_kind = "DELTA"
    value_type  = "DISTRIBUTION"
    unit        = "1"
  }
  value_extractor = "EXTRACT(jsonPayload.drift)"
  bucket_options {
    explicit_buckets {
      bounds = [0, 1, 2, 5, 10, 50, 100]
    }
  }
}

resource "google_monitoring_alert_policy" "ingest_failed" {
  display_name = "Ingest failed"
  combiner     = "OR"
  conditions {
    display_name = "an ingest_failed line in the last ten minutes"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_revision\" AND metric.type=\"logging.googleapis.com/user/documind/ingest_events\" AND metric.label.event=\"ingest_failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
        group_by_fields      = ["metric.label.tenant"]
      }
    }
  }
  notification_channels = local.alert_channel_ids
  alert_strategy { auto_close = "1800s" }
  documentation {
    content   = "A document failed to index (the claim was released, the message is retrying towards the DLQ). Read the line: gcloud logging read 'jsonPayload.event=\"ingest_failed\"' --limit 5; then make dlq. A poison object is ingest_poison, not this."
    mime_type = "text/markdown"
  }
  depends_on = [google_logging_metric.ingest_events]
}

# Two nights, not one: a single night's drift is a lost event the reconcile just repaired; drift that is still
# above zero after the next run is a lane nobody is reconciling (the apply failing, the job not scheduled).
resource "google_monitoring_alert_policy" "reconcile_drift" {
  count        = var.reconcile_job ? 1 : 0
  display_name = "Ledger drift above zero for two nights"
  combiner     = "OR"
  conditions {
    display_name = "reconcile_done.drift > 0 for 24 hours (two nightly runs)"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_job\" AND metric.type=\"logging.googleapis.com/user/documind/reconcile_drift\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "86400s"
      aggregations {
        alignment_period     = "86400s"
        per_series_aligner   = "ALIGN_PERCENTILE_99"
        cross_series_reducer = "REDUCE_MAX"
      }
    }
  }
  notification_channels = local.alert_channel_ids
  documentation {
    content   = "The nightly reconcile found the ledger out of step with the uploads bucket two runs in a row. make reconcile PROJECT=<project> prints the plan; make reconcile APPLY=1 acts; make sources TENANT= shows the ledger."
    mime_type = "text/markdown"
  }
  depends_on = [google_logging_metric.reconcile_drift]
}

resource "google_monitoring_alert_policy" "reconcile_failed" {
  count        = var.reconcile_job ? 1 : 0
  display_name = "Nightly reconcile failed"
  combiner     = "OR"
  conditions {
    display_name = "a documind-reconcile task attempt failed"
    condition_threshold {
      filter          = "resource.type=\"cloud_run_job\" AND resource.labels.job_name=\"documind-reconcile\" AND metric.type=\"run.googleapis.com/job/completed_task_attempt_count\" AND metric.labels.result=\"failed\""
      comparison      = "COMPARISON_GT"
      threshold_value = 0
      duration        = "0s"
      aggregations {
        alignment_period     = "3600s"
        per_series_aligner   = "ALIGN_DELTA"
        cross_series_reducer = "REDUCE_SUM"
      }
    }
  }
  notification_channels = local.alert_channel_ids
  alert_strategy { auto_close = "86400s" }
  documentation {
    content   = "documind-reconcile did not finish. gcloud run jobs executions list --job documind-reconcile, then the execution's logs; run it by hand with gcloud run jobs execute documind-reconcile --region <region>."
    mime_type = "text/markdown"
  }
}

# The e-mail channel. PagerDuty is the full profile's on-call; on the lane the admins' addresses are the on-call
# (make up passes ALERT_EMAILS, derived from ADMIN_EMAILS unless that is still the placeholder), and every policy
# in this file notifies both channels when both exist.
variable "alert_emails" {
  type    = list(string)
  default = []
}

resource "google_monitoring_notification_channel" "email" {
  for_each     = toset(var.alert_emails)
  display_name = "DocuMind admin ${each.key}"
  type         = "email"
  labels       = {
    email_address = each.key
  }
}

locals {
  alert_channel_ids = concat(google_monitoring_notification_channel.oncall[*].id, [for c in google_monitoring_notification_channel.email : c.id])
}
