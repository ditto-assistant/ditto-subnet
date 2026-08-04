# Custom VPC — no auto subnets, regional routing. Isolates the dittobox stack
# from the project's legacy `default` network.
resource "google_compute_network" "this" {
  project                 = var.project == "" ? null : var.project
  name                    = var.network_name
  auto_create_subnetworks = false
  routing_mode            = "REGIONAL"
}

# Single regional subnet. Hosts the Postgres VM and supplies the IP range Cloud
# Run uses for Direct VPC egress. private_ip_google_access lets the no-public-IP
# VM reach Google APIs (Secret Manager, Artifact Registry, logging) without NAT.
resource "google_compute_subnetwork" "this" {
  project                  = var.project == "" ? null : var.project
  name                     = var.subnet_name
  region                   = var.region
  network                  = google_compute_network.this.id
  ip_cidr_range            = var.subnet_cidr
  private_ip_google_access = true
}

# Cloud NAT — outbound internet for the private VM (apt, external APIs that are
# not Google). Only egress; no inbound exposure.
resource "google_compute_router" "this" {
  count   = var.enable_nat ? 1 : 0
  project = var.project == "" ? null : var.project
  name    = "${var.network_name}-router"
  region  = var.region
  network = google_compute_network.this.id
}

resource "google_compute_router_nat" "this" {
  count                              = var.enable_nat ? 1 : 0
  project                            = var.project == "" ? null : var.project
  name                               = "${var.network_name}-nat"
  router                             = google_compute_router.this[0].name
  region                             = var.region
  nat_ip_allocate_option             = "AUTO_ONLY"
  source_subnetwork_ip_ranges_to_nat = "ALL_SUBNETWORKS_ALL_IP_RANGES"

  dynamic "log_config" {
    for_each = var.nat_logging ? [1] : []
    content {
      enable = true
      filter = "ERRORS_ONLY"
    }
  }
}

# SSH only via IAP TCP forwarding — no public SSH. Targets the ssh tag.
resource "google_compute_firewall" "iap_ssh" {
  project       = var.project == "" ? null : var.project
  name          = "${var.network_name}-allow-iap-ssh"
  network       = google_compute_network.this.id
  direction     = "INGRESS"
  source_ranges = [var.iap_source_range]
  target_tags   = [var.ssh_target_tag]

  allow {
    protocol = "tcp"
    ports    = ["22"]
  }
}

# Postgres + PgBouncer reachable only from inside the subnet — this is the path
# Cloud Run Direct VPC egress traffic takes to the DB. Targets the postgres tag.
resource "google_compute_firewall" "internal_db" {
  project       = var.project == "" ? null : var.project
  name          = "${var.network_name}-allow-internal-db"
  network       = google_compute_network.this.id
  direction     = "INGRESS"
  source_ranges = [var.subnet_cidr]
  target_tags   = [var.postgres_target_tag]

  allow {
    protocol = "tcp"
    ports    = var.internal_db_ports
  }
}
