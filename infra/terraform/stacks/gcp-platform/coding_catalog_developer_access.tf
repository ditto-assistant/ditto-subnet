###############################################################################
# Human access for local work on the private Coding catalog.
#
# The two existing Secret Manager containers hold the dedicated Hippius
# Object Read Only identity for ditto-subnet-coding-catalog. Keep this grant
# separate from debug_operators and from the curator's read-write credential.
###############################################################################

locals {
  coding_catalog_developer_secret_ids = toset([
    google_secret_manager_secret.coding_catalog_access_key.secret_id,
    google_secret_manager_secret.coding_catalog_secret_key.secret_id,
  ])

  coding_catalog_developer_secret_access = {
    for pair in setproduct(
      local.coding_catalog_developer_secret_ids,
      toset(var.coding_catalog_secret_users),
      ) : "${pair[0]} ${pair[1]}" => {
      secret_id = pair[0]
      member    = pair[1]
    }
  }
}

resource "google_secret_manager_secret_iam_member" "coding_catalog_developer_access" {
  for_each  = local.coding_catalog_developer_secret_access
  project   = var.project
  secret_id = each.value.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value.member
}
