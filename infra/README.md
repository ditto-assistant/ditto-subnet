# Public subnet infrastructure

This directory is the source of truth for infrastructure that operates SN118:
the Platform API and database, validator/screener hosts, Targon-first capacity
controller, trusted Kaniko builder, Artifact Registry repositories, and the
public Backroom custom domain.

Ditto product infrastructure stays private. In particular, this tree must not
grow resources for `backroom.heyditto.ai`, product feature flags, app review,
product airdrops, or the Ditto application backend.

## State ownership

- `terraform/stacks/gcp-platform` retains the existing GCS backend
  `gs://ditto-app-dev-tfstate/gcp-platform`; moving the source repository does
  not move or recreate state.
- `terraform/stacks/cloudflare-dittobench` uses the independent prefix
  `cloudflare-dittobench-ai` and owns only new `dittobench.ai` resources.

Pull requests run credential-free formatting and validation. Real plans and
applies use `.github/workflows/infra-plan-apply.yml`, GCP Workload Identity
Federation, protected GitHub environments, and private GCS plan objects. The
workflow applies the exact reviewed plan binary for an exact main commit.

No Terraform or Ansible action is automatic on merge.
