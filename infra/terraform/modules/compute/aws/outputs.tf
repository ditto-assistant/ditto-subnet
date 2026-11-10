output "id" {
  description = "EC2 instance ID. The SSM Session Manager and Ansible aws_ssm target."
  value       = aws_instance.this.id
}

output "ipv4" {
  description = "Public IPv4 address. Empty when assign_public_ip = false."
  value       = aws_instance.this.public_ip
}

output "ipv6" {
  description = "Public IPv6 address. Always empty for now (not provisioned)."
  value       = ""
}

output "internal_ip" {
  description = "Private VPC IPv4 address."
  value       = aws_instance.this.private_ip
}

output "hostname" {
  description = "Logical hostname (the Name tag)."
  value       = var.name
}
