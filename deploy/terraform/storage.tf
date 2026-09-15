resource "google_storage_bucket" "uploads" {
  name                        = "${var.project_id}-uploads"
  location                    = var.india_region # in-region residency (best practice, not a DPDP mandate)
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }
  lifecycle_rule {
    condition { age = 90 }
    action {
      type          = "SetStorageClass"
      storage_class = "NEARLINE"
    }
  }
  lifecycle_rule {
    condition { age = 365 }
    action { type = "Delete" }
  }
  cors {
    origin          = ["https://documind.example.com"]
    method          = ["GET", "PUT", "POST"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }
}

resource "google_storage_bucket" "tts_cache" {
  name                        = "${var.project_id}-tts-cache"
  location                    = var.india_region
  uniform_bucket_level_access = true
  lifecycle_rule {
    condition { age = 30 }
    action { type = "Delete" }
  }
}

# 9.4's Media Studio bucket (gap G8). Same shape as tts_cache: india_region, uniform access,
# a 30-day delete rule - generated media is a cache, not a record. The AUDIT row that says it
# existed is the record, and that bucket keeps its rows for five years. The cors block is
# not optional: a browser PUT to a signed URL is a cross-origin request, and without it the
# upload fails in the browser while working perfectly from curl.
resource "google_storage_bucket" "media" {
  name                        = "${var.project_id}-media"
  location                    = var.india_region
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  lifecycle_rule {
    condition { age = 30 }
    action { type = "Delete" }
  }
  cors {
    origin          = ["https://documind.example.com"]
    method          = ["PUT"]
    response_header = ["Content-Type"]
    max_age_seconds = 3600
  }
}

# Module 10: the tuning dataset is a document (make trainset writes it here, frozen, with its manifest)
# and the tuned Gemma is a file (10.5's GGUF; the Ollama image 11.4 builds copies it from here).
# Versioned and never expiring: a training file that moved is a model nobody can defend. The owner
# writes; the API's account may read (a model_backend that loads from here is Module 11's).
resource "google_storage_bucket" "datasets" {
  name                        = "${var.project_id}-datasets"
  location                    = var.india_region
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"
  versioning { enabled = true }
}
resource "google_storage_bucket_iam_member" "api_datasets" {
  bucket = google_storage_bucket.datasets.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.api.email}"
}

resource "google_storage_bucket" "audit" {
  name                        = "${var.project_id}-audit"
  location                    = var.india_region
  uniform_bucket_level_access = true
  retention_policy {
    retention_period = 157680000 # 5 years -- audit/RBI retention best practice (DPDP Act sets no fixed number)
    # Locked only when var.audit_lock says so (15 September 2026; it was the full profile's). A LOCKED policy is
    # irreversible: the bucket cannot be deleted until its last object ages out, and Google liens the project so the
    # project cannot be deleted either - five years, for a throwaway lab. The term is the record's protection (an
    # unlocked policy still refuses to delete or overwrite an object inside it); the lock is what a production
    # account adds, on purpose: make up AUDIT_LOCK=true.
    is_locked = var.audit_lock
  }
}

# audit_log.emit refuses to drop an event, so a writer without this grant fails its request
# outright: the worker (doc.upload, dlp.finding) and the API's media router (9.4) write here.
# objectCreator, not objectAdmin - the bucket's retention policy refuses every delete (locked when var.audit_lock says so),
# so nothing may delete.
resource "google_storage_bucket_iam_member" "ingest_audit" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.ingest.email}"
}
resource "google_storage_bucket_iam_member" "api_audit" {
  bucket = google_storage_bucket.audit.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.api.email}"
}

# UI can read/write uploads + TTS cache only
resource "google_storage_bucket_iam_member" "ui_uploads" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ui.email}"
}
resource "google_storage_bucket_iam_member" "ui_tts" {
  bucket = google_storage_bucket.tts_cache.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.ui.email}"
}

# The API signs 9.4's upload URLs into the UPLOADS bucket - the one with the object.finalized
# notification - under the tenant's prefix. A V4 signed URL is authorised AS its signer, so
# the signer needs objectCreator here and nothing more (it never reads uploads; the worker does).
resource "google_storage_bucket_iam_member" "api_uploads" {
  bucket = google_storage_bucket.uploads.name
  role   = "roles/storage.objectCreator"
  member = "serviceAccount:${google_service_account.api.email}"
}

# Media (9.4 / 9.6, gaps G7-G8): rag-api WRITES generated assets and signs upload URLs;
# the UI only READS, through the V4 signed URLs it mints for figure and segment citations -
# a signed URL is authorised as its signer, so the signer needs objectViewer and nothing more.
resource "google_storage_bucket_iam_member" "api_media" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectAdmin"
  member = "serviceAccount:${google_service_account.api.email}"
}
resource "google_storage_bucket_iam_member" "ui_media" {
  bucket = google_storage_bucket.media.name
  role   = "roles/storage.objectViewer"
  member = "serviceAccount:${google_service_account.ui.email}"
}
