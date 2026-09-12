# GCS -> Pub/Sub -> Cloud Run, with a dead-letter topic.
#
# The bucket publishes its own object.finalized records (a Cloud Storage notification,
# payload JSON_API_V1) into the topic below, and the push subscription delivers them to the
# worker with an OIDC token and a floor of five attempts. The worker's IngestMessage is that
# record, field for field. An earlier draft declared an Eventarc trigger here instead: Eventarc
# delivers a CloudEvent straight to the service, around this subscription, its token, its retry
# ceiling and its DLQ - and the worker, which parses a Pub/Sub envelope, answered 400 to it.
data "google_project" "current" {}
data "google_storage_project_service_account" "gcs" {}

resource "google_pubsub_topic" "ingest" { name = "documind-ingest" }
resource "google_pubsub_topic" "ingest_dlq" { name = "documind-ingest-dlq" }

# Cloud Storage publishes as its own service agent. Without this grant the notification is
# created and nothing ever arrives - the first silent failure on this path.
resource "google_pubsub_topic_iam_member" "gcs_publishes" {
  topic  = google_pubsub_topic.ingest.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:${data.google_storage_project_service_account.gcs.email_address}"
}

resource "google_storage_notification" "uploads" {
  bucket         = google_storage_bucket.uploads.name
  topic          = google_pubsub_topic.ingest.id
  payload_format = "JSON_API_V1"
  event_types    = ["OBJECT_FINALIZE"]
  depends_on     = [google_pubsub_topic_iam_member.gcs_publishes]
}

locals {
  # Cloud Run's deterministic URL: the service name and the project NUMBER, no hash to look
  # up after the first deploy. commands/lesson-12.2.sh builds SELF_URL the same way.
  ingest_url = "https://documind-ingest-${data.google_project.current.number}.${var.region}.run.app"
}

# Pub/Sub mints the push token AS the ingest service account, which takes this grant to
# Pub/Sub's own service agent - the second silent failure. The dead-letter hop needs the
# same agent to publish to the DLQ topic and to subscribe here.
resource "google_service_account_iam_member" "pubsub_mints_ingest_token" {
  service_account_id = google_service_account.ingest.name
  role               = "roles/iam.serviceAccountTokenCreator"
  member             = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_topic_iam_member" "dlq_publisher" {
  topic  = google_pubsub_topic.ingest_dlq.id
  role   = "roles/pubsub.publisher"
  member = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

resource "google_pubsub_subscription" "ingest_push" {
  name  = "documind-ingest-push"
  topic = google_pubsub_topic.ingest.name

  push_config {
    push_endpoint = local.ingest_url
    oidc_token {
      # The worker is --no-allow-unauthenticated. Pub/Sub mints an OIDC token
      # for this service account, so the endpoint is reachable by Pub/Sub and
      # by nothing else on the internet. commands/lesson-12.5.sh grants the
      # account run.invoker on the service once the service exists.
      service_account_email = google_service_account.ingest.email
    }
  }

  # At-least-once, with a ceiling: attempts with exponential backoff, then the message goes
  # to the DLQ instead of being retried for a week. Twelve, not five: a corpus upload lands
  # thirty objects at once, the worker takes one request per instance, and Cloud Run refuses
  # ("no available instance") while it cold-starts - the first live load sent a document
  # to the DLQ on five refusals that were never the document's fault.
  retry_policy {
    minimum_backoff = "10s"
    maximum_backoff = "600s"
  }
  dead_letter_policy {
    dead_letter_topic     = google_pubsub_topic.ingest_dlq.id
    max_delivery_attempts = 12
  }
  ack_deadline_seconds = 600
  depends_on           = [google_service_account_iam_member.pubsub_mints_ingest_token]
}

resource "google_pubsub_subscription_iam_member" "dlq_subscriber" {
  subscription = google_pubsub_subscription.ingest_push.name
  role         = "roles/pubsub.subscriber"
  member       = "serviceAccount:service-${data.google_project.current.number}@gcp-sa-pubsub.iam.gserviceaccount.com"
}

# Somewhere to read the poison from. The README's Tier B block pulls from it by name.
resource "google_pubsub_subscription" "ingest_dlq_sub" {
  name  = "ingest-dlq-sub"
  topic = google_pubsub_topic.ingest_dlq.name
}
