# GKE Autopilot for the 11.5 comparison. UNOWNED: lesson 11.5 teaches it.
#
# Autopilot because the alternative is teaching node pools, which is a Kubernetes
# lesson wearing an LLM costume. Here the GPU is requested per POD and Google
# provisions the node - which is the honest contrast with Cloud Run.
resource "google_container_cluster" "autopilot" {
  # Part of the one shape since 15 September 2026 (it was the full profile's, or the lean lane's one-hour comparison
  # behind -var gke_cluster=true): the cluster is Terraform's, make up creates it and make down removes it; make gke-up
  # / gke-down apply and delete the vLLM WORKLOAD only. An empty Autopilot cluster still bills its fee (Rs 6,000 a
  # month) - the price of the shape, not a leftover. On the lane's VPC, not the default one its README used to confess to.
  name             = "documind-autopilot"
  location         = var.region
  enable_autopilot = true
  network          = google_compute_network.vpc.id
  subnetwork       = google_compute_subnetwork.subnet.id

  # Autopilot manages the node pool; this block only exists to stop terraform
  # recreating the cluster on every plan.
  ip_allocation_policy {}

  deletion_protection = false # a demo cluster, torn down after each session
}

output "gke_cluster" { value = google_container_cluster.autopilot.name }
