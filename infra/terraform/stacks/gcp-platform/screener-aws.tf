###############################################################################
# On-demand AWS screener identity (infra/terraform/stacks/aws-screener).
#
# An EC2 instance has no Google ambient identity. This pool trusts one AWS
# account and accepts only sessions of one exact instance role,
# ditto-screener-node, which aws-bootstrap creates and no CI root can retrust.
# That principal set reads the same review/probe keys the Hetzner node's X.509
# identity reads (screener-fleet-x509.tf) plus its own single-use Platform
# registration grant. There is no service account to impersonate and no key:
# the host signs an STS GetCallerIdentity request with its instance-role
# credentials and exchanges it for a federated token.
# Inert until enable_screener_aws_identity is set.
###############################################################################

variable "enable_screener_aws_identity" {
  description = "Create the AWS WIF pool/provider for the on-demand EC2 screener, its read grants on the screener review keys, and the empty registration-grant secret."
  type        = bool
  default     = false
}

variable "screener_aws_account_id" {
  description = "AWS account whose ditto-screener-node instance role is federated (aws-screener output screener_aws_account_id)."
  type        = string
  default     = "339713011188"

  validation {
    condition     = can(regex("^[0-9]{12}$", var.screener_aws_account_id))
    error_message = "screener_aws_account_id must be a 12-digit AWS account ID."
  }
}

locals {
  screener_aws_count = var.enable_screener_aws_identity ? 1 : 0
  # aws-bootstrap/screener.tf creates exactly this role; the CI apply role is
  # denied UpdateAssumeRolePolicy on it.
  screener_aws_role_name = "ditto-screener-node"
  screener_aws_principal_set = (
    var.enable_screener_aws_identity
    ? "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.screener_aws[0].workload_identity_pool_id}/attribute.aws_role/${local.screener_aws_role_name}"
    : ""
  )
}

resource "google_iam_workload_identity_pool" "screener_aws" {
  count                     = local.screener_aws_count
  project                   = var.project
  workload_identity_pool_id = "screener-aws"
  display_name              = "Screener on AWS"
  description               = "The on-demand EC2 screener's instance role; reads only the screener review keys and its registration grant."

  lifecycle {
    # The shared pool-admin grant is retained whenever this AWS pool is enabled;
    # the retired Hetzner X.509 identity is not a prerequisite.
    precondition {
      condition     = var.enable_screener_fleet
      error_message = "enable_screener_aws_identity requires enable_screener_fleet (the review-key secrets)."
    }
  }

  depends_on = [google_project_iam_member.screener_fleet_x509_pool_admin]
}

resource "google_iam_workload_identity_pool_provider" "screener_aws" {
  count                              = local.screener_aws_count
  project                            = var.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.screener_aws[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "aws-ec2"
  display_name                       = "AWS ${var.screener_aws_account_id}"
  description                        = "Sessions of the exact ditto-screener-node instance role."
  attribute_mapping = {
    "google.subject"     = "assertion.arn"
    "attribute.aws_role" = "assertion.arn.extract('assumed-role/{role}/')"
  }
  attribute_condition = "assertion.arn.startsWith('arn:aws:sts::${var.screener_aws_account_id}:assumed-role/${local.screener_aws_role_name}/')"

  aws {
    account_id = var.screener_aws_account_id
  }
}

# Terraform owns the container only. The operator adds the single-use Platform
# registration grant as its one version; Terraform never writes a version.
resource "google_secret_manager_secret" "screener_aws_registration_token" {
  count     = local.screener_aws_count
  project   = var.project
  secret_id = "screener-aws-registration-token"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_iam_member" "screener_aws_registration_token" {
  count     = local.screener_aws_count
  project   = var.project
  secret_id = google_secret_manager_secret.screener_aws_registration_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = local.screener_aws_principal_set

  depends_on = [google_iam_workload_identity_pool_provider.screener_aws]
}

# The same three review/probe secrets the Hetzner node's X.509 identity reads;
# the node's secret agent picks one by screener_fleet_source_review_secret_id.
resource "google_secret_manager_secret_iam_member" "screener_aws_source_review" {
  count     = local.screener_aws_count
  project   = var.project
  secret_id = "validator-openrouter-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = local.screener_aws_principal_set

  depends_on = [google_secret_manager_secret.validator_openrouter_key, google_iam_workload_identity_pool_provider.screener_aws]
}

resource "google_secret_manager_secret_iam_member" "screener_aws_ditto_inference_review" {
  count     = local.screener_aws_count
  project   = var.project
  secret_id = "screener-review-ditto-inference-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = local.screener_aws_principal_set

  depends_on = [google_secret_manager_secret.screener_review_ditto_inference_key, google_iam_workload_identity_pool_provider.screener_aws]
}

resource "google_secret_manager_secret_iam_member" "screener_aws_probe_review" {
  count     = local.screener_aws_count
  project   = var.project
  secret_id = "screener-probe-review-router-key"
  role      = "roles/secretmanager.secretAccessor"
  member    = local.screener_aws_principal_set

  depends_on = [google_iam_workload_identity_pool_provider.screener_aws]
}

output "screener_aws_wif_provider" {
  description = "Full provider resource name (aws-screener screener_fleet_aws_wif_provider); the host's external-account audience is //iam.googleapis.com/<this>. Empty when disabled."
  value       = var.enable_screener_aws_identity ? google_iam_workload_identity_pool_provider.screener_aws[0].name : ""
}

output "screener_capacity_controller_unique_id" {
  description = "Numeric unique ID of the capacity controller service account: the azp/sub the AWS wake role pins (aws-bootstrap screener_capacity_controller_unique_id). Empty when the controller is disabled."
  value       = var.enable_screener_capacity_controller ? google_service_account.screener_capacity_controller[0].unique_id : ""
}
