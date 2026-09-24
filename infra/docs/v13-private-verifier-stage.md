# V13 private verifier host: staged infrastructure proposal

This proposal is default off. `enable_v13_private_verifier=false` creates no
resources. It neither installs a protected case bank nor activates a scorer.
Review the exact Terraform plan and custodian set before any apply.

## Resources when enabled

| Resource | Proposed identity and boundary |
| --- | --- |
| VM | `ditto-v13-private-verifier-prod`, `us-central1-a`, Debian 13, e2-standard-4, 100 GiB balanced boot disk, Shielded VM, no public IP, deletion protection |
| Network | `10.32.0.0/24` dedicated subnet; IAP SSH only; deny egress to all RFC1918 private networks; no HTTP ingress |
| Runtime identity | `ditto-v13-verifier` service account; bank object read, ticket secret read, logging and metrics only; no validator hotkey, database, or provider secret read |
| Protected bank | Empty regional `ditto-app-dev-v13-private-bank` GCS bucket with uniform access, public access prevention, versioning, and prevent-destroy; no objects written by Terraform |
| Secrets | Empty `v13-private-ticket-key` container readable by Platform API and verifier; empty `v13-private-provider-key` container readable only by Platform API; no versions or values in Terraform |
| Operators | `v13_private_verifier_operators` defaults to empty. Each approved principal receives instance OS Admin Login, exact-instance IAP, compute viewer, and service account user. Review existing project-wide SSH/IAP grants separately. |

The stage playbook creates only a locked verifier user and an empty owner-only
bank directory. It deliberately does not install Docker, fetch bank objects,
read secret versions, start a scorer, or run a case.

## Read-only live-state plan, 2026-09-24

The GCS-backed `gcp-platform` state was read with a **targeted** Terraform
plan for all 17 new resource/module addresses, using
`enable_v13_private_verifier=true`, `manage_dns=false`, and synthetic values
for unrelated required secret variables. The redacted result was **14 creates,
0 changes, 0 destroys** for this proposal. Three existing dependencies
(`google_project_service.iap`, the Platform API service account, and the VPC)
were no-ops. This is an actual remote-state comparison, but a targeted plan
does not certify unrelated stack drift. A fresh protected full plan with real
deployment variables remains an apply gate.

Read-only project checks found `10.32.0.0/24` unused among current regional
subnets, IAP API enabled, and the proposed bucket name returned 404 (availability
must be rechecked at apply). Regional reported quota was E2 CPUs 0/600,
instances 7/6000, and total disk 0/102400 GiB; a single four-vCPU, 100-GiB
host fits these reported limits, subject to zone capacity and quota refresh.

The project IAM policy has three unconditional OS Admin Login principals, two
unconditional IAP tunnel principals, and six unconditional service-account-user
principals; one principal appears in both OS Admin Login and IAP sets. More
directly, project-wide storage admins (3), object viewer (1), secret accessors
(9), secret admins (2), owners (3), and editors (4) inherit onto any bank bucket
and secret container created here. Bucket-local and secret-local grants do not
remove those effective privileges. The proposal's empty operator list does
not establish exclusive custody. **Do not apply this project-local alternative
for protected bytes or key versions.** A separate project with reviewed
organization-owner access is the recommended custody boundary.

## Cost envelope

At the published on-demand `us-central1` rate, e2-standard-4 is
**$0.13402284/hour**, or about **$97.84 for 730 hours**. A 100 GiB balanced
persistent disk at $0.000136986/GiB-hour adds about **$10.00/month**. Thus an
always-on staged host is approximately **$108/month before** NAT, logging,
data transfer, bank bytes/operations, secret versions, and model inference.
The empty bank and empty secret containers carry no stored-byte or active-
version charge. A regional Standard bank is roughly $0.02/GiB-month once
populated; active secret versions beyond the billing account's free allowance
are $0.06/version-month. These are public list-price estimates, not a live
billing quote or capacity commitment.

Sources: [Google Compute E2 pricing](https://cloud.google.com/products/compute/pricing/general-purpose),
[persistent disk pricing](https://cloud.google.com/compute/disks-image-pricing),
[Cloud Storage pricing](https://cloud.google.com/storage/pricing), and
[Secret Manager pricing](https://cloud.google.com/secret-manager/pricing).

## Remaining activation gates

1. Review actual plan/state drift, region quota, bucket-name availability,
   existing project-wide IAP/OS Login grants, and exact operator principals.
2. Approve one immutable protected bank generation and its independent
   signing authority. Build a one-shot installer that fetches only that
   generation, verifies every digest and signed approval, and atomically
   exposes a read-only bank to the verifier uid. Never mount it into a miner
   container.
3. Implement and review the scorer-local manifest resolver and sandbox
   factory: exact registered manifest/image, fresh rootless container and
   broker per case, no caller-selected protected bytes, revocation/drain,
   strict stop, signed sanitized receipt. Block container access to the GCE
   metadata endpoint and host private addresses. Stage a dedicated rootless Docker
   service under the verifier uid only after this is ready.
4. Implement the separate V13 provider grant with case, source, model,
   budget, and expiry binding. Add secret versions out of band and deploy
   both consumers together. Do not reuse the capped L4 review key.
5. Run one report-only held case against one independently reviewed control
   after fresh Backroom identity checks. Keep policy activation and waves off.

Rollback starts by disabling ticket issuance and the verifier service,
draining outstanding tickets, then revoking the provider grant and bank/key
access. Preserve the bank generation and immutable receipts for audit.

The Terraform flag is a **creation gate, not a rollback switch**. If an
operator ever approves staging this alternative, first commit
`enable_v13_private_verifier=true` to the production intent file and then
apply the exact reviewed plan; keep that intent true on subsequent routine
plans. Setting it back to false would propose destruction of the VM, bucket,
and secrets and be blocked by deletion protection/prevent-destroy. A
supervised teardown must separately disable runtime and grants, preserve the
bank/audit trail, and review a specific state/resource disposition plan.
