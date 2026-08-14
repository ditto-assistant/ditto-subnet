###############################################################################
# Platform identity — created HERE, not in envs/gcp-shared.
#
# gcp-shared adopts the whole existing CI/identity inventory (frontend SA, AR
# repos, app buckets, …); applying it would touch a lot of non-platform infra.
# To keep `terraform apply` of THIS env self-contained, the two platform-only
# SAs live here. The `github` WIF pool already exists in the project (created
# out of band); we only REFERENCE it by constructed resource name to bind the
# deploy SA — we never manage the pool/provider.
###############################################################################

data "google_project" "this" {
  project_id = var.project
}

# Depot CI is a separate OIDC issuer from GitHub Actions. Keep a dedicated
# provider instead of widening or replacing the existing GitHub trust during
# the cutover. Depot does not enforce GitHub Environment protection rules, so
# the provider itself fails closed unless the token is for this exact Depot
# organization, repository, and main-branch ref.
resource "google_iam_workload_identity_pool_provider" "depot" {
  project                            = var.project
  workload_identity_pool_id          = var.wif_pool_id
  workload_identity_pool_provider_id = var.depot_wif_provider_id
  display_name                       = "Depot CI - ditto-subnet"

  attribute_mapping = {
    "google.subject"       = "assertion.repository_id"
    "attribute.org_id"     = "assertion.org_id"
    "attribute.repository" = "assertion.repository"
    "attribute.ref"        = "assertion.ref"
  }
  attribute_condition = "assertion.org_id == '${var.depot_org_id}' && assertion.repository == '${var.depot_deploy_repository}' && assertion.ref == '${var.depot_deploy_ref}'"

  oidc {
    issuer_uri = "https://identity.depot.dev"
  }
}

locals {
  depot_deploy_principal = "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/attribute.repository/${var.depot_deploy_repository}"
}

# Runtime SA the app VMs run as: reads its .env from Secret Manager and the
# agent buckets via the GCS HMAC key. (Net-new; confirmed absent in the project.)
resource "google_service_account" "ditto_platform" {
  project      = var.project
  account_id   = "ditto-platform"
  display_name = "Ditto Platform (SN118 API)"
}

# Dedicated runtime for the public platform API. It is the only steady-state
# identity allowed to read the platform-owned OpenRouter credential.
resource "google_service_account" "platform_api" {
  project      = var.project
  account_id   = "ditto-platform-api"
  display_name = "Ditto Platform API and inference proxy"
}

# Deploy SA the ditto-platform CI impersonates via WIF to IAP-SSH the app VMs.
# Tight IAP / OS-Login role set — the deploy runs scripts/update.sh over SSH
# (osAdminLogin grants the sudo it needs; viewer resolves the instance).
resource "google_service_account" "platform_deploy" {
  project      = var.project
  account_id   = "github-actions-platform-deploy"
  display_name = "GitHub Actions Deploy (ditto-platform)"
}

resource "google_project_iam_member" "platform_deploy_roles" {
  for_each = toset(var.platform_deploy_sa_roles)
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.platform_deploy.email}"
}

# `gcloud compute ssh --tunnel-through-iap` to a VM that has an attached
# service account requires actAs on that SA. The app VMs run as ditto_platform,
# so the deploy SA needs serviceAccountUser on it (scoped to that one SA, not
# project-wide).
resource "google_service_account_iam_member" "platform_deploy_actas_runtime" {
  service_account_id = google_service_account.ditto_platform.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.platform_deploy.email}"
}

resource "google_service_account_iam_member" "platform_deploy_actas_api_runtime" {
  service_account_id = google_service_account.platform_api.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.platform_deploy.email}"
}

# The monorepo screener deploy workflow IAP-SSHes to worker VMs that now carry
# the dedicated provider-secret-free identity.
resource "google_service_account_iam_member" "platform_deploy_actas_screener_worker" {
  service_account_id = google_service_account.screener_worker.name
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.platform_deploy.email}"
}

