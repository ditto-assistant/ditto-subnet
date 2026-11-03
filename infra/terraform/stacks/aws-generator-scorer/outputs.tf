output "generator_scorer_instance_id" {
  description = "EC2 instance ID (the SSM target). Empty when disabled."
  value       = var.enable_generator_scorer ? module.generator_scorer[0].id : ""
}

output "generator_scorer_private_ip" {
  description = "Private VPC address. Empty when disabled."
  value       = var.enable_generator_scorer ? module.generator_scorer[0].internal_ip : ""
}

output "generator_scorer_role_name" {
  description = "Instance role federated into GCP. gcp-platform's generator_scorer_aws_role_name must equal it."
  value       = var.enable_generator_scorer ? aws_iam_role.scorer[0].name : ""
}

output "generator_scorer_aws_account_id" {
  description = "gcp-platform's generator_scorer_aws_account_id must equal it."
  value       = data.aws_caller_identity.current.account_id
}

output "generator_scorer_ssm_transfer_bucket" {
  description = "Bucket the Ansible aws_ssm connection stages files in. Empty when disabled."
  value       = var.enable_generator_scorer ? aws_s3_bucket.ssm_transfer[0].bucket : ""
}
