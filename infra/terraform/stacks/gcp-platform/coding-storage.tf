###############################################################################
# Default-off private coding S3-compatible authorities.
#
# Cloud Storage's XML API is the repository's existing S3-compatible provider.
# These buckets and identities remain absent until the reviewed production
# intent sets enable_coding_s3_authorities=true and a protected Terraform apply
# is approved. No validator, executor, candidate container, or model identity is
# granted storage or Secret Manager access here.
###############################################################################

locals {
  coding_storage_envs = var.enable_coding_s3_authorities ? local.app_envs : {}

  coding_storage_auditor_bindings = {
    for binding in setproduct(keys(local.coding_storage_envs), var.coding_storage_auditors) :
    "${binding[0]}:${binding[1]}" => {
      environment = binding[0]
      member      = binding[1]
    }
  }
}

# The runtime reader can fetch one server-derived private object but cannot
# list, create, update, or delete objects. Signed GET capabilities inherit this
# identity's narrow authority.
resource "google_project_iam_custom_role" "coding_private_input_reader" {
  count = var.enable_coding_s3_authorities ? 1 : 0

  project     = var.project
  role_id     = "dittoCodingPrivateInputReader"
  title       = "Ditto Coding Private Input Reader"
  description = "Get-only private coding input authority; no list, write, or delete"
  permissions = ["storage.objects.get"]
}

# Evidence finalization needs create-only upload authority plus exact-object
# HEAD/download verification. It deliberately lacks list, update, and delete.
resource "google_project_iam_custom_role" "coding_evidence_finalizer" {
  count = var.enable_coding_s3_authorities ? 1 : 0

  project     = var.project
  role_id     = "dittoCodingEvidenceFinalizer"
  title       = "Ditto Coding Evidence Finalizer"
  description = "Create and verify exact sealed coding evidence; no list, update, or delete"
  permissions = [
    "storage.objects.create",
    "storage.objects.get",
  ]
}

resource "google_storage_bucket" "coding_private_inputs" {
  for_each = local.coding_storage_envs

  project                     = var.project
  name                        = "${var.project}-coding-private-inputs-${each.key}"
  location                    = var.coding_storage_location
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  labels = {
    environment = each.key
    authority   = "coding-private-inputs"
    managed_by  = "terraform"
  }

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = var.coding_private_input_retention_seconds
    is_locked        = false
  }

  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

resource "google_storage_bucket" "coding_sealed_evidence" {
  for_each = local.coding_storage_envs

  project                     = var.project
  name                        = "${var.project}-coding-sealed-evidence-${each.key}"
  location                    = var.coding_storage_location
  storage_class               = "STANDARD"
  force_destroy               = false
  uniform_bucket_level_access = true
  public_access_prevention    = "enforced"

  labels = {
    environment = each.key
    authority   = "coding-sealed-evidence"
    managed_by  = "terraform"
  }

  versioning {
    enabled = true
  }

  retention_policy {
    retention_period = var.coding_sealed_evidence_retention_seconds
    is_locked        = false
  }

  lifecycle_rule {
    condition {
      age = 1
    }
    action {
      type = "AbortIncompleteMultipartUpload"
    }
  }

  lifecycle {
    prevent_destroy = true
  }
}

# Three non-interchangeable identities per environment. The curator can only
# create immutable private-input objects. Platform's future input reader can
# only get exact objects. The evidence finalizer can create and verify exact
# evidence, but cannot enumerate or remove it.
resource "google_service_account" "coding_private_input_curator" {
  for_each = local.coding_storage_envs

  project      = var.project
  account_id   = "ditto-coding-curator-${each.key}"
  display_name = "Ditto coding input curator (${each.key})"
  description  = "Create-only private coding input release identity"
}

resource "google_service_account" "coding_private_input_reader" {
  for_each = local.coding_storage_envs

  project      = var.project
  account_id   = "ditto-coding-input-${each.key}"
  display_name = "Ditto coding input reader (${each.key})"
  description  = "Get-only private coding input capability identity"
}

resource "google_service_account" "coding_evidence_finalizer" {
  for_each = local.coding_storage_envs

  project      = var.project
  account_id   = "ditto-coding-evidence-${each.key}"
  display_name = "Ditto coding evidence finalizer (${each.key})"
  description  = "Create and exact-object verify sealed coding evidence"
}

resource "google_storage_bucket_iam_member" "coding_private_input_curator" {
  for_each = local.coding_storage_envs

  bucket = google_storage_bucket.coding_private_inputs[each.key].name
  role   = "roles/storage.objectCreator"
  member = google_service_account.coding_private_input_curator[each.key].member
}

resource "google_storage_bucket_iam_member" "coding_private_input_reader" {
  for_each = local.coding_storage_envs

  bucket = google_storage_bucket.coding_private_inputs[each.key].name
  role   = google_project_iam_custom_role.coding_private_input_reader[0].name
  member = google_service_account.coding_private_input_reader[each.key].member
}

