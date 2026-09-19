# Module 4's seven application services run on Cloud Run. GKE is for the
# optional Module 11.5 self-hosted vLLM/GPU comparison.
# Default Standard boot disks do not consume regional SSD_TOTAL_GB quota.
variable "gke_autopilot" {
  type        = bool
  default     = false
  description = "Use Autopilot for the optional GPU lesson. Changing modes REPLACES the cluster; Autopilot requires sufficient SSD/GPU quota."
}

variable "gke_node_zone" {
  type        = string
  default     = null
  description = "Single node zone for the Standard lab; null selects REGION-a (asia-south1-a for Mumbai)."
  validation {
    condition     = var.gke_node_zone == null ? true : startswith(var.gke_node_zone, "${var.region}-")
    error_message = "gke_node_zone must be in the configured region."
  }
}

locals {
  gke_lab_zone = coalesce(var.gke_node_zone, "${var.region}-a")
}

resource "google_service_account" "gke_nodes" {
  count        = var.gke_autopilot ? 0 : 1
  account_id   = "documind-gke-nodes"
  display_name = "DocuMind Standard GKE nodes"
}

resource "google_project_iam_member" "gke_nodes" {
  count   = var.gke_autopilot ? 0 : 1
  project = var.project_id
  role    = "roles/container.defaultNodeServiceAccount"
  member  = "serviceAccount:${google_service_account.gke_nodes[0].email}"
}

resource "google_artifact_registry_repository_iam_member" "gke_nodes_reader" {
  count      = var.gke_autopilot ? 0 : 1
  project    = var.project_id
  location   = google_artifact_registry_repository.docker.location
  repository = google_artifact_registry_repository.docker.repository_id
  role       = "roles/artifactregistry.reader"
  member     = "serviceAccount:${google_service_account.gke_nodes[0].email}"
}

# Preserve the Terraform address/name for state, outputs and lesson commands.
# The legacy name does not imply Autopilot is enabled. Changing the mode is a
# reviewed REPLACEMENT, not an in-place disk resize; account for GKE workloads.
resource "google_container_cluster" "autopilot" {
  name       = "documind-autopilot"
  location   = var.region
  network    = google_compute_network.vpc.id
  subnetwork = google_compute_subnetwork.subnet.id

  # Null omits mode-specific arguments, avoiding Standard/Autopilot conflicts.
  enable_autopilot         = var.gke_autopilot ? true : null
  remove_default_node_pool = var.gke_autopilot ? null : true
  initial_node_count       = var.gke_autopilot ? null : 1
  node_locations          = var.gke_autopilot ? null : [local.gke_lab_zone]

  # The temporary bootstrap pool must ALSO use a standard disk. Configuring
  # only the final pool would still hit SSD quota during cluster creation.
  dynamic "node_config" {
    for_each = var.gke_autopilot ? [] : [1]
    content {
      machine_type    = "e2-standard-2"
      disk_type       = "pd-standard"
      disk_size_gb    = 30
      image_type      = "COS_CONTAINERD"
      service_account = google_service_account.gke_nodes[0].email
      oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
      workload_metadata_config {
        mode = "GKE_METADATA"
      }
    }
  }

  ip_allocation_policy {}
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  deletion_protection = false # lab teardown; review every replacement
  depends_on          = [
    google_project_iam_member.gke_nodes,
    google_artifact_registry_repository_iam_member.gke_nodes_reader,
  ]
}

resource "google_container_node_pool" "lab" {
  count          = var.gke_autopilot ? 0 : 1
  name           = "documind-lab"
  cluster        = google_container_cluster.autopilot.name
  location       = var.region
  node_locations = [local.gke_lab_zone]
  node_count     = 1 # ONE zone, not one node in each of three regional zones

  node_config {
    machine_type    = "e2-standard-2" # 2 vCPU, 8 GB RAM; no GPU
    disk_type       = "pd-standard"
    disk_size_gb    = 30
    image_type      = "COS_CONTAINERD"
    service_account = google_service_account.gke_nodes[0].email
    oauth_scopes    = ["https://www.googleapis.com/auth/cloud-platform"]
    workload_metadata_config {
      mode = "GKE_METADATA"
    }
  }

  management {
    auto_repair  = true
    auto_upgrade = true
  }
  upgrade_settings {
    max_surge       = 0 # no extra upgrade node; this lab accepts interruption
    max_unavailable = 1
  }
}

output "gke_cluster" { value = google_container_cluster.autopilot.name }
output "gke_mode" { value = var.gke_autopilot ? "AUTOPILOT" : "STANDARD" }
