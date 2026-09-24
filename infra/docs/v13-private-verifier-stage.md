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
   strict stop, signed sanitized receipt. Stage a dedicated rootless Docker
   service under the verifier uid only after this is ready.
4. Implement the separate V13 provider grant with case, source, model,
   budget, and expiry binding. Add secret versions out of band and deploy
   both consumers together. Do not reuse the capped L4 review key.
5. Run one report-only held case against one independently reviewed control
   after fresh Backroom identity checks. Keep policy activation and waves off.

Rollback starts by disabling ticket issuance and the verifier service,
draining outstanding tickets, then revoking the provider grant and bank/key
access. Preserve the bank generation and immutable receipts for audit.
