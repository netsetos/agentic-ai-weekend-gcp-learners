# The graph 4.6 builds. UNOWNED: lesson 4.6 teaches the DDL and the queries but is in
# Module 4, which extract_documind.py does not map - it reads Module 12 only.
#
# FREE TRIAL, deliberately. Spanner Graph requires ENTERPRISE edition, and a trial
# instance is Enterprise for 90 days at Rs 0: 10 GB, at most 5 databases, one per
# project. That is what makes 4.6 runnable for a learner rather than a Rs 50,000/month
# lesson. The trial for this cohort stops Fri 4 Dec 2026 - the `pricing` row re-runs
# 4.6 before then or moves it to the Neo4j Free mirror.
resource "google_spanner_instance" "graph" {
  name             = "documind-graph"
  config           = "regional-asia-south1" # graph data at rest stays in Mumbai
  display_name     = "DocuMind knowledge graph"
  edition          = "ENTERPRISE" # Spanner Graph needs it
  processing_units = 100          # the trial minimum
  force_destroy    = false
}

resource "google_spanner_database" "graph" {
  instance = google_spanner_instance.graph.name
  name     = "documind"

  # Byte-for-byte the DDL 4.6 applies, so a learner who ran the notebook and an
  # operator who ran terraform end up with the same schema. CREATE TABLE IF NOT
  # EXISTS and CREATE OR REPLACE PROPERTY GRAPH are both re-runnable.
  ddl = [
    <<-EOT
      CREATE TABLE IF NOT EXISTS GraphNode (
        tenant_id  STRING(64)  NOT NULL,
        node_id    STRING(128) NOT NULL,
        kind       STRING(64),
        name       STRING(512),
        chunk_id   STRING(128),
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
      ) PRIMARY KEY (tenant_id, node_id, dst_id, rel)
    EOT
    ,
    <<-EOT
      CREATE OR REPLACE PROPERTY GRAPH DocuMindGraph
        NODE TABLES (GraphNode KEY (tenant_id, node_id))
        EDGE TABLES (
          GraphEdge KEY (tenant_id, node_id, dst_id, rel)
            SOURCE KEY (tenant_id, node_id) REFERENCES GraphNode (tenant_id, node_id)
            DESTINATION KEY (tenant_id, dst_id) REFERENCES GraphNode (tenant_id, node_id)
        )
    EOT
  ]

  deletion_protection = true
}

# The graph-builder job reads chunks and writes nodes/edges. It is a job, not a
# service: it runs per document off the graph-jobs topic and exits.
resource "google_service_account" "graph_builder" {
  account_id   = "documind-graph-sa"
  display_name = "DocuMind graph-builder job"
}

resource "google_spanner_database_iam_member" "graph_builder" {
  instance = google_spanner_instance.graph.name
  database = google_spanner_database.graph.name
  role     = "roles/spanner.databaseUser"
  member   = "serviceAccount:${google_service_account.graph_builder.email}"
}

output "spanner_instance" { value = google_spanner_instance.graph.name }
output "spanner_database" { value = google_spanner_database.graph.name }
