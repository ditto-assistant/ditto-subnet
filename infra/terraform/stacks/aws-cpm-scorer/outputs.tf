output "cpm_scorer_instance_id" {
  description = "EC2 instance ID (the SSM target). Empty when disabled."
  value       = var.enable_cpm_scorer ? module.cpm_scorer[0].id : ""
}

output "cpm_scorer_private_ip" {
  description = "Private VPC address. Empty when disabled."
  value       = var.enable_cpm_scorer ? module.cpm_scorer[0].internal_ip : ""
}

output "cpm_scorer_role_name" {
  description = "Instance role read from the bootstrap-created profile. Empty when disabled."
  value       = var.enable_cpm_scorer ? data.aws_iam_instance_profile.scorer[0].role_name : ""
}

output "cpm_scorer_aws_account_id" {
  description = "Account that owns the scorer; a later GCP federation must name it."
  value       = data.aws_caller_identity.current.account_id
}

output "cpm_scorer_ssm_transfer_bucket" {
  description = "Bucket the Ansible aws_ssm connection stages files in. Empty when disabled."
  value       = var.enable_cpm_scorer ? aws_s3_bucket.ssm_transfer[0].bucket : ""
}
