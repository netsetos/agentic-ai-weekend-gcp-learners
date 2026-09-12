resource "google_compute_network" "vpc" {
  name                    = "documind-vpc"
  auto_create_subnetworks = false
}
resource "google_compute_subnetwork" "subnet" {
  name          = "documind-subnet"
  network       = google_compute_network.vpc.id
  region        = var.region
  ip_cidr_range = "10.20.0.0/20"
  private_ip_google_access = true
}
resource "google_vpc_access_connector" "conn" {
  name          = "documind-vpc"
  region        = var.region
  network       = google_compute_network.vpc.name
  ip_cidr_range = "10.8.0.0/28"
  min_instances = 2
  max_instances = 6
}
