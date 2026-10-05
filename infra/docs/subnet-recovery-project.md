# Dedicated subnet recovery project

The existing `ditto-subnet` project (number `286408627661`, organization
`406520318131`) holds recovery credentials and the restore identity. PostgreSQL
and Platform VMs stay in `ditto-app-dev`. This avoids inheriting that project's
broad Secret Manager payload grants without moving or restarting a VM.

The project was empty of service accounts and had only
`user:peyton@omniaura.ai` as project Owner on 2026-10-05. Billing was disabled.
The approved candidate links the existing open `Billing Omni Aura` account
`01279D-184F4C-3102C7`. No new compute is provisioned.

## One-time owner bootstrap

`gcp-subnet-bootstrap` uses a separate GCS state prefix and contains exactly:

- The existing billing account link.
- Five APIs: Secret Manager, IAM, IAM Credentials, STS, Cloud Resource Manager.
- Two project custom roles and their grants to the existing Terraform plan/apply
  service accounts in `ditto-app-dev`.

The first live plan on 2026-10-05 showed **10 creates, 0 updates, 0 deletes**.
Its binary SHA-256 was
`d2f3292f7f77079342a044ec562f4d70747682904dcaefd0476914074dd6f03a`.
It passed `check-subnet-recovery-plan.py bootstrap`. It has not been applied.

The plan identity gets resource metadata and IAM-policy reads. The apply
identity additionally manages empty containers, service accounts, federation,
and the reviewed IAM bindings. Neither custom role has secret-version access,
token minting, service-account act-as, resource deletion, or project IAM writes.
The apply identity can change per-secret and service-account IAM: it is a
trusted administrator, not an identity isolated from potential escalation.
Protect changes to these configurations, restore code, and workflows through
review and the existing protected infrastructure environments.

As [`ci-bootstrap.md`](ci-bootstrap.md) explains, "The public workflow cannot
create the identity that authorizes its own first run." This owner-controlled
root is the equivalent audited one-time bootstrap. It is deliberately absent
from public workflow root choices. Review the committed source and saved plan
and obtain explicit owner authorization for this first privileged apply; do
not replace it with ad-hoc `gcloud` IAM changes.

From an authorized owner controller, prepare the private plan:

```sh
umask 077
mkdir -p .tmp
terraform -chdir=infra/terraform/stacks/gcp-subnet-bootstrap init -input=false -lockfile=readonly
terraform -chdir=infra/terraform/stacks/gcp-subnet-bootstrap plan -input=false -out=../../../../.tmp/subnet-bootstrap.tfplan
terraform -chdir=infra/terraform/stacks/gcp-subnet-bootstrap show -json ../../../../.tmp/subnet-bootstrap.tfplan > .tmp/subnet-bootstrap-plan.json
python3 infra/scripts/check-subnet-recovery-plan.py bootstrap .tmp/subnet-bootstrap-plan.json
shasum -a 256 .tmp/subnet-bootstrap.tfplan
```

After the exact saved plan and source are approved, apply that binary under the
owner's audited bootstrap authority. Record source SHA, plan checksum,
authorization and the resulting resource identities. A changed plan needs fresh
review. Do not apply the recovery root locally under that bootstrap approval.

## Protected recovery provisioning

After merge and bootstrap, use `infra-plan-apply.yml` on exact current main:

1. Plan/apply the following four `gcp-platform` targets separately from the
   snapshot schedule; require exactly four creates and no unrelated changes:

   ```text
   google_storage_bucket_iam_member.terraform_plan_subnet_recovery_state_lock,google_storage_bucket_iam_member.terraform_plan_subnet_recovery_state_initial_create,google_project_iam_custom_role.pg_backup_snapshot_reader,google_project_iam_member.pg_backup_snapshot_reader
   ```

   These are only two narrow recovery state-object grants and snapshot metadata
   access for Platform. The existing state bucket and its other policies stay
   in the existing project.
2. Plan root `gcp-subnet-recovery` without targets. The first plan must contain
   **15 creates**: six empty secrets, five per-secret reader grants, the restore
   service account, pool/provider and exact service-account federation grant.
   `check-subnet-recovery-plan.py recovery` rejects wrong projects, broader
   readers, replacements, deletions, unexpected resources or weaker federation.
3. Review the private plan, exact current-main SHA, run id and checksum; apply
   the saved binary through protected `infra-apply`. No Cloudflare/application
   password environment is passed to the recovery plan or apply command.

The Platform API service account gets only the separate Hippius reader pair.
The restore identity gets that pair plus the private age identity. The DB VM
has no attached service account: Ansible fetches its writer pair and public
recipient on the authorized controller and writes protected host files.
Restore federation requires immutable repository/owner IDs, exact main
workflow, prod environment and only scheduled/manual events.

## Custody and activation

Before adding any secret versions, inspect project and ancestor policies,
secret policies and service-account impersonation paths. The initial project
audit is not proof of effective access after provisioning. Restrict private
identity reads to the approved human owners and dedicated restore identity.
The old project's payload grants must not acquire a new-project binding.

All six values belong in `ditto-subnet`, outside Terraform state. Generate the
age key offline and retain an independently protected human recovery copy.
Capture the owner-approved 30-day Hippius single-bucket writer and reader
tokens directly into Secret Manager, then prove their scopes. Follow
[`platform-postgres-backup-restore.md`](platform-postgres-backup-restore.md)
for secret capture, host convergence, snapshot activation and actual restore.

Bootstrap, protected infrastructure apply, host activation, and real backup
and restore remain separately evidenced stages. Billing/API/empty-secret
provisioning alone does not satisfy SN-49.
