#!/usr/bin/env bash
# Export everything infra/ansible/playbooks/gcp-platform-app.yml needs, straight from
# the Terraform outputs. SOURCE it (it exports into your shell); do not execute.
#
#   source infra/ansible/scripts/platform-app-env.sh
#   ansible-playbook -i infra/ansible/inventory/gcp.yml \
#     infra/ansible/playbooks/gcp-platform-app.yml --limit ditto-platform-prod
#
# WHY THIS EXISTS: on 2026-07-23 that playbook was run against prod with only
# GCP_OSLOGIN_USER exported — the only variable its header documented. The four
# PLATFORM_* values fell back to their placeholder/empty defaults, the play
# reported success, and /opt/ditto-platform/.env was written with
# POSTGRES_HOST=TODO_TF_OUTPUT_pg_internal_ip and no DATA_PIPELINE_URL. Hand-
# transcribing four terraform outputs before every converge is the failure mode;
# this script removes the transcription.
#
# The playbook does NOT shell out to Terraform on its own (a `lookup('pipe',
# 'terraform output …')` in group_vars would make every converge depend on a
# terraform binary, an initialised working directory, GCS backend credentials
# and state availability — turning a config-management run into something that
# can fail, or worse silently return empty, for reasons on the operator's laptop
# rather than on the host). Sourcing is explicit, greppable, and fails where the
# operator can see it. The role's preflight assertions are what actually
# guarantee the values arrived.
#
# GCP_OSLOGIN_USER is deliberately NOT set here: it is per-person, not
# infrastructure. Find yours with `gcloud compute os-login describe-profile`.

_pae_tf_dir="${PLATFORM_TF_DIR:-infra/terraform/stacks/gcp-platform}"

if [ ! -d "$_pae_tf_dir" ]; then
  echo "platform-app-env.sh: no such directory: $_pae_tf_dir" >&2
  echo "  run this from the repo root, or set PLATFORM_TF_DIR to the env dir." >&2
  unset _pae_tf_dir
  return 1 2>/dev/null || exit 1
fi

if ! command -v terraform >/dev/null 2>&1; then
  echo "platform-app-env.sh: terraform not on PATH." >&2
  unset _pae_tf_dir
  return 1 2>/dev/null || exit 1
fi

# $1 = env var name, $2 = terraform output name, $3 = required|optional
_pae_export() {
  local var="$1" output="$2" requirement="$3" value

  # Already exported by the operator? Leave it — an explicit override wins.
  if [ -n "${!var:-}" ]; then
    echo "  $var: already set, left as-is"
    return 0
  fi

  if ! value="$(terraform -chdir="$_pae_tf_dir" output -raw "$output" 2>/dev/null)" ||
    [ -z "$value" ]; then
    if [ "$requirement" = required ]; then
      echo "  $var: FAILED to read \`terraform output -raw $output\`" >&2
      echo "     is the working directory initialised (terraform -chdir=$_pae_tf_dir init)" >&2
      echo "     and are your GCP credentials current? Do NOT run the playbook without this." >&2
      _pae_failed=1
    else
      # An optional output is absent whenever its feature is not deployed
      # (enable_embedder / enable_datapipeline = false). That is a legitimate
      # state, so leave the variable unset — the play warns that the feature
      # renders disabled.
      echo "  $var: not available (feature not deployed) — will render DISABLED"
    fi
    return 0
  fi

  export "$var=$value"
  echo "  $var: set from terraform output $output"
}

_pae_failed=0
echo "platform-app-env.sh: reading $_pae_tf_dir outputs"
_pae_export PLATFORM_PG_HOST pg_internal_ip required
_pae_export PLATFORM_STORAGE_ACCESS_KEY storage_hmac_access_id required
_pae_export PLATFORM_EMBEDDER_URL embedder_url optional
_pae_export PLATFORM_DATAPIPELINE_URL datapipeline_url optional

if [ -z "${GCP_OSLOGIN_USER:-}" ]; then
  echo "  GCP_OSLOGIN_USER: NOT set — export your OS Login username" >&2
  echo "     (gcloud compute os-login describe-profile)" >&2
  _pae_failed=1
fi

unset -f _pae_export
unset _pae_tf_dir

if [ "$_pae_failed" != 0 ]; then
  unset _pae_failed
  echo "platform-app-env.sh: incomplete — fix the above before converging." >&2
  return 1 2>/dev/null || exit 1
fi
unset _pae_failed
