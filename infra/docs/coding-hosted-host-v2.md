# Platform-owned native-v2 qualification host

This is a dormant host foundation, not a deployed worker or an approved private
execution environment. `enable_coding_hosted_host = false` in both the variable
default and reviewed production intent creates no native-v2 VM, network, NAT,
service account, or operator grant. The legacy `coding_executor_host_count = 0`
and all existing Coding gates remain unchanged.

The [native-v2 trust decision](../../docs/coding-platform-private-execution-v2.md)
places private execution under Platform custody. The old k=3 executor cohort
and its validator-facing scorer are not the native-v2 deployment target. Do not
repurpose them, attach a validator wallet, inherit their operator list, or relax
the native launcher's socket checks to make the old role fit.

## What an explicitly approved apply would create

- One `ditto-coding-hosted-v2` private Debian VM labelled `role=coding_hosted`
  and `owner=platform`, using the existing compute module's deletion protection,
  OS Login, secure boot, vTPM and integrity monitoring. The initial disk is
  200 GB; this is a qualification host, not a three-execution quorum.
- A separate custom VPC with one IPv4 subnet, no peering or private route into
  the Platform/validator VPC, no public VM address and no public ingress rule.
  IAP SSH is the sole configured ingress exception.
- A dedicated telemetry-only service account. No provider, storage, registry,
  PostgreSQL, Secret Manager, signing or key-custody permission is granted.
- Explicit `user:email` Platform custodians through instance-scoped OS Login
  and IAP SSH (port 22 only), plus actAs on this otherwise-empty runtime identity.
  `coding_hosted_operators` defaults empty and enabling without it fails. No
  project-wide compute viewer or tunnel grant is added; operators need existing
  approved discovery permissions. Existing broader project/org grants must also
  be audited before approval; this module does not revoke inherited access.

The host may download provisioning packages over HTTP(S) through NAT scoped to
its subnet. Higher-priority egress rules deny RFC1918 and link-local destinations;
other ordinary egress is denied. These are **host provisioning rules, not a
candidate sandbox**. Google Cloud always allows the VM's own metadata-server
traffic regardless of VPC firewall rules; the link-local rule does not change
that exception. See [Google's firewall documentation](https://cloud.google.com/firewall/docs/firewalls#alwaysallowed).
Candidate metadata/DNS/egress denial must be independently installed and tested
inside the host before any hostile code runs. No daemon or candidate is installed
by this module.

## Activation and retention

Prepare a separate reviewed change to production intent with the enable flag
and exact approved custodian set. Use the existing protected `gcp-platform`
plan/apply workflow and review the saved plan for unrelated drift, replacements,
IAM, network changes and cost. Do not use an out-of-band CLI create, a targeted
apply that omits safeguards, or a new workflow bypass. This PR does not dispatch
that workflow or grant its identities extra permissions.

Once provisioned, record the exact project/zone/instance ID and effective IAM.
The shared compute module refuses implicit destruction. Setting the flag back
to false is not a safe cleanup or rollback once state/images/evidence exist:
reconcile retained material, plan a separately authorized decommission, and
preserve private tombstones/evidence. Do not disable deletion protection or
remove state as an automatic recovery step.

## Required next layers

1. A separate native-v2 daemon/host role, targeting `role_coding_hosted` only.
   Its worker-owned socket must be mode `0600` in a private `0700` directory;
   the legacy role's group-shared `0660` socket is not compatible. Prove rootless
   identity, the isolated-daemon label, empty daemon credentials, UID/subuid
   separation and the candidate firewall/proxy boundary. Do not weaken existing
   launcher checks. No current Ansible playbook targets this new role.
2. Approved installed Platform/Go revisions and digest-verified image import,
   then synthetic hostile isolation and unchanged private base/reference
   qualification through the actual executor. VM readiness is not driver proof.
3. A separately reviewed trusted-control network path and bounded database,
   provider, Hippius and custody provisioning. This VPC currently has no private
   PostgreSQL route. Do not expose production PostgreSQL publicly to bypass it.
   Platform control capabilities and candidate egress must remain distinct.
4. Durable recovery/public signing, encrypted release publication/readback/
   registration and one deployed private shadow canary. Hippius stays the sole
   remote store for private inputs and sealed evidence. No private corpus belongs
   in Git, Terraform state, images, user-data or qualification logs.

## Validation

The module's credential-free Terraform tests use a mocked Google provider and
`command = plan` exclusively. They cover disabled resources/IAM, explicit
custodians, invalid identities/disk sizes, network policy and least-privilege IAM.
Root regression tests protect production defaults, legacy separation, VM
hardening and CI routing. These checks do not contact a live daemon, create a VM,
approve an image or establish live firewall enforcement.

```bash
terraform -chdir=infra/terraform/modules/coding-hosted-host init -backend=false
terraform -chdir=infra/terraform/modules/coding-hosted-host test
terraform -chdir=infra/terraform/stacks/gcp-platform init -backend=false
terraform -chdir=infra/terraform/stacks/gcp-platform validate
uv run pytest -q ditto/tests/test_coding_hosted_infrastructure.py
```
