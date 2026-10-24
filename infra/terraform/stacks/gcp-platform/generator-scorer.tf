###############################################################################
# DittoBench generator competition scorer.
#
# challenge-scorer (services/dittobench-api/cmd/challenge-scorer) polls
# Platform for generator qualification and round work, builds each miner's Go
# generator in a networkless, read-only Docker sandbox, and posts the verdict.
# It talks to no model. It runs on its own private VM because it executes
# untrusted miner code: the Platform, screener, and validator hosts never build
# a generator, and this VM's identity can read nothing but its own bearer.
###############################################################################

variable "enable_generator_scorer" {
  description = "Create the private generator-competition scorer VM and its identity. The Platform bearer secret exists regardless."
  type        = bool
  default     = false
}

variable "generator_scorer_gce_deletion_protection" {
  description = "GCE deletion protection for ditto-generator-scorer-prod. Set false only in the reviewed plan that precedes its retirement."
  type        = bool
  default     = true
}

locals {
  generator_scorer_count = var.enable_generator_scorer ? 1 : 0
}

# The value is populated out of band (a random 64-hex bearer). Platform reads it
# as DITTO_GENERATOR_SCORER_TOKEN; the scorer presents it on every request.
# The secret and Platform's read grant do not follow enable_generator_scorer:
# Platform's host_vars name the secret whether or not the VM exists, and the
# destroy guard must not block disabling the scorer.
resource "google_secret_manager_secret" "generator_scorer_token" {
  project   = var.project
  secret_id = "generator-scorer-token-prod"
  replication {
    auto {}
  }
  lifecycle {
    prevent_destroy = true
  }
}

resource "google_service_account" "generator_scorer" {
  count        = local.generator_scorer_count
  project      = var.project
  account_id   = "ditto-generator-scorer"
  display_name = "Ditto Generator Competition Scorer"
}

resource "google_secret_manager_secret_iam_member" "generator_scorer_token_scorer_access" {
  count     = local.generator_scorer_count
  project   = var.project
  secret_id = google_secret_manager_secret.generator_scorer_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.generator_scorer[0].email}"
}

resource "google_secret_manager_secret_iam_member" "generator_scorer_token_platform_access" {
  project   = var.project
  secret_id = google_secret_manager_secret.generator_scorer_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${local.platform_api_sa_email}"
}

# e2-standard-4: each sandboxed build is capped at 2 vCPU / 2 GiB, leaving room
# for the generator run, Docker, and the scorer itself.
module "generator_scorer_vm" {
  source   = "../../modules/compute/gcp"
  count    = local.generator_scorer_count
  project  = var.project
  name     = "ditto-generator-scorer-prod"
  size     = "app-standard"
  image    = "debian-13"
  location = var.zone

  subnetwork       = module.network.subnetwork_id
  network_tags     = [module.network.ssh_target_tag]
  assign_public_ip = false
  boot_disk_gb     = 50

  service_account_email = google_service_account.generator_scorer[0].email
  # Flip to false only in the reviewed plan that precedes retiring this VM
  # (see services/dittobench-api/docs/challenge-scorer.md, AWS cutover).
  deletion_protection = var.generator_scorer_gce_deletion_protection
  labels = {
    env     = "prod"
    role    = "generator-scorer"
    managed = "terraform"
  }
}

output "generator_scorer_vm_name" {
  description = "Private generator-scorer VM name (empty when disabled)."
  value       = var.enable_generator_scorer ? module.generator_scorer_vm[0].hostname : ""
}

output "generator_scorer_sa_email" {
  description = "Identity that can read only the generator-scorer bearer."
  value       = var.enable_generator_scorer ? google_service_account.generator_scorer[0].email : ""
}

###############################################################################
# AWS-hosted scorer identity (infra/terraform/stacks/aws-generator-scorer).
#
# An EC2 instance has no Google ambient identity. This pool trusts one AWS
# account and accepts only sessions of one exact instance role; that principal
# set can read the scorer bearer and nothing else. There is no service account
# to impersonate and no key: the host signs an STS GetCallerIdentity request
# with its instance-role credentials and exchanges it for a federated token.
# Inert until generator_scorer_aws_account_id is set.
###############################################################################

variable "generator_scorer_aws_account_id" {
  description = "AWS account whose generator-scorer instance role may read generator-scorer-token-prod. Empty disables the federation."
  type        = string
  default     = ""

  validation {
    condition     = var.generator_scorer_aws_account_id == "" || can(regex("^[0-9]{12}$", var.generator_scorer_aws_account_id))
    error_message = "generator_scorer_aws_account_id must be empty or a 12-digit AWS account ID."
  }
}

variable "generator_scorer_aws_role_name" {
  description = "Exact IAM role name of the AWS scorer instance profile (aws-generator-scorer output generator_scorer_role_name)."
  type        = string
  default     = "ditto-generator-scorer"

  validation {
    condition     = can(regex("^[A-Za-z0-9+=,.@_-]{1,64}$", var.generator_scorer_aws_role_name))
    error_message = "generator_scorer_aws_role_name must be a bare IAM role name."
  }
}

locals {
  generator_scorer_aws_count = var.generator_scorer_aws_account_id == "" ? 0 : 1
}

resource "google_iam_workload_identity_pool" "generator_scorer_aws" {
  count                     = local.generator_scorer_aws_count
  project                   = var.project
  workload_identity_pool_id = "generator-scorer-aws"
  display_name              = "Generator scorer on AWS"
  description               = "The AWS-hosted generator scorer's instance role; reads only its Platform bearer."

  depends_on = [google_project_iam_member.screener_fleet_x509_pool_admin]
}

resource "google_iam_workload_identity_pool_provider" "generator_scorer_aws" {
  count                              = local.generator_scorer_aws_count
  project                            = var.project
  workload_identity_pool_id          = google_iam_workload_identity_pool.generator_scorer_aws[0].workload_identity_pool_id
  workload_identity_pool_provider_id = "aws-ec2"
  display_name                       = "AWS ${var.generator_scorer_aws_account_id}"
  description                        = "Sessions of the exact generator-scorer instance role."
  attribute_mapping = {
    "google.subject"     = "assertion.arn"
    "attribute.aws_role" = "assertion.arn.extract('assumed-role/{role}/')"
  }
  attribute_condition = "assertion.arn.startsWith('arn:aws:sts::${var.generator_scorer_aws_account_id}:assumed-role/${var.generator_scorer_aws_role_name}/')"

  aws {
    account_id = var.generator_scorer_aws_account_id
  }
}

resource "google_secret_manager_secret_iam_member" "generator_scorer_token_aws_access" {
  count     = local.generator_scorer_aws_count
  project   = var.project
  secret_id = google_secret_manager_secret.generator_scorer_token.secret_id
  role      = "roles/secretmanager.secretAccessor"
  member    = "principalSet://iam.googleapis.com/projects/${data.google_project.this.number}/locations/global/workloadIdentityPools/${google_iam_workload_identity_pool.generator_scorer_aws[0].workload_identity_pool_id}/attribute.aws_role/${var.generator_scorer_aws_role_name}"

  depends_on = [google_iam_workload_identity_pool_provider.generator_scorer_aws]
}

output "generator_scorer_aws_provider" {
  description = "Provider resource name; the AWS scorer's external-account audience is //iam.googleapis.com/<this>. Empty when disabled."
  value       = local.generator_scorer_aws_count == 1 ? google_iam_workload_identity_pool_provider.generator_scorer_aws[0].name : ""
}
