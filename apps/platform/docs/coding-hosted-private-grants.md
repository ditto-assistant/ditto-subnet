# Hosted private task grants and patch freeze

Status: injected Platform-only primitives; no worker, KMS adapter, private HTTP
delivery, production publication or scoring activation. Validator control still
returns opaque signed status only. No credential or grant is sent to validators.

## Selection and ownership

`HostedTaskSelection` binds one isolated arm to its evaluation/attempt IDs,
registered release, artifact, private schedule commitment, catalog index and
maximum patch size. Its canonical digest must equal the selection commitment
already approved in the immutable hosted assignment. `bind_hosted_private_task`
cannot create or approve that assignment. It creates one private task row and
two distinct grant IDs before the start boundary; replays preserve those IDs.

This representation is not a sampling algorithm. A multi-arm experiment needs
distinct hosted assignments/attempts under an approved common schedule. It does
not decide between the proposed 30-task sampling designs or authorize competitive
use. The scheduler must validate release eligibility, task selection and the
runtime policy's patch limit before approving the assignment. Neither the task
selection preimage nor its catalog index belongs in a validator-facing response.

## Access lifecycle

`HostedPrivateGrantStore` implements the encrypted retriever's grant-store
interface. Trusted runtime configuration fixes its worker identity and authoring
or grading audience. Every check opens a fresh database transaction and validates
release lifecycle, screened artifact, durable worker ownership, assignment expiry,
selection identity and task phase. No grant is active before committed start.
Roles are derived by Platform, never supplied by a validator or candidate.

- Started, unfrozen: only authoring roles, excluding the hidden grader.
- Frozen: authoring object access is revoked; only grading roles are active,
  excluding issue/memory and carrying the committed frozen patch digest.
- Closed, expired, retired/quarantined or artifact drift: neither grant is active.

`freeze_hosted_private_patch` hashes the bounded actual patch bytes, not a claimed
digest. It commits hash, byte count and database time once. A replay of identical
bytes returns the original identity without a new freeze; different bytes fail.
The transaction revokes authoring object access atomically with freeze. Rollback
preserves the previous phase. These functions must commit before the caller
starts the next phase.

The trusted supervisor MUST quiesce the candidate and revoke its inference relay
before freezing, preserve the exact frozen bytes, and use a distinct pristine
grading process. This database marker does not prove process termination, does
not parse/validate the patch, and does not itself retain encrypted patch evidence.
Those runtime and evidence adapters remain required before a live hosted canary.
Revocation stops future retrieval; it cannot erase bytes already delivered to a
trusted process.

`close_hosted_private_task` permanently removes both grants and remains callable
by the owning worker after expiry, release retirement or artifact drift. It is
not terminal result finalization, a grade, or an authorization to rerun a task.

## Persistence and verification

Normal lock order is release, agent, assignment, private task. Access removal
takes assignment then private task only and never requests a release lock later.
No provider download or unwrap happens while the grant-store transaction holds
those locks. The retriever rechecks the store after download and before returning
plaintext, so a freeze during download blocks unwrap and plaintext delivery.

PostgreSQL guards prevent selection/grant changes, deletion, incomplete NULL
phase pairs, freezing before start, replacement of a frozen patch, and reopening
a closed task. Reads also reconstruct and verify the canonical selection instead
of trusting its stored digest alone. No plaintext patch or dataset is stored in
the new table; its private selection metadata stays inside Platform.

Tests exercise the real migrated PostgreSQL ledger, concurrent freezes, rollback,
cross-worker/audience denial, retirement, direct SQL guards, and the encrypted
retriever with synthetic AES-GCM objects and a test unwrap implementation. These
are local security and integration evidence, not live Hippius or production key
custody proof. Every assignment remains shadow-only and weight-ineligible.
