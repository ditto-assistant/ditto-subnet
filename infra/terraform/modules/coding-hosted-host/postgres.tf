variable "postgres_peer" {
  description = "Optional reviewed private Platform PostgreSQL path. Null preserves complete network separation; no database credential or worker activation is granted."
  type = object({
    network_self_link = string
    private_ip        = string
    target_tag        = string
  })
  default = null

  validation {
    condition = var.postgres_peer == null ? true : (
      var.enabled &&
      var.postgres_peer.network_self_link == "https://www.googleapis.com/compute/v1/projects/${var.project}/global/networks/ditto-platform-net" &&
      can(cidrnetmask("${var.postgres_peer.private_ip}/32")) &&
      try(cidrhost("${var.postgres_peer.private_ip}/24", 0) == "10.30.0.0", false) &&
      can(regex("^[a-z][a-z0-9-]{0,62}$", var.postgres_peer.target_tag))
    )
    error_message = "Private PostgreSQL access requires the enabled native host, its owning project's Platform VPC, one private Platform-subnet IPv4 address and a valid PostgreSQL target tag."
  }
}

locals {
  postgres_enabled = var.enabled && var.postgres_peer != null
}

# VPC peering exchanges private subnet routes, not firewall permissions. This
# /32 TCP exception is the only new host egress permission; private/metadata
# denial and the non-PostgreSQL port deny remain unchanged.
resource "google_compute_firewall" "postgres_egress" {
  count              = local.postgres_enabled ? 1 : 0
  project            = var.project
  name               = "${local.name}-postgres-egress"
  network            = google_compute_network.host[0].id
  direction          = "EGRESS"
  priority           = 800
  target_tags        = [local.name]
  destination_ranges = ["${var.postgres_peer.private_ip}/32"]
  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}

# Cross-VPC source tags/service accounts do not carry firewall authority.
# Bind the actual single VM address, never the whole Coding subnet.
resource "google_compute_firewall" "postgres_ingress" {
  count         = local.postgres_enabled ? 1 : 0
  project       = var.project
  name          = "${local.name}-postgres-ingress"
  network       = var.postgres_peer.network_self_link
  direction     = "INGRESS"
  priority      = 800
  source_ranges = ["${module.host[0].internal_ip}/32"]
  target_tags   = [var.postgres_peer.target_tag]
  allow {
    protocol = "tcp"
    ports    = ["5432"]
  }
}

resource "google_compute_network_peering" "host_to_postgres" {
  count                               = local.postgres_enabled ? 1 : 0
  name                                = "coding-hosted-to-platform-postgres"
  network                             = google_compute_network.host[0].self_link
  peer_network                        = var.postgres_peer.network_self_link
  import_custom_routes                = false
  export_custom_routes                = false
  import_subnet_routes_with_public_ip = false
  export_subnet_routes_with_public_ip = false
  stack_type                          = "IPV4_ONLY"
  depends_on = [
    google_compute_firewall.postgres_ingress,
    google_compute_firewall.postgres_egress,
  ]
}

resource "google_compute_network_peering" "postgres_to_host" {
  count                               = local.postgres_enabled ? 1 : 0
  name                                = "platform-postgres-to-coding-hosted"
  network                             = var.postgres_peer.network_self_link
  peer_network                        = google_compute_network.host[0].self_link
  import_custom_routes                = false
  export_custom_routes                = false
  import_subnet_routes_with_public_ip = false
  export_subnet_routes_with_public_ip = false
  stack_type                          = "IPV4_ONLY"
  depends_on                          = [google_compute_network_peering.host_to_postgres]
}

output "postgres_access" {
  description = "Exact network coordinates for separately approved guest firewall/HBA convergence. Not login, TLS, execution or canary readiness."
  value = local.postgres_enabled ? {
    client_ip            = module.host[0].internal_ip
    client_cidr          = "${module.host[0].internal_ip}/32"
    postgres_private_ip  = var.postgres_peer.private_ip
    port                 = 5432
    database_login_ready = false
    shadow_only          = true
    weight_eligible      = false
  } : null
}
