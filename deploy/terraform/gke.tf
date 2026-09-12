# GKE Autopilot for the 11.5 comparison. UNOWNED: lesson 11.5 teaches it.
#
# Autopilot because the alternative is teaching node pools, which is a Kubernetes
# lesson wearing an LLM costume. Here the GPU is requested per POD and Google
# provisions the node - which is the honest contrast with Cloud Run.
resource "google_container_cluster" "autopilot" {
  # The full profile, or the lean lane's one-hour comparison (make gke-up passes -var gke_cluster=true - decision D5
  # of the Module 11 plan). On the lane's VPC, not the default one its README used to confess to.
  count            = (local.full || var.gke_cluster) ? 1 : 0
  name             = "documind-autopilot"
  location         = var.region
  enable_autopilot = true
  network          = google_compute_network.vpc.id
  subnetwork       = google_compute_subnetwork.subnet.id

  # Autopilot manages the node pool; this block only exists to stop terraform
  # recreating the cluster on every plan.
  ip_allocation_policy {}

  deletion_protection = false      # a demo cluster, torn down after each session
}

output "gke_cluster" { value = one(google_container_cluster.autopilot[*].name) }
