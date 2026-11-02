# Isolated Gamma custody

Peyton approved project creation and a fresh delegate migration on 2026-10-05.
The existing collector identities, procurement address, source history, budget
and cursor are retained. Emissions activation is a separate decision.

`sn118-gamma-custody` (1037372464879) is under organization 406520318131 and
uses the existing billing account. Organization owners still inherit access;
this is not a claim of owner immunity, non-extractable keys or on-chain limits.

## Owner bootstrap and protected deployment

As described in ci-bootstrap.md, a workflow cannot create the identity that
authorizes its first run. The organization owner applies the reviewed
`gcp-gamma-custody-bootstrap` configuration once under their own identity,
against a saved exact binary plan. Import adopts the explicitly created project.
Retain the initial state/plan privately, migrate bootstrap state to the new
bucket, and inspect the effective IAM before proceeding. Never give the general
app Terraform or backend identities access to this project to shortcut bootstrap.

Bootstrap owns only the project, API enablement, default-account grant refusal,
separate plan/apply identities and metadata-only secret roles, owner-gated GitHub
federation, private versioned state bucket and Secret Manager read/write audit
configuration. Neither deployment account has payload access/version-add
permissions. The apply identity can administer secret IAM and signer hosts and
therefore remains part of the reviewed custody administrator boundary.

The separate `gcp-gamma-custody` root reuses the reviewed two-host resources and
ceremony source. Its state, network, secrets and service accounts live in the new
project. It never imports, deletes or replaces existing ditto-app-dev resources.
The protected infra workflow uses new project identities only when this exact
root is selected, excludes Platform/Cloudflare secret inputs, refuses targeting,
fences project/resource scope and applies a checksum-verified saved plan from
exact current main. Federation additionally requires the precise repository,
owner, main/manual workflow, protected environment and Peyton actor IDs.

## Phases and rotation

1. Bootstrap both new private shielded hosts with no secret authority.
2. Verify exact source/dependencies/host IAM and absence of uncontrolled default
   readers. A fresh reviewed plan arms each host, removes internet egress and
   grants only own-secret first-version add/list authority.
3. Generate one new delegate in each guest; only public receipts leave. Never
   copy the prior keys. Uncertain generation must be reconciled, never repeated.
4. Apply locked to remove writer permissions, then sealed in a separate apply
   to grant only each numeric project/version-1 read. Re-derive public identities
   without returning seeds. Enable only reviewed Finney TLS egress after sealing.
5. Prepare a new cold signing packet containing exact new policies, new proxy
   grants and revocation of both old delegates. Revalidate chain metadata,
   genesis, current nonces, mortality, call bytes, fees and filters. Never reuse
   a stale packet or repeat existing funding/registration.
6. Before starting new services, quiesce both old timers, reconcile all pending
   operations and transfer immutable journal history, budgets and cursor. The
   previous unused-journal migration cannot handle the completed registration;
   a separately reviewed migration is required. Preserve backups and refuse
   missing, mismatched or concurrently active source history.
7. Verify finalized proxy replacement/revocation and new runtime behavior;
   disable old secret versions and retire old authority only after the reviewed
   transition. No secret deletion or journal reset is part of this provisioning.

Current keys are software-controlled native proxies. A copied Transfer delegate
can submit allowed transfers directly, bypassing software destinations/caps.
Moving existing keys alone does not remove that risk. Offline primary custody
and one-time delegate replacement are distinct from routine hot-wallet purchases.

## Used-journal transfer

`ditto.treasury.collector_migration.manifest` prepares a public manifest for one
private standalone SQLite backup. The deployment operator first stops and
verifies both old timer/service units, reconciles unresolved operations and
exports each database using SQLite backup in DELETE journal mode. A copied main
file with WAL sidecars is not a valid snapshot. Retain the original databases and
private backups; no old database is edited by the migration.

The offline collector coldkey signs
`ditto-collector-custody-migration-v1:<sha256(canonical(manifest))>` alongside the
new collector policy. The manifest binds the full history hash, snapshot hash,
role, old/new policy digests, cursor, row counts and reserved lifetime registration
spend. The helper permits only revision +1, the isolated project/accounts and
two fresh delegate replacements; runtime, collector, caps, start block and
destinations must remain unchanged. Missing or unresolved operations stop the
transition. Approval is of an exact snapshot, not a general budget reset.

After cold signing and finalized proxy replacement, run
`scripts/treasury_migrate_collector_journal.py` with both signed policy files,
their immutable digests, the source snapshot, an exclusive private target and
the signed manifest envelope. Both policies and the manifest are verified with
the collector coldkey. The helper preserves every history row and cursor, changes
only the policy pin and appends the bound migration event. An existing output is
refused even on retry: inspect the retained output/receipt before proceeding.
No key, chain, cloud client, fresh initialization or service activation is part
of this command. Start new services only after verifying new policy, proxy,
delegate, journal and custody boundaries. Keep old timers stopped throughout.
