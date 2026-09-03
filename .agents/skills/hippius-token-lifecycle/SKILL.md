---
name: hippius-token-lifecycle
description: Audit Hippius S3 master and sub-token age, expiry, scope, and backing GCP Secret Manager versions, then safely recreate or rotate Ditto's bucket-scoped credentials. Use for Hippius token inventory, expiry alerts, credential renewal, or probe-access cleanup; not for ordinary object operations.
---

# Hippius Token Lifecycle

Keep provider truth, secret custody, runtime IAM, and application activation separate. Pair live Hippius Console evidence with GCP metadata; Secret Manager version age alone does not prove a token's provider expiry.

## Audit

1. Resolve current Coding PR heads and runtime configuration before treating a historical secret or bucket as active.
2. In Hippius Console, open **S3 Storage -> Manage** and inspect both **Sub Tokens** and **Master Tokens**. Record only token name, permission, bucket count/names, created time, expiry, and status. Never show/copy a master token, access ID, secret, or secret suffix.
3. Run `scripts/audit_gcp_secret_versions.sh --project ditto-app-dev`. It reports metadata for Ditto's six managed Coding secret containers and exits `2` when a latest enabled version is missing or older than the configured maximum age.
4. Reconcile every active provider token, including tokens not backed by these six containers. Flag expired tokens and tokens expiring within seven days. Treat an unexplained bucket scope, all-bucket grant, or permission mismatch as a security finding regardless of age.
5. Inspect conditional IAM on the six secrets. Temporary probe access must name the exact runtime principal and expiry; report but do not silently renew or broaden it.

Managed Coding identities:

| Provider identity | Required provider scope | Secret Manager pair | Runtime boundary |
| --- | --- | --- | --- |
| private-input curator | Object Read & Write; private-input bucket only | `platform-coding-catalog-curator-access-key`, `platform-coding-catalog-curator-secret-key` | owner/offline only; Platform access only through a short probe condition |
| private-input Platform reader | Object Read Only; private-input bucket only | `platform-coding-catalog-access-key`, `platform-coding-catalog-secret-key` | Platform runtime |
| sealed-evidence mediator | Object Read & Write; sealed-evidence bucket only | `platform-coding-hippius-evidence-access-key`, `platform-coding-hippius-evidence-secret-key` | Platform runtime |

The current bucket contract is `ditto-subnet-coding-private-input` and `ditto-subnet-coding-sealed-evidence`. Re-read the active source/PR before using those names; never repurpose avatar, trace, miner-upload, or historical catalog credentials.

## Renew an expiring managed token

Credential creation, rotation, revocation, IAM changes, and live probes require the user's authority at the point of mutation. An audit request alone is read-only.

1. Create a replacement sub-token before touching the incumbent. Preserve the exact single-bucket scope and permission above. Use the owner-approved lifetime; do not silently change a 30-day token to `Forever`.
2. Capture the one-time pair directly into new Secret Manager versions. Never emit credentials through DOM/accessibility snapshots, terminal output, command arguments, shell tracing, or an unguarded `pbpaste`. If using **Copy All**, pipe the clipboard through a strict parser directly to `gcloud secrets versions add`, validate shape without printing, then clear the clipboard.
3. Keep the previous Secret Manager versions enabled until the replacement passes. If any credential appears in chat, logs, screenshots, or tool output, treat it as compromised: rotate again, install only the clean rotation, and disable the exposed versions.
4. Run the exact active PR head's `probe_hippius_coding_storage.py` from a clean detached worktree. Load secrets only inside the consumer process. Preserve the redacted receipt digest and verify:
   - anonymous GET, HEAD, and LIST are denied;
   - reader write/delete and every cross-bucket operation are denied;
   - full-byte readback and presigned exact-GET checks pass;
   - provider-permitted bucket listing is recorded as observed, never mistaken for application authority.
5. Verify the intended Platform VM service account can read only the required pairs. Curator access must be an expiry-conditioned probe grant and removed or allowed to expire after the probe.
6. Only after successful probe and runtime verification, revoke the incumbent provider token and disable its superseded Secret Manager versions. Re-read provider and GCP state afterward.

For ordinary secret-container or deployment ownership, also load `$ditto-subnet-release-ops`. A successful renewal/probe does not merge PRs, apply Terraform, deploy Platform, enable Coding, or make a shadow contract weight-eligible.

## Report

Report token names and scopes, provider expiry horizon, latest Secret Manager version numbers/states, temporary IAM principal/expiry, probe source SHA and redacted receipt digest, and any remaining merge/deploy/activation gap. Never report credential values or fingerprints derived directly from secrets.
