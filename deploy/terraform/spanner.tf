# The graph 4.6 builds - Spanner Graph, on the lane since 16 September 2026 (GRAPH_BACKEND=spanner on the API and
# make graph; shared/documind_graph.SpannerGraph). Until then the instance was provisioned and unread: the lane's
# graph lived in Firestore only, and the DDL here was not the lesson's - no interleave, no cascade, no updated_at,
# no edge label - so 4.6's own load and walk failed against it. It is the lesson's DDL now, plus one column the
# lesson does not have: `embedding`, so a walk can be SEEDED BY MEANING (COSINE_DISTANCE on the node names' vectors)
# instead of by a name the question happens to contain.
#
# WHAT THIS COSTS. This is a PROVISIONED instance - Enterprise edition (Spanner Graph needs it), 100 processing
# units, the smallest a provisioned instance can be - and it bills by the hour while it exists. The comment that
# used to call it a free trial was wrong: a free-trial instance is created with `gcloud spanner instances create
# --instance-type free-instance` (4.6's cell 5: 90 days, 10 GiB, one per project, Spanner Graph included) and is
# not what this resource declares. Take the trial by hand and point a data source at it, or price this on the
# Spanner pricing page for regional-asia-south1 before a cohort. Either way, `make down` removes it.
resource "google_spanner_instance" "graph" {
  name             = "documind-graph"
  config           = "regional-asia-south1" # graph data at rest stays in Mumbai
  display_name     = "DocuMind knowledge graph"
  edition          = "ENTERPRISE" # Spanner Graph needs it
  processing_units = 100          # the provisioned minimum
  force_destroy    = true         # a lab instance: make down removes it with its database
}

resource "google_spanner_database" "graph" {
  instance = google_spanner_instance.graph.name
  name     = "documind"

  # 4.6's DDL (cell 7), byte for byte in what matters, plus `embedding`: tenant_id is the FIRST primary-key column
  # on both tables, every query filters on it, and edges are INTERLEAVED in their source node so a tenant's
  # subgraph is physically co-located and a tenant delete cascades - the DPDP erasure path as a schema property.
  # The property graph names the edge label RELATES_TO, which the walk (SpannerGraph.EXPAND_GQL) matches on.
  # `embedding` is ARRAY<FLOAT32>(vector_length=>768): text-embedding-005 on the canonical node name, written by
  # make graph GRAPH_BACKEND=spanner; COSINE_DISTANCE over it is exact and enough for a graph of thousands of nodes.
  # The ANN step for millions - CREATE VECTOR INDEX ... OPTIONS (distance_type='COSINE') and APPROX_COSINE_DISTANCE -
  # is deliberately not here: it needs tuning that a lab cannot judge.
  ddl = [
    <<-EOT
      CREATE TABLE IF NOT EXISTS GraphNode (
        tenant_id  STRING(64)  NOT NULL,
        node_id    STRING(128) NOT NULL,
        kind       STRING(64)  NOT NULL,
        name       STRING(MAX) NOT NULL,
        chunk_ids  ARRAY<STRING(128)>,
        embedding  ARRAY<FLOAT32>(vector_length=>768),
        updated_at TIMESTAMP OPTIONS (allow_commit_timestamp=true),
      ) PRIMARY KEY (tenant_id, node_id)
    EOT
    ,
    <<-EOT
      CREATE TABLE IF NOT EXISTS GraphEdge (
        tenant_id  STRING(64)  NOT NULL,
        node_id    STRING(128) NOT NULL,
        dst_id     STRING(128) NOT NULL,
        rel        STRING(64)  NOT NULL,
        chunk_id   STRING(128),
        confidence FLOAT64,
      ) PRIMARY KEY (tenant_id, node_id, dst_id, rel),
        INTERLEAVE IN PARENT GraphNode ON DELETE CASCADE
    EOT
    ,
    <<-EOT
      CREATE OR REPLACE PROPERTY GRAPH DocuMindGraph
        NODE TABLES (GraphNode)
        EDGE TABLES (
          GraphEdge
            SOURCE KEY (tenant_id, node_id) REFERENCES GraphNode (tenant_id, node_id)
            DESTINATION KEY (tenant_id, dst_id) REFERENCES GraphNode (tenant_id, node_id)
            LABEL RELATES_TO
        )
    EOT
  ]

  deletion_protection = false # a lab database; the project is thrown away. True on a real tenant's graph.
}

# The graph-builder job reads chunks and writes nodes/edges. It is a job, not a
# service: it runs per document off the graph-jobs topic and exits.
resource "google_service_account" "graph_builder" {
  account_id   = "documind-graph-sa"
  display_name = "DocuMind graph-builder job"
}

# Who reads and writes the graph (16 September 2026): the builder, the API (retriever.graph_candidates walks it
# when GRAPH_BACKEND=spanner) and the ingest worker's account, which make graph runs as on the lane.
resource "google_spanner_database_iam_member" "graph_users" {
  for_each = {
    builder = google_service_account.graph_builder.email
    api     = google_service_account.api.email
    ingest  = google_service_account.ingest.email
  }
  instance = google_spanner_instance.graph.name
  database = google_spanner_database.graph.name
  role     = "roles/spanner.databaseUser"
  member   = "serviceAccount:${each.value}"
}

output "spanner_instance" { value = google_spanner_instance.graph.name }
output "spanner_database" { value = google_spanner_database.graph.name }
