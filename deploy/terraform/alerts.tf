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
