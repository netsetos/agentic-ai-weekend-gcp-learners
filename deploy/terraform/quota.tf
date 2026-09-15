# A consumer quota override, so a runaway cannot spend the whole month in an
# afternoon. This is the ceiling BELOW the budget alert: the alert tells you
# afterwards, the quota stops it happening.
resource "google_service_usage_consumer_quota_override" "gemini_rpm" {
  # Behind var.gemini_quota_override (variables.tf), off by default. The metric and limit names below
  # are the shape the API documents, not names verified against `gcloud alpha services quota list
  # --service=aiplatform.googleapis.com` on a project, and an unknown metric fails the whole apply.
  # Verify the names, then GEMINI_QUOTA_OVERRIDE=true - and once they are known, this count can go.
  count          = var.gemini_quota_override ? 1 : 0
  provider       = google-beta
  project        = var.project_id
  service        = "aiplatform.googleapis.com"
  metric         = "aiplatform.googleapis.com%2Fgenerate_content_requests"
  limit          = "%2Fmin%2Fproject"
  override_value = "600"
  force          = true
}

# The alerts that say something is wrong with the SYSTEM, not with a request.
# Each one names a failure this course has already met.
locals {
  documind_alerts = {
    # 12.3: a tenant whose questions the corpus cannot answer
    unanswerable_rate = "documind/unanswerable_rate > 0.20 for 30m"
    # 12.5: a poison message reached the dead-letter topic
    dlq_depth = "pubsub subscription documind-ingest-dlq num_undelivered > 0"
    # 12.6: the guard is blocking a lot, which is either an attack or a bug
    guardrail_blocks = "documind/guardrail_block_rate > 0.05 for 15m"
    # 12.6: the semantic cache stopped paying for itself
    cache_hit_low = "documind/cache_hit_rate < 0.30 for 1h"
    # SRE: error budget burning faster than the month can absorb
    burn_rate = "slo burn_rate > 14.4 for 1h"
  }
}
