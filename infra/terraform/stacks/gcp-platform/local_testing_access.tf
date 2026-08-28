###############################################################################
# Human access for repo-local tests that make bounded real OpenRouter calls.
#
# localstack/lib.sh reads this existing secret only when OPENROUTER_API_KEY is
# unset. Keep this grant separate from debug_operators and from the
# platform-owned validator-openrouter-key used by production inference.
###############################################################################

resource "google_secret_manager_secret_iam_member" "local_openrouter_key_access" {
  for_each  = toset(var.local_openrouter_secret_users)
  project   = var.project
  secret_id = "LOCAL_OPENROUTER_API_KEY"
  role      = "roles/secretmanager.secretAccessor"
  member    = each.value
}
