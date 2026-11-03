# SN-138: retire the complete native host module after the protected
# deletion-protection update and verified cold snapshot restore. Keep the
# reusable module default-off; reintroduction requires a new reviewed plan.
# Null outputs document retirement; Terraform omits null values from state.
output "coding_hosted_host" {
  description = "Retired native-v2 host; recoverable cold snapshot is retained outside this Terraform root."
  value       = null
}

output "coding_hosted_postgres_access" {
  description = "Retired native-v2 private PostgreSQL path."
  value       = null
}
