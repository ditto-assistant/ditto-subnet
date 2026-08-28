# Persistent data disk for Postgres. Kept as a separate resource (not an
# inline boot disk) so it survives instance replacement and can carry its own
# snapshot schedule. Created only when var.data_disk_gb > 0.
resource "google_compute_disk" "data" {
  count   = var.data_disk_gb > 0 ? 1 : 0
  project = var.project == "" ? null : var.project
  name    = "${var.name}-data"
  type    = var.data_disk_type
  zone    = var.location
  size    = var.data_disk_gb
  labels  = var.labels

  lifecycle {
    # Postgres data volume — never destroy implicitly.
    prevent_destroy = true
  }
}

resource "google_compute_instance" "this" {
  project      = var.project == "" ? null : var.project
  name         = var.name
  machine_type = local.machine_type
  zone         = var.location
  labels       = var.labels
  tags         = var.network_tags

  # A machine_type change requires a stop/start rather than failing the apply.
  allow_stopping_for_update = true

  boot_disk {
    initialize_params {
      image = local.image
      size  = var.boot_disk_gb
      type  = var.boot_disk_type
    }
  }

  dynamic "attached_disk" {
    for_each = var.data_disk_gb > 0 ? [1] : []
    content {
      source      = google_compute_disk.data[0].id
      device_name = "${var.name}-data"
    }
  }

  network_interface {
    network    = var.subnetwork == "" ? "default" : null
    subnetwork = var.subnetwork == "" ? null : var.subnetwork

    # Only attach an external IP when explicitly requested. The Postgres VM
    # stays private: egress via Cloud NAT, SSH via IAP.
    dynamic "access_config" {
      for_each = var.assign_public_ip ? [1] : []
      content {}
    }
  }

  metadata = merge(
    { enable-oslogin = var.enable_os_login ? "TRUE" : "FALSE" },
    var.user_data == null ? {} : { user-data = var.user_data },
  )

  dynamic "service_account" {
    for_each = var.service_account_email == "" ? [] : [1]
    content {
      email  = var.service_account_email
      scopes = var.service_account_scopes
    }
  }

  shielded_instance_config {
    enable_secure_boot          = var.enable_secure_boot
    enable_vtpm                 = var.enable_vtpm
    enable_integrity_monitoring = var.enable_integrity_monitoring
  }

  deletion_protection = true

  lifecycle {
    # The PG VM carries irreplaceable state (Postgres data disk, River worker).
    # Recreating it is always a manual decision — mirrors the Hetzner module.
    prevent_destroy = true

    ignore_changes = [
      # Boot image is set at create time; in-place apt upgrades must not force
      # a recreate. Re-imaging is a manual decision.
      boot_disk[0].initialize_params[0].image,
      # OS Login / authorized keys are managed out of band, not by this metadata.
      metadata["ssh-keys"],
    ]
  }
}
