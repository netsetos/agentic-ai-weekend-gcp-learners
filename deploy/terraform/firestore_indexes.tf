# Two vector indexes, both 768-d to match text-embedding-005.

# chunks: the chaos fallback for retriever.py. Written by indexer.py on every
# ingest; queried only when Vector Search is down.
resource "google_firestore_index" "chunks_vector" {
  project     = var.project_id
  database    = google_firestore_database.main.name
  collection  = "chunks"
  query_scope = "COLLECTION"

  # Equality filter FIRST, vector field LAST. Firestore will not accept the
  # reverse, and the error message points at an index name that looks correct.
  fields {
    field_path = "tenant_id"
    order      = "ASCENDING"
  }
  # Firestore records the document key between the ordered fields and the vector field, and
  # reports it back in that position. Declared here the same way, the provider reads the
  # index it wrote. Left out, every apply read a three-field index against a two-field
  # definition, planned a replacement, and the asynchronous deletion raced the re-creation
  # into a 409 - the first live applies (the first live run, 6 September 2026) looped on exactly that.
  fields {
    field_path = "__name__"
    order      = "ASCENDING"
  }
  fields {
    field_path = "embedding"
    vector_config {
      dimension = 768
      flat {}
    }
  }
}

# chunks, current only: the ledger's promise (12.5, 11 September 2026). A SECOND index, not a change to the
# first - a changed vector index is destroyed and re-created, and retrieval would be refused while it builds;
# this one builds beside the first, and RETRIEVAL_CURRENT_ONLY=on on the API is what starts using it, after
# `make backfill-current` has stamped the chunks written before the ledger.
resource "google_firestore_index" "chunks_current_vector" {
  project     = var.project_id
  database    = google_firestore_database.main.name
  collection  = "chunks"
  query_scope = "COLLECTION"

  fields {
    field_path = "tenant_id"
    order      = "ASCENDING"
  }
  fields {
    field_path = "current"
    order      = "ASCENDING"
  }
  fields {
    field_path = "__name__"
    order      = "ASCENDING"
  }
  fields {
    field_path = "embedding"
    vector_config {
      dimension = 768
      flat {}
    }
  }
}

# answer_cache: the semantic cache from 12.6. Same shape, different collection -
# and the tenant_id filter is what stops one customer's answer reaching another.
resource "google_firestore_index" "answer_cache_vector" {
  project     = var.project_id
  database    = google_firestore_database.main.name
  collection  = "answer_cache"
  query_scope = "COLLECTION"

  fields {
    field_path = "tenant_id"
    order      = "ASCENDING"
  }
  # Firestore records the document key between the ordered fields and the vector field, and
  # reports it back in that position. Declared here the same way, the provider reads the
  # index it wrote. Left out, every apply read a three-field index against a two-field
  # definition, planned a replacement, and the asynchronous deletion raced the re-creation
  # into a 409 - the first live applies (the first live run, 6 September 2026) looped on exactly that.
  fields {
    field_path = "__name__"
    order      = "ASCENDING"
  }
  fields {
    field_path = "embedding"
    vector_config {
      dimension = 768
      flat {}
    }
  }
}

# Retention (12 September 2026, deploy/INDEXING.md). The ledger retires a chunk row with a flag and a stamp -
# expire_at = superseded_at + retention_days (variables.tf; the worker reads it as RETENTION_DAYS) - and this
# TTL policy is the ONLY thing that ever deletes one: the platform removes the row within about a day of the
# stamp, no account on the lane needs a delete, no cron runs one. The window is the audit window (a citation
# can still open what it quoted) and the undo window (reactivate clears the stamp) at once. A staged version
# the worker never swapped carries a one-day stamp and leaves the same way.
resource "google_firestore_field" "chunks_expire_at" {
  project    = var.project_id
  database   = google_firestore_database.main.name
  collection = "chunks"
  field      = "expire_at"

  ttl_config {}
}

# The roster's reverse lookup (shared/tenancy.py tenant_for, lesson 12.8: "which tenant is
# this person on?") is a COLLECTION-GROUP query on members.email, across every tenant's
# members at once. Firestore indexes a single field at collection scope by default and
# refuses a collection-group query on it - the UI's first page was a FailedPrecondition
# on the first live sign-in (the first live run, 6 September 2026). This field override adds the
# collection-group index; the point lookup (is_member) reads by document id and needs none.
resource "google_firestore_field" "members_email" {
  project    = var.project_id
  database   = google_firestore_database.main.name
  collection = "members"
  field      = "email"
  index_config {
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION"
    }
    indexes {
      order       = "ASCENDING"
      query_scope = "COLLECTION_GROUP"
    }
  }
}
