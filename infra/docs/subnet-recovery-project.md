# Dedicated subnet recovery project

The existing `ditto-subnet` project (number `286408627661`, organization
`406520318131`) holds recovery credentials and the restore identity. PostgreSQL
and Platform VMs stay in `ditto-app-dev`. This avoids inheriting that project's
broad Secret Manager payload grants without moving or restarting a VM.

The project was empty of service accounts and had only
`user:peyton@omniaura.ai` as project Owner on 2026-10-05. Billing was disabled.
The applied owner bootstrap links the existing open `Billing Omni Aura` account
`01279D-184F4C-3102C7`. No new compute is provisioned.

## One-time owner bootstrap

`gcp-subnet-bootstrap` uses a separate GCS state prefix and contains exactly:

- The existing billing account link.
- Six APIs: Secret Manager, IAM, IAM Credentials, STS, Cloud Resource Manager,
  and Cloud Storage.
- Two project custom roles, dedicated `subnet-recovery-tf-plan` and
  `subnet-recovery-tf-apply` service accounts in `ditto-subnet`, and their grants.
- A separate `subnet-recovery-infra` pool/provider admitting only the exact
  repository/owner IDs, current main infrastructure workflow, manual dispatch
  and protected `infra-plan` / `infra-apply` environment subjects.
- Private, versioned `ditto-subnet-recovery-tfstate` storage with uniform bucket
  access and public access prevention. Only the dedicated CI pair receives
  object administration; neither receives bucket IAM administration.

A fresh bootstrap contains 20 resources. The earlier 10-resource receipt below
is historical and does not prove the new custody isolation has been applied.

The first live plan on 2026-10-05 showed **10 creates, 0 updates, 0 deletes**.
Its binary SHA-256 was
`d2f3292f7f77079342a044ec562f4d70747682904dcaefd0476914074dd6f03a`.
It passed `check-subnet-recovery-plan.py bootstrap`. With explicit owner
approval, that exact binary was applied from source
`1aa25edd9f6a0a6d979c528a7a7f33ea0f492586` on 2026-10-05:
**10 added, 0 changed, 0 destroyed**. Read-back at 16:38 UTC verified the
billing account link, all five APIs, both custom roles' complete permission
sets and their exact CI principals. The project policy contains only those two
custom-role bindings and Peyton's existing Owner binding. A fresh owner plan
reported no changes. No secret containers or values existed at that check.

Applied role IDs:

- `projects/ditto-subnet/roles/subnetRecoveryTerraformPlan` (15 permissions)
- `projects/ditto-subnet/roles/subnetRecoveryTerraformApply` (25 permissions)

The original bootstrap completed, but its delegation is superseded by the
custody migration. A live 2026-10-05 audit found unconditional old-project
`roles/iam.serviceAccountTokenCreator` for `ditto-backend` and
`firebase-adminsdk-4caea`. These principals can impersonate the shared Terraform
apply account, whose recovery custom role can change per-secret IAM. Direct
payload denial alone therefore did not close the private-key access path.

The expanded owner bootstrap replaces exactly the two task-owned old CI
custom-role grants with the dedicated new-project CI pair. The private saved-plan
guard checks both old principals/roles/projects, rejects partial migration and
rejects every other replacement/deletion. The new pool/provider and state bucket
also remove dependencies on old-project federation and mutable plan storage.
Do not install private-key versions until the migration is applied and effective
project/ancestor and service-account IAM are read back. The owner bootstrap's
own state remains in its owner-controlled old-project prefix; the dedicated CI
identities have no write grant to that bootstrap state.

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

1. The separate `gcp-platform` four-create access plan was applied from
   `c4736e5ab72f02f9d7f3321644767c104a5dcbb1` in protected run `37385152877`.
   Its checksum was
   `cc23ecc3c3b44681049f01b7496bf52519fa50b61eb45c59417ffebeed79ef67`.
   Two resources grant only `compute.snapshots.list` metadata to Platform; the
   other two narrow shared-plan state grants predate the custody migration and
   become unused when recovery state moves to the dedicated bucket. They do
   not grant new-project secret access. Cleanup must use a separately reviewed
   scoped protected plan, without altering unrelated shared-project grants.
   The snapshot apply `37385744233` created `ditto-pg-platform-daily`, but its
   disk attachment failed on a malformed policy path. Source now passes the
   policy name; the recovery checker permits the exact unchanged policy plus
   one attachment create, with no VM/disk or unrelated mutation. A fresh sealed
   plan and protected apply are required before claiming the schedule is live.
2. Plan root `gcp-subnet-recovery` without targets. The first plan must contain
   **15 creates**: six empty secrets, five per-secret reader grants, the restore
   service account, pool/provider and exact service-account federation grant.
   `check-subnet-recovery-plan.py recovery` rejects wrong projects, broader
   readers, replacements, deletions, unexpected resources or weaker federation.
3. The workflow selects the dedicated new-project federation provider, plan/apply
   accounts and `ditto-subnet-recovery-tfstate` plan bucket only for this root.
   Reconfigure the empty recovery backend after verifying the old prefix contains
   no managed resources. Do not migrate a populated state without reviewing it.
   Review the private plan, exact current-main SHA, run id and checksum; apply
   the saved binary through protected `infra-apply`. No Cloudflare/application
   password environment is passed to the recovery plan or apply command.

The Platform API service account gets only the separate Hippius reader pair.
The restore identity gets that pair plus the private age identity. The DB VM
has no attached service account: Ansible fetches its writer pair and public
recipient on the authorized controller and writes protected host files.
Restore federation requires immutable repository/owner IDs, exact main
workflow, prod environment and only scheduled/manual events.

## Custody and activation

After merge and with repository-administration authorization, install the exact
ruleset in `infra/github/platform-pg-backup-ruleset.json` before installing the
private identity. It requires an independent `admin` team approval for restore
code, its imported backup module, its dependency lock, the infrastructure
workflow and recovery custody roots. Read back the rule and its file patterns;
the saved JSON alone does not enforce it. Organization/repository admins retain
the same explicit emergency bypass as the existing Hippius probe rule.

The restore runner installs only hash-locked binary wheels in an isolated venv
and runs copied reviewed files using Python isolated mode. It does not install
the Platform application, local packages, or their build hooks. The dependency
install precedes secret capture; no plaintext dump or key artifact is uploaded.

Before adding any secret versions, inspect project and ancestor policies,
secret policies and service-account impersonation paths. The initial project
audit is not proof of effective access after provisioning. Restrict private
identity reads to the approved human owners and dedicated restore identity.
The old project's payload grants must not acquire a new-project binding.

All six values belong in `ditto-subnet`, outside Terraform state. Generate the
age key offline and retain an independently protected human recovery copy.
The owner selected their password manager and requested a pause to save the
private key. Generate into a protected file outside disposable workspaces;
pause for confirmation before storing its private Secret Manager version or
activating production. Never return the private bytes in tool output or chat.
Capture the owner-approved 30-day Hippius single-bucket writer and reader
tokens directly into Secret Manager, then prove their scopes. Follow
[`platform-postgres-backup-restore.md`](platform-postgres-backup-restore.md)
for secret capture, host convergence, snapshot activation and actual restore.

Bootstrap, protected infrastructure apply, host activation, and real backup
and restore remain separately evidenced stages. Billing/API/empty-secret
provisioning alone does not satisfy SN-49.
