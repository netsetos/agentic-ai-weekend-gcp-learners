# The STREAM_UPDATE index, provisioned for the first time in this lesson.
# Modules 4 and 12.2 have been querying an index that the course never created -
# this is it.
resource "google_vertex_ai_index" "documind" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  region       = var.region
  display_name = "documind-chunks"
  description  = "DocuMind chunk embeddings, 768-d, streaming upserts"

  metadata {
    contents_delta_uri = "gs://${google_storage_bucket.uploads.name}/index-delta"
    config {
      dimensions                  = 768
      approximate_neighbors_count = 150
      distance_measure_type       = "DOT_PRODUCT_DISTANCE"
      algorithm_config {
        tree_ah_config {
          leaf_node_embedding_count    = 500
          leaf_nodes_to_search_percent = 7
        }
      }
    }
  }

  # STREAM_UPDATE, not BATCH_UPDATE. With BATCH_UPDATE the worker's
  # upsert_datapoints call is accepted and applied at the next batch job, so an
  # uploaded document is simply absent for hours and nothing reports an error.
  index_update_method = "STREAM_UPDATE"
}

resource "google_vertex_ai_index_endpoint" "documind" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  region                  = var.region
  display_name            = "documind-endpoint"
  public_endpoint_enabled = true
}

# An index and an endpoint are two things; neither of them serves a query. The
# DEPLOYED index is the third, and it is the one that costs money per hour - which
# is why it is easy to leave out of the terraform and then wonder why
# find_neighbors returns nothing against an index that plainly exists.
resource "google_vertex_ai_index_endpoint_deployed_index" "documind" {
  count = local.full ? 1 : 0   # the full profile only (variables.tf)
  index_endpoint    = google_vertex_ai_index_endpoint.documind[0].id
  index             = google_vertex_ai_index.documind[0].id
  deployed_index_id = "documind_chunks_v1"
  display_name      = "documind-chunks-v1"

  # One small replica. This is the line to raise for a live cohort and the line
  # to drop to zero afterwards - see the scale-down runbook in deploy/README.
  dedicated_resources {
    machine_spec { machine_type = "e2-standard-2" }
    min_replica_count = 1
    max_replica_count = 1
  }
}

# These two outputs are the whole contract with rag-api/config.py, which reads
# them as VECTOR_INDEX_ENDPOINT and VECTOR_DEPLOYED_INDEX_ID. The endpoint output
# is the RESOURCE NAME, not the public domain: retriever.py passes it straight to
# aiplatform.MatchingEngineIndexEndpoint(), which wants the name.
output "vector_index_endpoint" {
  description = "VECTOR_INDEX_ENDPOINT for rag-api"
  value       = one(google_vertex_ai_index_endpoint.documind[*].id)
}

output "vector_deployed_index_id" {
  description = "VECTOR_DEPLOYED_INDEX_ID for rag-api"
  value       = one(google_vertex_ai_index_endpoint_deployed_index.documind[*].deployed_index_id)
}

output "vector_index_name" {
  description = "VECTOR_INDEX_NAME for the ingest worker's upsert_datapoints"
  value       = one(google_vertex_ai_index.documind[*].id)
}
