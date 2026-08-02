# Golden image for the SN118 prod screener fleet.
#
# Bakes the slow, secret-free provisioning (base packages, Docker engine, the
# IMDS guard, uv, the deploy:ditto layout, a warm ditto-screener checkout with a
# synced venv) into the `ditto-screener-fleet` image family. A fleet MIG booted
# from this image runs scripts/bootstrap-screener.sh in normal mode; its
# idempotent guards skip everything already baked, so first boot goes straight
# to fetching secrets + the fast updater — cutting time-to-first-claim from
# ~5-10 min to ~1-2 min so autoscaling relieves the pet VM promptly in a burst.
#
# NO SECRET is baked: the checkout is seeded from an uploaded tarball (not a
# deploy-key clone), and the deploy key / mnemonic / API token are all fetched
# at runtime by the startup script + updater. The resulting image carries no
# credentials.
#
# Build (CI does this in .github/workflows/bake-image.yml):
#   tar czf /tmp/src.tgz .
#   packer init packer/
#   packer build -var src_tarball=/tmp/src.tgz packer/screener-fleet.pkr.hcl

packer {
  required_plugins {
    googlecompute = {
      source  = "github.com/hashicorp/googlecompute"
      version = ">= 1.1.0"
    }
  }
}

variable "project_id" {
  type    = string
  default = "ditto-app-dev"
}

variable "zone" {
  type    = string
  default = "us-central1-c"
}

variable "source_image_family" {
  type    = string
  default = "debian-13"
}

variable "source_image_project" {
  type    = string
  default = "debian-cloud"
}

variable "machine_type" {
  # Match the fleet's runtime class so the baked venv/toolchain are arch-correct.
  type    = string
  default = "n2d-standard-4"
}

variable "image_family" {
  type    = string
  default = "ditto-screener-fleet"
}

variable "src_tarball" {
  description = "Path to a .tgz of the ditto-screener checkout (including .git) to bake in."
  type        = string
}

variable "access_token" {
  description = "Optional pre-minted OAuth access token. Leave empty in CI (WIF provides ADC). Set for local runs where interactive ADC needs reauth: -var access_token=$(gcloud auth application-default print-access-token)."
  type        = string
  default     = ""
  sensitive   = true
}

variable "subnetwork" {
  description = "Subnetwork for the temporary build VM. Use the platform subnet so egress rides Cloud NAT with no external IP."
  type        = string
  default     = "ditto-platform-us-central1"
}

variable "ssh_tag" {
  description = "Network tag granting IAP-SSH to the build VM (the platform network's ssh target tag)."
  type        = string
  default     = "ssh"
}

source "googlecompute" "screener" {
  project_id              = var.project_id
  access_token            = var.access_token
  zone                    = var.zone
  source_image_family     = var.source_image_family
  source_image_project_id = [var.source_image_project]
  machine_type            = var.machine_type

  image_name        = "ditto-screener-fleet-{{timestamp}}"
  image_family      = var.image_family
  image_description = "SN118 prod screener fleet golden image: docker + uv + warm ditto-screener checkout/venv + IMDS guard. No baked secrets. See packer/screener-fleet.pkr.hcl."
  image_labels = {
    role    = "screener-fleet"
    managed = "packer"
  }

  # Private posture: no external IP, egress via Cloud NAT, SSH via IAP.
  subnetwork       = var.subnetwork
  omit_external_ip = true
  use_internal_ip  = true
  use_iap          = true
  tags             = [var.ssh_tag]

  ssh_username = "packer"
}

build {
  name    = "screener-fleet"
  sources = ["source.googlecompute.screener"]

  provisioner "file" {
    source      = var.src_tarball
    destination = "/tmp/ditto-screener-src.tgz"
  }

  provisioner "shell" {
    inline = [
      "set -euo pipefail",
      "sudo mkdir -p /tmp/ditto-screener-src",
      "sudo tar xzf /tmp/ditto-screener-src.tgz -C /tmp/ditto-screener-src",
      "sudo SCREENER_BAKE_ONLY=1 SCREENER_BAKE_SRC=/tmp/ditto-screener-src bash /tmp/ditto-screener-src/workers/screener/scripts/bootstrap-screener.sh",
      # Leave no build artifacts (and definitely no source tree with a live git
      # remote) in the image beyond the baked /opt/ditto checkout.
      "sudo rm -rf /tmp/ditto-screener-src /tmp/ditto-screener-src.tgz",
    ]
  }
}
