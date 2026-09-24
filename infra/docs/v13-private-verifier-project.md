# V13 protected verifier custody: dedicated project proposal

This is a **default-off, stage-only** alternative to placing the verifier in
`ditto-app-dev` (draft #2229). It creates no protected bank object, secret
version, provider grant, rootless Docker daemon, scorer service, or case run.
No plan has been applied.

## Read-only custody comparison, 2026-09-24

| Boundary | Stage in `ditto-app-dev` | Dedicated new project |
| --- | --- | --- |
| Existing IAM inheritance | Project-wide storage admins/viewer, secret accessors/admins, owners/editors, OS Login/IAP grants already apply to new resources | None of those **project-level** grants carry over; only organization-level grants inherit |
| Existing operations | Reuses VPC, NAT, state root, deploy identity | Needs project creation/billing link, APIs, VPC/NAT, separate state prefix, inventory, and approved project deploy identity |
| Change risk | Narrowing broad live IAM grants could disrupt current Platform, screener, and deploy workflows | Leaves current project IAM and services intact; only new resources are planned |
| Residual authority | Existing project owners/admins remain effective on bank and key | Three organization-level `roles/owner` principals still inherit; they are root custodians and can access secret versions or change IAM |

The project policy read showed unconditional `storage.admin` (3 members),
`storage.objectViewer` (1), `secretmanager.secretAccessor` (9),
`secretmanager.admin` (2), `roles/owner` (3), `roles/editor` (4),
OS Admin Login (3), and IAP tunnel access (2). These are direct project
bindings; names are intentionally omitted. The organization policy showed
three `roles/owner` principals and no direct storage/secret/OS Login/IAP
bindings. `roles/owner` includes secret-version access, OS Admin Login, IAP,
and IAM mutation; this design does not isolate from organization owners.

**Recommendation:** use the dedicated project. Narrowing the current
project's grants would change active production access for many unrelated
services. A new project removes those inherited project bindings without
mutating current operations. Before any protected bytes or key versions,
review the three organization owners, inherited custom/group grants, and the
new project's effective IAM. If those owners are outside the approved custody
set, create the project under a separately controlled parent instead.

## Exact stage plan

The proposed project ID is `ditto-v13-private-verifier`, subject to global
availability at apply. `enable_v13_private_project=false` is the default.
The design has **two state roots**. The bootstrap root keeps only the project,
billing link, Storage API, an empty state bucket, and one exact bucket
object-admin grant to the proposed state custodian in a separate prefix of
the existing state bucket. Its read-only full plan against that remote state
returned **4 creates, 0 changes, 0 destroys**. It contains no verifier,
bank, key, provider, or host-custodian metadata. The verifier root's backend
is the new bucket **inside the new project**, so the existing
`ditto-app-dev` project-wide storage admins cannot read or rewrite verifier
state by inheriting an IAM role on the old bucket. The verifier root cannot
run a remote-state plan until an explicitly approved bootstrap creates that
bucket. Its earlier 29-create shared-backend plan is superseded; do not use
it for approval. An **offline, empty-state** verifier plan with the proposed
custodian produced 28 creates, 0 changes, 0 destroys; it checks resource
shape only, and Storage API may be a no-op after bootstrap. After bootstrap,
run a fresh protected full plan with the
proposed custodian supplied from the private approval package.

The verifier root specifies seven APIs, VPC/subnet/router/NAT, two firewalls,
e2-standard-4 Shielded private VM with 100 GiB disk, dedicated service
account, observability grants, empty versioned regional bank bucket with only
verifier object-read grant, and two empty secret containers. The Platform API
service account alone gets provider-key read; Platform and verifier get
ticket-key read. The verifier never gets provider-key read. Four planned IAM
grants attach to the one proposed human custodian.

Both roots are checked in disabled. The checked-in intent selects no human host custodian; the private approval
package names one proposed operator for the protected plan. With `enable=false`,
no grant exists. If separately approved for creation, that custodian receives
instance OS Admin Login, exact-instance IAP, compute viewer, and actAs on the
verifier SA. The stage playbook and inventory are scoped to
this new project and create only an unprivileged uid and empty private bank
directory. Before convergence, run the dynamic-inventory preflight; it invokes
the GCP plugin and requires the exact host group and Terraform labels. It must
fail while the project/VM is absent. The proposed bank bucket name and project ID cannot be reserved by
a read-only check; GCP returned permission denied-or-absent for the project
lookup. A fresh plan and name/quota check are required at apply.

After an approved apply and before any stage playbook run:

```bash
python3 infra/ansible/scripts/check-v13-private-inventory.py
ANSIBLE_ROLES_PATH=infra/ansible/roles ansible-playbook \
  -i infra/ansible/inventory/v13-private-gcp_compute.yml \
  infra/ansible/playbooks/gcp-v13-private-verifier-stage.yml
```

An approved bootstrap requires a reviewed intent commit setting its flag true,
then a read-only exact-head plan and separate apply approval. The protected
`infra-plan-apply.yml` workflow has a `gcp-v13-private-bootstrap` root: its
plan checks out current `main`, rejects targets, requires one private state
custodian, and permits only the four bootstrap creates. It keeps detailed
Terraform output on the runner and publishes the plan SHA, run ID, and binary
SHA-256; the binary plan and checksum are held in the private CI plan bucket.
The `infra-apply` environment approval is required before apply. Apply
requires the reviewed SHA-256 as a dispatch input, verifies the stored binary
plan against it, and requires its commit still equal current `main`.

The route cannot run until separately approved setup adds dedicated bootstrap
plan/apply service accounts and WIF bindings, the `infra-plan` secret
`GCP_V13_BOOTSTRAP_PLAN_SA`, the `infra-apply` secret
`GCP_V13_BOOTSTRAP_APPLY_SA`, and `infra-plan` values
`V13_PRIVATE_ORGANIZATION_ID`, `V13_PRIVATE_BILLING_ACCOUNT_ID`, and
`V13_PRIVATE_STATE_CUSTODIANS_JSON`, plus sufficient temporary
project-creation/billing/bucket authority. The generic Terraform apply account
does not have direct organization project-creator or billing-user authority;
the workflow rejects missing dedicated identities. The existing
`gcp-platform` apply service account receives no new-project grant in this
plan. Revoke temporary bootstrap roles after state-bucket creation;
organization owners remain the reviewed root trust set. The
verifier root needs a subsequent reviewed intent commit setting its flag true
and an exact-head protected plan using its own backend. Keep both flags true
on subsequent routine plans. Setting either false after creation proposes
deletion that project/VM deletion protection and bucket/secret
prevent-destroy will block.
Runtime rollback disables issuance/services/grants while retaining Terraform
intent. Eventual infrastructure teardown needs a supervised, resource-specific
disposition plan that preserves the bank and audit evidence.

## Cost envelope

At current Google on-demand list prices, 730 hours of e2-standard-4 costs
about **$97.84**, 100 GiB balanced persistent disk **$10.00**, and one Cloud
NAT assignment plus one NAT IP about **$4.67**. The always-on baseline is
therefore **about $112.51/month** before NAT data processing, internet
egress, logs, protected bank bytes/operations, secret versions, and private
inference. A regional Standard bank is about $0.02/GiB-month; a non-free
active secret version about $0.06/month. Project creation and empty
containers do not add a fixed service charge. Billing-account policies and
any quota or discount changes need a fresh quote before apply.

Price sources: [Compute E2](https://cloud.google.com/products/compute/pricing/general-purpose),
[persistent disk](https://cloud.google.com/compute/disks-image-pricing),
[Cloud NAT](https://cloud.google.com/nat/pricing),
[Cloud Storage](https://cloud.google.com/storage/pricing), and
[Secret Manager](https://cloud.google.com/secret-manager/pricing).

## Apply and activation boundaries

1. Review this exact root, full plan, organization owner set, project creator
   and billing link authority, project ID and bucket uniqueness, and quota.
   Approve explicit host custodians. An empty custodian set means Ansible
   cannot connect through the intended path.
2. After a separately approved project apply, re-read effective IAM before
   installing any protected object or secret version. Preserve an immutable
   signed bank approval and exact object-generation receipt.
3. Implement the scorer-local registered-manifest resolver, fresh rootless
   sandbox/broker factory, metadata-server block, exact image binding,
   source revocation/drain/strict stop, and signed sanitized receipt. Stage
   the Docker daemon and scorer only after code review. Implement the V13
   provider grant with case/source/model/budget/expiry limits.
4. Run one current held target and independently reviewed control in
   report-only mode after fresh Backroom identity checks. No policy verdict
   or rescreen wave is authorized by this infrastructure plan.

Rollback starts by disabling ticket issuance and the verifier service,
draining tickets, revoking the provider grant and bank/key IAM, and stopping
the daemon. Preserve protected provenance and immutable receipt history.