# Only the listed repos, and only their PROD-ENVIRONMENT jobs, may impersonate
# the deploy SA via the existing `github` pool.
#
# We bind the exact OIDC subject (`google.subject = assertion.sub`, mapped by the
# provider in envs/gcp-shared) rather than `attribute.repository`. GitHub sets
# `sub = repo:<owner>/<repo>:environment:<name>` only for a job that declares
# `environment: <name>`, and each repo's `prod` environment is protected so only
# `main` can deploy into it. A workflow added on another branch therefore cannot
# obtain a token GCP will accept — repository-wide federation (any branch could
# impersonate this sudo-capable SA) is closed.
#
# REQUIREMENT: each repo in platform_deploy_repos MUST have a GitHub
# `${var.platform_deploy_environment}` environment with deployment-branch
# restrictions (main only). ditto-subnet's scheduled screener deploy needs that
# environment to have NO required reviewers (branch restriction is what secures
# it), or the */5 reconvergence run would block on approval.
#
# The old service repos remain during the cutover window; ditto-subnet becomes
# the sole deploy principal after their workflows are disabled.
resource "google_service_account_iam_member" "platform_deploy_wif" {
  for_each           = toset(var.platform_deploy_repos)
  service_account_id = google_service_account.platform_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/repo:${each.value}:environment:${var.platform_deploy_environment}"
}

# Manual monorepo deploys may target the protected `dev` environment. Keep the
# principal exact rather than widening the prod binding to the whole repo.
resource "google_service_account_iam_member" "platform_deploy_wif_dev" {
  service_account_id = google_service_account.platform_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/repo:ditto-assistant/ditto-subnet:environment:dev"
}

resource "google_service_account_iam_member" "platform_deploy_depot_wif" {
  service_account_id = google_service_account.platform_deploy.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.depot_deploy_principal
}

# Datagen releases publish one immutable generate-service image and deploy it to
# only the ditto-datapipeline Cloud Run service. Keep that identity separate
# from the broad platform deploy identity and scope federation to main through
# the protected GitHub `prod` environment.
resource "google_service_account" "datagen_release" {
  project      = var.project
  account_id   = "github-actions-datagen-release"
  display_name = "GitHub Actions datagen image release"
}

resource "google_service_account_iam_member" "datagen_release_wif" {
  service_account_id = google_service_account.datagen_release.name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/repo:ditto-assistant/ditto-subnet:environment:${var.platform_deploy_environment}"
}

resource "google_service_account_iam_member" "datagen_release_depot_wif" {
  service_account_id = google_service_account.datagen_release.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.depot_deploy_principal
}

# The pre-for_each address held the ditto-platform binding; keep its state entry.
moved {
  from = google_service_account_iam_member.platform_deploy_wif
  to   = google_service_account_iam_member.platform_deploy_wif["ditto-assistant/ditto-platform"]
}

# --- Screener golden-image bake SA --------------------------------------------
# ditto-subnet's Bake workflow (workers/screener/packer/...) impersonates
# this SA via WIF to build the `ditto-screener-fleet` image. Kept separate from
# the sudo-capable deploy SA: baking only needs to spin a temporary VM and write
# an image, never to reach production hosts. No Secret Manager access — the bake
# uploads the checkout and bakes NO credential into the image. Off unless the
# fleet feature is being used.
resource "google_service_account" "screener_bake" {
  count        = local.screener_fleet_secrets_count
  project      = var.project
  account_id   = "github-actions-screener-bake"
  display_name = "GitHub Actions Screener Image Bake"
}

resource "google_project_iam_member" "screener_bake_roles" {
  for_each = local.screener_fleet_secrets_count == 1 ? toset(var.screener_bake_sa_roles) : toset([])
  project  = var.project
  role     = each.value
  member   = "serviceAccount:${google_service_account.screener_bake[0].email}"
}

# Packer's build VM runs as the project's default compute SA; the bake SA needs
# actAs on it to launch that VM. Scoped to that ONE SA, not project-wide
# serviceAccountUser. (The build VM itself makes no GCP API calls; the bake only
# apt/docker/uv-syncs an uploaded checkout.)
resource "google_service_account_iam_member" "screener_bake_actas_default_compute" {
  count              = local.screener_fleet_secrets_count
  service_account_id = "projects/${var.project}/serviceAccounts/${data.google_project.this.number}-compute@developer.gserviceaccount.com"
  role               = "roles/iam.serviceAccountUser"
  member             = "serviceAccount:${google_service_account.screener_bake[0].email}"
}

# Same env-scoped WIF pattern as the deploy SA: only ditto-subnet's prod
# environment jobs may impersonate it.
resource "google_service_account_iam_member" "screener_bake_wif" {
  count              = local.screener_fleet_secrets_count
  service_account_id = google_service_account.screener_bake[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = "principal://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${var.wif_pool_id}/subject/repo:ditto-assistant/ditto-subnet:environment:${var.platform_deploy_environment}"
}

resource "google_service_account_iam_member" "screener_bake_depot_wif" {
  count              = local.screener_fleet_secrets_count
  service_account_id = google_service_account.screener_bake[0].name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.depot_deploy_principal
}
