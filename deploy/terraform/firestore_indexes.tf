# Firestore indexes used by the production RAG and graph paths.

# chunks: vector fallback for retriever.py.
resource "google_firestore_index" "chunks_vector" {
  project     = var.project_id
  database    = google_firestore_database.main.name
  collection  = "chunks"
  query_scope = "COLLECTION"

  fields {
    field_path = "tenant_id"
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

# CURRENT chunks only, for the ledger-aware fallback path.
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

# Filter combinations used by the Firestore vector fallback. These already cover
# individual and combined doc_type + kind filters, with and without CURRENT.
locals {
  chunks_filter_indexes = {
    doc_type              = ["tenant_id", "doc_type"]
    current_doc_type      = ["tenant_id", "current", "doc_type"]
    kind                  = ["tenant_id", "kind"]
    current_kind          = ["tenant_id", "current", "kind"]
    doc_type_kind         = ["tenant_id", "doc_type", "kind"]
    current_doc_type_kind = ["tenant_id", "current", "doc_type", "kind"]
  }
}

resource "google_firestore_index" "chunks_filter_vector" {
  for_each    = local.chunks_filter_indexes
  project     = var.project_id
  database    = google_firestore_database.main.name
  collection  = "chunks"
  query_scope = "COLLECTION"

  dynamic "fields" {
    for_each = each.value
    content {
      field_path = fields.value
      order      = "ASCENDING"
    }
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

# FirestoreGraph.expand() is an undirected walk. Each hop performs both of these
# compound queries:
#   graph_edges: tenant_id == tenant AND node_id IN (...)
#   graph_edges: tenant_id == tenant AND dst_id  IN (...)
# and then hydrates the reached nodes with:
#   graph_nodes: tenant_id == tenant AND node_id IN (...)
# Explicit indexes make a clean project reproducible instead of relying on a
# console-generated FailedPrecondition link during the demo.
locals {
  graph_filter_indexes = {
    edges_from = {
      collection = "graph_edges"
      fields     = ["tenant_id", "node_id"]
    }
    edges_to = {
      collection = "graph_edges"
      fields     = ["tenant_id", "dst_id"]
    }
    nodes_by_id = {
      collection = "graph_nodes"
      fields     = ["tenant_id", "node_id"]
    }
  }
}

resource "google_firestore_index" "graph_filter" {
  for_each    = local.graph_filter_indexes
  project     = var.project_id
  database    = google_firestore_database.main.name
  collection  = each.value.collection
  query_scope = "COLLECTION"

  dynamic "fields" {
    for_each = each.value.fields
    content {
      field_path = fields.value
      order      = "ASCENDING"
    }
  }

  fields {
    field_path = "__name__"
    order      = "ASCENDING"
  }
}

# answer_cache semantic vector index.
resource "google_firestore_index" "answer_cache_vector" {
  project     = var.project_id
  database    = google_firestore_database.main.name
  collection  = "answer_cache"
  query_scope = "COLLECTION"

  fields {
    field_path = "tenant_id"
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

# TTL for superseded/staged chunk rows.
resource "google_firestore_field" "chunks_expire_at" {
  project    = var.project_id
  database   = google_firestore_database.main.name
  collection = "chunks"
  field      = "expire_at"

  ttl_config {}
}

# TTL for semantic-cache entries.
resource "google_firestore_field" "answer_cache_expire_at" {
  project    = var.project_id
  database   = google_firestore_database.main.name
  collection = "answer_cache"
  field      = "expire_at"

  ttl_config {}
}

# Roster reverse lookup: COLLECTION_GROUP query on members.email.
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
