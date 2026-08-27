locals {
  # Logical → native GCE machine type. Add new logical sizes by extending this
  # map AND the matching map in every other modules/compute/<provider>/. The
  # provider-portability contract lives in modules/compute/interface.md.
  machine_type_map = {
    # Contract-parity sizes (mirror the Hetzner dittobox classes).
    "prod-64gb"    = "e2-standard-16" # 16 vCPU / 64 GB
    "staging-32gb" = "e2-standard-8"  # 8 vCPU / 32 GB

    # GCP migration: right-sized Postgres+worker VMs (see docs / migration plan).
    "db-prod"    = "e2-standard-4" # 4 vCPU / 16 GB — prod is tiny + near-idle (2026-05-27: ~4% CPU, prod DB ~1.9 GB; whole working set caches in 16 GB)
    "db-staging" = "e2-standard-2" # 2 vCPU / 8 GB

    # Small app host: ditto-platform (SN118 API under pm2/uv + a Pylon Docker
    # sidecar). FastAPI + Pylon are light; e2-medium (2 shared vCPU / 4 GB) is ample.
    "app-small" = "e2-medium" # 2 vCPU (shared) / 4 GB

    # Capacity reconciler: one small, private control-plane VM. It does not
    # execute submissions; it only talks to Platform, Targon, and the GCE MIG.
    "controller-small" = "e2-small" # 2 shared vCPU / 2 GB

    # Standard app host: the prod SN118 API under miner upload storms. Every
    # upload computes fingerprints/embedding input in-process (GIL-bound), which
    # pegged the shared-core e2-medium on 2026-07-16; dedicated cores + headroom
    # keep the API responsive while uploads crunch.
    "app-standard" = "e2-standard-4" # 4 vCPU / 16 GB

    # SN118 validator host: co-located dittobench-api that compiles untrusted
    # miner Rust crates in Docker + runs a harness container + the worker. Cold
    # cargo builds are RAM/CPU-hungry, so 4 vCPU / 16 GB (not app-small).
    "validator" = "e2-standard-4" # 4 vCPU / 16 GB

    # Production validator: eight advertised slots with enough CPU/RAM for
    # concurrent full runs. Disk is configured separately by the caller.
    "validator-prod" = "e2-standard-8" # 8 vCPU / 32 GB

    # Temporary SN118 screener class for policy-wide rescreen backlogs. The
    # steady-state screener can return to `validator` after horizontal worker
    # scaling is implemented.
    "screener-burst" = "e2-standard-8" # 8 vCPU / 32 GB

    # Same burst class on the N2D (AMD EPYC) family: what ditto-screener-prod
    # was actually recreated as on 2026-07-14 (ditto-screener#12 moved it to
    # us-central1-c at the same time). Faster cargo builds than e2 at a small
    # price premium; kept as its own logical size so e2 consumers don't move.
    "screener-burst-n2d" = "n2d-standard-8" # 8 vCPU / 32 GB
  }

  # Logical → native GCE image (project/family form resolves the newest image).
  image_map = {
    "debian-13" = "projects/debian-cloud/global/images/family/debian-13"
    "debian-12" = "projects/debian-cloud/global/images/family/debian-12"
    "ubuntu-24" = "projects/ubuntu-os-cloud/global/images/family/ubuntu-2404-lts-amd64"
  }

  machine_type = lookup(local.machine_type_map, var.size, null)
  image        = lookup(local.image_map, var.image, null)
}

# Hard-fail at plan time if a caller passes an unsupported logical size/image.
check "size_supported" {
  assert {
    condition     = local.machine_type != null
    error_message = "Unsupported logical size '${var.size}'. Add it to modules/compute/gcp/locals.tf."
  }
}

check "image_supported" {
  assert {
    condition     = local.image != null
    error_message = "Unsupported logical image '${var.image}'. Add it to modules/compute/gcp/locals.tf."
  }
}
