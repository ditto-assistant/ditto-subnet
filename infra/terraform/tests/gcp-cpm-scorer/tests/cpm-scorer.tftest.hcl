# The CPM scorer's bearer container and AWS federation (gcp-platform's
# cpm-scorer.tf, copied in by ../run.sh), planned against a mocked provider: no
# GCP credentials and no API calls. Every run is a plan, so prevent_destroy on
# the secret never meets a destroy. That prod.auto.tfvars enables none of this
# is checked by ditto/tests/test_cpm_scorer_deploy.py.

mock_provider "google" {
  mock_data "google_project" {
    defaults = {
      number = "22790208601"
    }
  }
}

run "inert_by_default" {
  command = plan

  assert {
    condition     = length(google_secret_manager_secret.model_context_scorer_token) == 0
    error_message = "the bearer container must not exist until enable_model_context_scorer_secret"
  }
  assert {
    condition     = length(google_secret_manager_secret_iam_member.model_context_scorer_token_platform_access) == 0
    error_message = "Platform must not be granted a container that does not exist"
  }
  assert {
    condition     = length(google_iam_workload_identity_pool.cpm_scorer_aws) == 0 && length(google_iam_workload_identity_pool_provider.cpm_scorer_aws) == 0
    error_message = "the AWS federation must stay off until cpm_scorer_aws_account_id is set"
  }
  assert {
    condition     = length(google_secret_manager_secret_iam_member.model_context_scorer_token_aws_access) == 0
    error_message = "no AWS principal may read the bearer by default"
  }
  assert {
    condition     = output.model_context_scorer_secret_id == "" && output.cpm_scorer_aws_provider == ""
    error_message = "disabled outputs must be empty"
  }
}

run "container_only" {
  command = plan

  variables {
    enable_model_context_scorer_secret = true
  }

  assert {
    condition     = google_secret_manager_secret.model_context_scorer_token[0].secret_id == "model-context-scorer-token-prod"
    error_message = "the container must be model-context-scorer-token-prod"
  }
  assert {
    condition     = google_secret_manager_secret_iam_member.model_context_scorer_token_platform_access[0].role == "roles/secretmanager.secretAccessor"
    error_message = "Platform only reads the bearer"
  }
  assert {
    condition     = startswith(google_secret_manager_secret_iam_member.model_context_scorer_token_platform_access[0].member, "serviceAccount:")
    error_message = "the Platform grant names Platform's runtime service account"
  }
  assert {
    condition     = length(google_iam_workload_identity_pool.cpm_scorer_aws) == 0
    error_message = "creating the container must not federate anything"
  }
}

run "federation_reads_one_secret_for_one_role" {
  command = plan

  variables {
    enable_model_context_scorer_secret = true
    cpm_scorer_aws_account_id          = "339713011188"
  }

  assert {
    condition     = google_iam_workload_identity_pool.cpm_scorer_aws[0].workload_identity_pool_id == "cpm-scorer-aws"
    error_message = "the pool id must match the Ansible role's cpm_scorer_gcp_wif_pool"
  }
  assert {
    condition     = google_iam_workload_identity_pool_provider.cpm_scorer_aws[0].workload_identity_pool_provider_id == "aws-ec2"
    error_message = "the provider id must match the Ansible role's cpm_scorer_gcp_wif_provider"
  }
  assert {
    condition     = google_iam_workload_identity_pool_provider.cpm_scorer_aws[0].attribute_condition == "assertion.arn.startsWith('arn:aws:sts::339713011188:assumed-role/ditto-cpm-scorer/')"
    error_message = "only sessions of ditto-cpm-scorer in the one AWS account may federate"
  }
  assert {
    condition     = google_iam_workload_identity_pool_provider.cpm_scorer_aws[0].aws[0].account_id == "339713011188"
    error_message = "the provider trusts exactly one AWS account"
  }
  assert {
    condition     = google_secret_manager_secret_iam_member.model_context_scorer_token_aws_access[0].member == "principalSet://iam.googleapis.com/projects/22790208601/locations/global/workloadIdentityPools/cpm-scorer-aws/attribute.aws_role/ditto-cpm-scorer"
    error_message = "the grant must name only the ditto-cpm-scorer role's principal set"
  }
  assert {
    condition     = google_secret_manager_secret_iam_member.model_context_scorer_token_aws_access[0].role == "roles/secretmanager.secretAccessor"
    error_message = "the AWS principal only reads the bearer"
  }
  assert {
    condition     = length(google_project_iam_member.screener_fleet_x509_pool_admin) == 1
    error_message = "the CI apply identity needs the shared pool-admin grant to create the pool"
  }
}

run "federation_requires_the_container" {
  command = plan

  variables {
    cpm_scorer_aws_account_id = "339713011188"
  }

  expect_failures = [
    google_iam_workload_identity_pool.cpm_scorer_aws,
  ]
}

run "rejects_a_malformed_account" {
  command = plan

  variables {
    enable_model_context_scorer_secret = true
    cpm_scorer_aws_account_id          = "arn:aws:iam::339713011188:root"
  }

  expect_failures = [
    var.cpm_scorer_aws_account_id,
  ]
}
