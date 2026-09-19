# Module 5's production lane: the feature tables and the quality gate. Lesson 5.5, adopted by
# 12.3 (gap G9).
#
# 5.5 taught this against a synthetic fixture in a dataset called rag_data. The SAME dataset
# name is used here, on purpose: every SQL statement in 5.5 runs unchanged against production,
# and what fills chunk_source is no longer a fixture - services/ingest/indexer.py streams one
# row per chunk it indexes (BQ_CHUNK_TABLE), with the canonical names 2.3/4.x/rag-api use and
# the DLP verdict the worker already computed. So "the SQL lane reads DocuMind's real chunks"
# is literally what happens, and pii_flag needs no second scan and no BigQuery model.
#
# Terraform DECLARES the tables (schemas), sql/chunk_metadata.sql COMPUTES them (`make features`):
# cheap features -> MERGE into chunk_metadata -> ingest_events -> the index_feed by subtraction.
# Declaring index_feed here rather than CREATE OR REPLACE-ing it is what lets the Dataplex scan
# below exist before the first job has run.
#
# The product was renamed - Dataplex Universal Catalog became Knowledge Catalog on 10 April 2026
# - but the API, the provider resource and the IAM roles all still say dataplex, which is why
# everything below does.

resource "google_project_service" "dataplex" {
  service            = "dataplex.googleapis.com"
  disable_on_destroy = false
}

# Enabling the API does not ensure its service agent exists on a new project.
# Generate (or reuse) it before granting its service role and creating the scan.
resource "google_project_service_identity" "dataplex" {
  provider = google-beta
  project  = var.project_id
  service  = google_project_service.dataplex.service
}

# Proactively generated service agents need their normal service-agent role.
# An additive member preserves the project's other IAM bindings.
resource "google_project_iam_member" "dataplex_service_agent" {
  project = var.project_id
  role    = "roles/dataplex.serviceAgent"
  member  = "serviceAccount:${google_project_service_identity.dataplex.email}"
}

resource "google_bigquery_dataset" "rag_data" {
  dataset_id                 = "rag_data"
  location                   = var.india_region
  description                = "Chunk features, ingest events and the index feed (lesson 5.5); chunk_source is streamed by the ingest worker"
  delete_contents_on_destroy = true # a learner's throwaway project: make down must take it
}

# The worker writes; the admin surface already reads project-wide (sa.tf).
resource "google_bigquery_dataset_iam_member" "ingest_writer" {
  dataset_id = google_bigquery_dataset.rag_data.dataset_id
  role       = "roles/bigquery.dataEditor"
  member     = "serviceAccount:${google_service_account.ingest.email}"
}

resource "google_bigquery_table" "chunk_source" {
  dataset_id          = google_bigquery_dataset.rag_data.dataset_id
  table_id            = "chunk_source"
  deletion_protection = false
  description         = "One row per indexed chunk, streamed by services/ingest - the canonical document, plus the worker's DLP verdict"
  time_partitioning {
    type  = "DAY"
    field = "ingested_at"
  }
  clustering = ["tenant_id", "doc_type"]
  schema = jsonencode([
    { name = "chunk_id", type = "STRING", mode = "REQUIRED", description = "Firestore document id in chunks" },
    { name = "tenant_id", type = "STRING", mode = "REQUIRED" },
    { name = "text", type = "STRING", mode = "NULLABLE" },
    { name = "source_uri", type = "STRING", mode = "NULLABLE" },
    { name = "page_start", type = "INTEGER", mode = "NULLABLE" },
    { name = "page_end", type = "INTEGER", mode = "NULLABLE" },
    { name = "doc_type", type = "STRING", mode = "NULLABLE" },
    { name = "kind", type = "STRING", mode = "NULLABLE", description = "text | figure | table | segment" },
    { name = "heading_path", type = "STRING", mode = "NULLABLE" },
    { name = "last_revised_at", type = "DATE", mode = "NULLABLE" },
    { name = "pii_flag", type = "BOOLEAN", mode = "NULLABLE", description = "TRUE keeps the chunk OUT of the shared index" },
    { name = "ingested_at", type = "TIMESTAMP", mode = "NULLABLE" },
  ])
}

