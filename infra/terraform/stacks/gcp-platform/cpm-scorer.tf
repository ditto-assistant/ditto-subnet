###############################################################################
# CPM ("model_context") scorer bearer and its AWS identity
# (docs/research/cpm/competition-v1.md §1, §5.4, §6.1).
#
# The scorer is a GPU host in AWS (infra/terraform/stacks/aws-cpm-scorer) that
# leases model_context runs from Platform with one bearer. Platform reads the
# bearer as DITTO_MODEL_CONTEXT_SCORER_TOKEN; the host reads it through Workload
# Identity Federation from its EC2 instance role, exactly as the AWS generator
# scorer reads its own (generator-scorer.tf). Both halves are off by default:
#
# - enable_model_context_scorer_secret creates the empty secret container
#   model-context-scorer-token-prod and lets Platform's runtime identity read
#   it. Its value is added out of band and never enters Terraform state. Once
#   created, the container is protected from destroy, so turning the flag back
#   off fails the plan instead of deleting the bearer.
# - cpm_scorer_aws_account_id creates a pool that trusts one AWS account and
#   accepts only sessions of one exact instance role (ditto-cpm-scorer, which
#   aws-bootstrap creates and no CI root can retrust). That principal set can
#   read this one secret and nothing else. There is no service account to
#   impersonate and no key.
###############################################################################

variable "enable_model_context_scorer_secret" {
  description = "Create the empty model-context-scorer-token-prod secret container and Platform's read grant. Values are added out of band."
  type        = bool
  default     = false
}

variable "cpm_scorer_aws_account_id" {
  description = "AWS account whose CPM scorer instance role may read model-context-scorer-token-prod. Empty disables the federation."
  type        = string
  default     = ""

  validation {
    condition     = var.cpm_scorer_aws_account_id == "" || can(regex("^[0-9]{12}$", var.cpm_scorer_aws_account_id))
    error_message = "cpm_scorer_aws_account_id must be empty or a 12-digit AWS account ID."
  }
}

variable "cpm_scorer_aws_role_name" {
  description = "Exact IAM role name of the CPM scorer's instance profile (aws-bootstrap output cpm_scorer_role_name)."
  type        = string
  default     = "ditto-cpm-scorer"

  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.cpm_scorer_aws_role_name))
    error_message = "cpm_scorer_aws_role_name must be a bare IAM role name."
  }
}

locals {
  model_context_scorer_secret_count = var.enable_model_context_scorer_secret ? 1 : 0
  cpm_scorer_aws_count              = var.cpm_scorer_aws_account_id == "" ? 0 : 1
}

resource "google_secret_manager_secret" "model_context_scorer_token" {
  count     = local.model_context_scorer_secret_count
  project   = var.project
  secret_id = "model-context-scorer-token-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_secret_manager_secret_iam_member" "model_context_scorer_token_platform_access" {
  count     = local.model_context_scorer_secret_count
  project   = var.project
  secret_id = google_secret_manager_secret.model_context_scorer_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

resource "google_iam_workload_identity_pool" "cpm_scorer_aws" {
  count                     = local.cpm_scorer_aws_count
  project                   = var.project
  workload_identity_pool_id = "cpm-scorer-aws"
  display_name              = "CPM scorer on AWS"
  description               = "The AWS-hosted CPM scorer's instance role; reads only its Platform bearer."

  lifecycle {
    precondition {
      condition     = var.enable_model_context_scorer_secret
      error_message = "cpm_scorer_aws_account_id requires enable_model_context_scorer_secret (the only secret this pool may read)."
    }
  }

  # Pool administration comes from the shared grant in screener-fleet-x509.tf,
  # which exists while any of the WIF pools is enabled.
  depends_on = [google_project_iam_member.screener_fleet_x509_pool_admin]
}

resource "google_iam_workload_identity_pool_provider" "cpm_scorer_aws" {
  count                              = local.cpm_scorer_aws_count
  project                            = var.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.cpm_scorer_aws[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "aws-ec2"
  display_name                       = "AWS ${var.cpm_scorer_aws_account_id}"
  description                        = "Sessions of the exact CPM scorer instance role."
  attribute_mapping = {
    "google.subject"     = "assertion.arn"
    "attribute.aws_role" = "assertion.arn.extract('assumed-role/{role}/')"
  }
  attribute_condition = "assertion.arn.startsWith('arn:aws:sts::${var.cpm_scorer_aws_account_id}:assumed-role/${var.cpm_scorer_aws_role_name}/')"

  aws {
    account_id = var.cpm_scorer_aws_account_id
  }
}

resource "google_secret_manager_secret_iam_member" "model_context_scorer_token_aws_access" {
  count     = local.cpm_scorer_aws_count
  project   = var.project
  secret_id = google_secret_manager_secret.model_context_scorer_token[0].secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.cpm_scorer_aws[0].workload_identity_pool_id}/attribute.aws_role/${var.cpm_scorer_aws_role_name}"

  depends_on = [google_iam_workload_identity_pool_provider.cpm_scorer_aws]
}

output "model_context_scorer_secret_id" {
  description = "Secret Manager container for DITTO_MODEL_CONTEXT_SCORER_TOKEN. Empty when disabled."
  value       = var.enable_model_context_scorer_secret ? google_secret_manager_secret.model_context_scorer_token[0].secret_id : ""
}

output "cpm_scorer_aws_provider" {
  description = "Provider resource name; the CPM scorer's external-account audience is //iam.googleapis.com/<this>. Empty when disabled."
  value       = local.cpm_scorer_aws_count == 1 ? google_iam_workload_identity_pool_provider.cpm_scorer_aws[0].name : ""
}