resource "google_storage_bucket_iam_member" "coding_evidence_finalizer" {
  for_each = local.coding_storage_envs

  bucket = google_storage_bucket.coding_sealed_evidence[each.key].name
  role   = google_project_iam_custom_role.coding_evidence_finalizer[0].name
  member = google_service_account.coding_evidence_finalizer[each.key].member
}

# Optional read-only audit principals receive exact-object get only on both
# authorities. Empty by default; no operator is silently privileged.
resource "google_storage_bucket_iam_member" "coding_private_input_auditor" {
  for_each = local.coding_storage_auditor_bindings

  bucket = google_storage_bucket.coding_private_inputs[each.value.environment].name
  role   = google_project_iam_custom_role.coding_private_input_reader[0].name
  member = each.value.member
}

resource "google_storage_bucket_iam_member" "coding_evidence_auditor" {
  for_each = local.coding_storage_auditor_bindings

  bucket = google_storage_bucket.coding_sealed_evidence[each.value.environment].name
  role   = google_project_iam_custom_role.coding_private_input_reader[0].name
  member = each.value.member
}

# HMAC credentials support the existing S3-compatible client surface. Secrets
# are retained in Secret Manager and Terraform state; this layer intentionally
# grants no runtime principal secretAccessor. A later integration PR must grant
# only the reader/finalizer pair to the Platform identity and must never expose
# the curator credential to Platform, validators, executors, or candidates.
resource "google_storage_hmac_key" "coding_private_input_curator" {
  for_each = local.coding_storage_envs

  project               = var.project
  service_account_email = google_service_account.coding_private_input_curator[each.key].email
}

resource "google_storage_hmac_key" "coding_private_input_reader" {
  for_each = local.coding_storage_envs

  project               = var.project
  service_account_email = google_service_account.coding_private_input_reader[each.key].email
}

resource "google_storage_hmac_key" "coding_evidence_finalizer" {
  for_each = local.coding_storage_envs

  project               = var.project
  service_account_email = google_service_account.coding_evidence_finalizer[each.key].email
}

resource "google_secret_manager_secret" "coding_private_input_curator_hmac" {
  for_each = local.coding_storage_envs

  project   = var.project
  secret_id = "coding-input-curator-${each.key}-hmac-secret"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_version" "coding_private_input_curator_hmac" {
  for_each = local.coding_storage_envs

  secret      = google_secret_manager_secret.coding_private_input_curator_hmac[each.key].id
  secret_data = google_storage_hmac_key.coding_private_input_curator[each.key].secret
}

resource "google_secret_manager_secret" "coding_private_input_reader_hmac" {
  for_each = local.coding_storage_envs

  project   = var.project
  secret_id = "coding-input-reader-${each.key}-hmac-secret"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_version" "coding_private_input_reader_hmac" {
  for_each = local.coding_storage_envs

  secret      = google_secret_manager_secret.coding_private_input_reader_hmac[each.key].id
  secret_data = google_storage_hmac_key.coding_private_input_reader[each.key].secret
}

resource "google_secret_manager_secret" "coding_evidence_finalizer_hmac" {
  for_each = local.coding_storage_envs

  project   = var.project
  secret_id = "coding-evidence-finalizer-${each.key}-hmac-secret"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_version" "coding_evidence_finalizer_hmac" {
  for_each = local.coding_storage_envs

  secret      = google_secret_manager_secret.coding_evidence_finalizer_hmac[each.key].id
  secret_data = google_storage_hmac_key.coding_evidence_finalizer[each.key].secret
}

# Cloud Storage Admin Activity logs are always present; this opt-in enables the
# otherwise-disabled Data Read and Data Write logs required to audit object use.
# The resource is intentionally tied to the same false-by-default authority
# switch so merging alone cannot change project-wide logging cost or coverage.
resource "google_project_iam_audit_config" "coding_storage" {
  count = var.enable_coding_s3_authorities ? 1 : 0

  project = var.project
  service = "storage.googleapis.com"

  audit_log_config {
    log_type = "DATA_READ"
  }

  audit_log_config {
    log_type = "DATA_WRITE"
  }
}

# Unlike the JSON API, Cloud Storage's XML/S3-compatible API accepts plaintext
# HTTP unless this organization-policy constraint is enforced. This policy is
# project-wide, false-by-default with the coding authorities, and must be
# reviewed against every existing XML client before the first protected apply.
resource "google_project_organization_policy" "coding_storage_secure_transport" {
  count = var.enable_coding_s3_authorities ? 1 : 0

  project    = var.project
  constraint = "storage.secureHttpTransport"

  boolean_policy {
    enforced = true
  }
}