resource "google_bigquery_table" "chunk_metadata" {
  dataset_id          = google_bigquery_dataset.rag_data.dataset_id
  table_id            = "chunk_metadata"
  deletion_protection = false
  description         = "One row per chunk: the feature table and the join key back to the corpus (5.5). No text, no vectors."
  time_partitioning {
    type  = "DAY"
    field = "featured_at"
  }
  clustering = ["tenant_id", "doc_type"]
  schema = jsonencode([
    { name = "chunk_id", type = "STRING", mode = "REQUIRED" },
    { name = "tenant_id", type = "STRING", mode = "REQUIRED" },
    { name = "source_uri", type = "STRING", mode = "NULLABLE" },
    { name = "page_start", type = "INTEGER", mode = "NULLABLE" },
    { name = "page_end", type = "INTEGER", mode = "NULLABLE" },
    { name = "token_count", type = "INTEGER", mode = "NULLABLE", description = "Approximate: characters / 4" },
    { name = "heading_depth", type = "INTEGER", mode = "NULLABLE" },
    { name = "has_table", type = "BOOLEAN", mode = "NULLABLE" },
    { name = "has_figure", type = "BOOLEAN", mode = "NULLABLE" },
    { name = "language", type = "STRING", mode = "NULLABLE", description = "en | hi | mixed" },
    { name = "freshness_days", type = "INTEGER", mode = "NULLABLE" },
    { name = "doc_type", type = "STRING", mode = "NULLABLE" },
    { name = "doc_type_conf", type = "FLOAT", mode = "NULLABLE" },
    { name = "pii_flag", type = "BOOLEAN", mode = "NULLABLE" },
    { name = "featured_at", type = "TIMESTAMP", mode = "NULLABLE" },
  ])
}

resource "google_bigquery_table" "ingest_events" {
  dataset_id          = google_bigquery_dataset.rag_data.dataset_id
  table_id            = "ingest_events"
  deletion_protection = false
  description         = "Append-only: what changed and when. 'withheld' is the row an auditor asks about."
  time_partitioning {
    type  = "DAY"
    field = "occurred_at"
  }
  clustering = ["tenant_id", "event_type"]
  schema = jsonencode([
    { name = "event_id", type = "STRING", mode = "REQUIRED" },
    { name = "chunk_id", type = "STRING", mode = "REQUIRED" },
    { name = "tenant_id", type = "STRING", mode = "REQUIRED" },
    { name = "event_type", type = "STRING", mode = "REQUIRED", description = "created | updated | reindexed | withheld" },
    { name = "reason", type = "STRING", mode = "NULLABLE" },
    { name = "occurred_at", type = "TIMESTAMP", mode = "REQUIRED" },
  ])
}

resource "google_bigquery_table" "index_feed" {
  dataset_id          = google_bigquery_dataset.rag_data.dataset_id
  table_id            = "index_feed"
  deletion_protection = false
  description         = "What SHIPS to the index - chunk_metadata minus PII and minus fragments, by subtraction. The scan below gates on this table."
  clustering          = ["tenant_id", "doc_type"]
  schema = jsonencode([
    { name = "chunk_id", type = "STRING", mode = "REQUIRED" },
    { name = "tenant_id", type = "STRING", mode = "REQUIRED" },
    { name = "doc_type", type = "STRING", mode = "NULLABLE" },
    { name = "language", type = "STRING", mode = "NULLABLE" },
    { name = "token_count", type = "INTEGER", mode = "NULLABLE" },
    { name = "freshness_days", type = "INTEGER", mode = "NULLABLE" },
    { name = "heading_depth", type = "INTEGER", mode = "NULLABLE" },
    { name = "has_table", type = "BOOLEAN", mode = "NULLABLE" },
  ])
}

# The gate - 5.5 cell 9's three rules, on the table that ships. If they fail, the index does
# not get rebuilt. On demand: `make features` runs the job and then the scan, and reads the
# result the way 5.5's dq_gate() does.
resource "google_dataplex_datascan" "chunk_dq" {
  location     = var.india_region
  data_scan_id = "documind-chunk-dq"
  display_name = "DocuMind index feed quality"

  data {
    resource = "//bigquery.googleapis.com/projects/${var.project_id}/datasets/${google_bigquery_dataset.rag_data.dataset_id}/tables/${google_bigquery_table.index_feed.table_id}"
  }

  execution_spec {
    trigger {
      on_demand {}
    }
  }

  data_quality_spec {
    # 1. one row per chunk - a duplicate is a duplicated citation
    rules {
      column    = "chunk_id"
      dimension = "UNIQUENESS"
      uniqueness_expectation {}
    }
    # 2. below 32 tokens a chunk carries no answer; above 2,048 it will not survive the packer.
    #    On the feed this is a regression test that the subtraction in the job still happens.
    rules {
      column    = "token_count"
      dimension = "VALIDITY"
      threshold = 0.99
      range_expectation {
        min_value = "32"
        max_value = "2048"
      }
    }
    # 3. nothing flagged as PII may be in the feed. Passes when the statement returns no rows.
    rules {
      dimension = "VALIDITY"
      sql_assertion {
        sql_statement = "SELECT chunk_id FROM `${var.project_id}.rag_data.chunk_metadata` WHERE pii_flag AND chunk_id IN (SELECT chunk_id FROM `${var.project_id}.rag_data.index_feed`)"
      }
    }
  }

  depends_on = [google_project_iam_member.dataplex_service_agent]
}

output "chunk_dq_scan" { value = google_dataplex_datascan.chunk_dq.name }
