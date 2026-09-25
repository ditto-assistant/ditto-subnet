# SN118 treasury host activation intent for protected plan review

Status: **draft intent, not an apply approval**. This proposal turns on two
Terraform flags in `prod.auto.tfvars` and names `peyton@omniaura.ai` as the
sole IAP OS Login operator. That identity matches the existing named coding
host custodian in this stack, but treasury access must be reviewed separately.
Merging this PR would make the host resources persistent Terraform intent; it
would not apply them, create a secret version or wallet, change validator
weights, alter Backroom policy, enable a timer, or transfer tokens.

## Exact infrastructure intent

| Setting | Proposed value | Effect after a separately approved apply |
|---|---|---|
| `enable_treasury_host` | `true` | Private Shielded signer VM, dedicated service account and empty signing-key secret |
| `enable_treasury_planner_host` | `true` | Separate private Shielded planner VM, dedicated service account and empty GM API-key secret |
| `treasury_operator_email` | `peyton@omniaura.ai` | Instance-scoped IAP tunnel and OS Admin Login on those two hosts |

The signer service account can access only its signing secret. The planner
service account can access only its GM key secret. Neither secret receives a
version from Terraform. The key-provisioner role is created unbound. No host
startup script installs the treasury runtime or starts the daily timer.

## Protected plan handoff

The existing `Infrastructure plan or apply` workflow checks out `main`, not a
draft PR. **First review and merge this intent, then request a plan against the
exact new main commit.** Select these inputs without any `-target` addresses:

| Workflow input | Value |
|---|---|
| `operation` | `plan` |
| `root` | `gcp-platform` |
| `targets` | empty |
| `screener_fleet_dev_host_enabled` | `false` |
| `screener_fleet_x509_identity_enabled` | `true` |
| `validator_hotkey_admin_phase` | `absent` |
| `validator_hotkey_admin_revision` | empty |
| `plan_sha` and `plan_run_id` | empty for plan |

The protected run seals its binary plan in private GCS and records its main
SHA, run ID, and checksum. Inspect the complete plan for **only** the expected
two VMs, two service accounts, two empty secret containers, two secret-scoped
accessor grants, one unbound key-provisioner role, and four instance-scoped
operator access grants. Check for all unrelated updates, replacements, and
deletions; any unexpected change blocks apply. The protected apply requires a
separate decision on that exact sealed plan. A local `-backend=false` validate
cannot establish the production resource diff.

## Economic proposal and remaining decisions

Propose **25 bps of the released miner vector for maintenance bounties and
25 bps for GM credits**, 50 bps combined. This is a proposal only: the active
Backroom burn revision 8 has `burn_share=1`, so current treasury accrual is
zero. The older maintenance-treasury proposal instead makes treasury and burn
additive full-vector shares. Decide that denominator, the burn-recovery policy,
and the dedicated non-owner treasury hotkey/coldkey before any nonzero shadow
revision or validator weight change. A shadow policy record alone cannot route
emission.

The signer prototype remains **paused and unfunded**. Its allocation amounts,
spending bounds, reviewer names, and later-leg quote are operator supplied;
they do not prove finalized treasury receipts, current Backroom policy, or
independent approval. Identity-bound review, authoritative allocation proof,
fresh signer-side quotes, and an exact pinned host deployment remain required
before any payment. The GM account owner must link the verified public sender
and provide current Billing instructions through GM's account UI. No treasury
key or GM credential should be provisioned as part of host planning.
