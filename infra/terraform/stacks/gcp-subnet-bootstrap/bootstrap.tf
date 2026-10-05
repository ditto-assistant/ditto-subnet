# One-time owner-controlled billing/API/CI delegation. This state never
# creates secrets, secret versions, restore identities or compute resources.
# The public CI identities cannot bootstrap their own first project grant.
locals {
  project = "ditto-subnet"
  services = toset([
    "secretmanager.googleapis.com",
    "iam.googleapis.com",
    "iamcredentials.googleapis.com",
    "sts.googleapis.com",
    "cloudresourcemanager.googleapis.com",
  ])
  metadata_permissions = [
    "resourcemanager.projects.get",
    "serviceusage.services.get",
    "serviceusage.services.list",
    "serviceusage.services.use",
    "secretmanager.secrets.get",
    "secretmanager.secrets.list",
    "secretmanager.secrets.getIamPolicy",
    "iam.serviceAccounts.get",
    "iam.serviceAccounts.list",
    "iam.serviceAccounts.getIamPolicy",
    "iam.googleapis.com/workloadIdentityPools.get",
    "iam.googleapis.com/workloadIdentityPools.list",
    "iam.googleapis.com/workloadIdentityPoolProviders.get",
    "iam.googleapis.com/workloadIdentityPoolProviders.list",
    "iam.googleapis.com/workloadIdentityPools.getIamPolicy",
  ]
  mutation_permissions = [
    "secretmanager.secrets.create",
    "secretmanager.secrets.update",
    "secretmanager.secrets.setIamPolicy",
    "iam.serviceAccounts.create",
    "iam.serviceAccounts.update",
    "iam.serviceAccounts.setIamPolicy",
    "iam.googleapis.com/workloadIdentityPools.create",
    "iam.googleapis.com/workloadIdentityPools.update",
    "iam.googleapis.com/workloadIdentityPoolProviders.create",
    "iam.googleapis.com/workloadIdentityPoolProviders.update",
  ]
}
resource "google_billing_project_info" "subnet" {
  project         = local.project
  billing_account = "01279D-184F4C-3102C7"
  lifecycle { prevent_destroy = true }
}
resource "google_project_service" "recovery" {
  for_each           = local.services
  project            = local.project
  service            = each.value
  disable_on_destroy = false
  depends_on         = [google_billing_project_info.subnet]
}
resource "google_project_iam_custom_role" "plan" {
  project     = local.project
  role_id     = "subnetRecoveryTerraformPlan"
  title       = "Subnet recovery metadata plan"
  permissions = local.metadata_permissions
  depends_on  = [google_project_service.recovery]
}
resource "google_project_iam_custom_role" "apply" {
  project     = local.project
  role_id     = "subnetRecoveryTerraformApply"
  title       = "Subnet recovery container and identity management"
  permissions = concat(local.metadata_permissions, local.mutation_permissions)
  depends_on  = [google_project_service.recovery]
}
resource "google_project_iam_member" "plan" {
  project    = local.project
  role       = "projects/ditto-subnet/roles/subnetRecoveryTerraformPlan"
  member     = "serviceAccount:github-actions-terraform-plan@ditto-app-dev.iam.gserviceaccount.com"
  depends_on = [google_project_iam_custom_role.plan]
}
resource "google_project_iam_member" "apply" {
  project    = local.project
  role       = "projects/ditto-subnet/roles/subnetRecoveryTerraformApply"
  member     = "serviceAccount:github-actions-terraform-apply@ditto-app-dev.iam.gserviceaccount.com"
  depends_on = [google_project_iam_custom_role.apply]
}
