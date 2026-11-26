output "screener_instance_id" {
  description = "EC2 instance ID: the SSM target and the controller's wake target (screener_fleet_provider_resource_id). Empty when disabled."
  value       = var.enable_aws_screener ? module.screener[0].id : ""
}

output "screener_node_id" {
  description = "Platform node id the host enrolls as. Empty when disabled."
  value       = var.enable_aws_screener ? var.screener_fleet_node_id : ""
}

output "screener_aws_account_id" {
  description = "gcp-platform's screener_aws_account_id must equal it."
  value       = data.aws_caller_identity.current.account_id
}

output "screener_vpc_id" {
  description = "Dedicated screener VPC. Empty when disabled."
  value       = var.enable_aws_screener ? aws_vpc.screener[0].id : ""
}
