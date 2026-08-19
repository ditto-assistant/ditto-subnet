# Coding repair shadow contract

Status: research contract, weight zero. This document does not activate a
coding score or reserve a production `bench_version`.

Project: [DittoBench Coding](https://github.com/orgs/ditto-assistant/projects/7)

## Benchmark definition

DittoBench Coding evaluates whether an AI coding agent can produce a correct
repository-level patch while selectively using valid persistent experience,
rejecting stale or conflicting memory, respecting current user instructions,
and using engineering tools efficiently.

The authority order is immutable:

1. current user instruction and explicit constraints;
2. current repository state and executable tests;
3. current architecture and dependency constraints;
4. verified, in-scope persistent memory;
5. old, uncertain, or weakly related memory.

A request for a new method rejects only memories that conflict with that
request. Still-valid compatibility constraints, edge cases, test knowledge,
and failure lessons remain usable.

## Trust statement

The miner process may receive only one opaque task envelope and visible
workspace at a time. Grader material is withheld until the authoring process is
stopped and its patch is frozen.

An ordinary validator host can inspect any bytes it executes. Per-ticket
delivery prevents bulk distribution but does not make selected capsules secret
from the host owner. A stronger statement requires a remote signed grader or an
attested confidential runtime.

## Assignment identity

A future `PrivateTaskLease` must bind:

```text
coding_contract_version
weight_eligible = false
agent_id and agent_artifact_sha256
ticket_id, validator_hotkey, deadline
corpus_release_id and corpus_merkle_root
selection_derivation_id
selection_block_number and independently verified block_hash
task_set_id and task_set_manifest_sha256
```

Every selected task must bind opaque case ID, visible-bundle digest, base-tree
digest, task environment image digest and platform, memory-view digest,
resource-profile digest, grader-bundle digest, grader image digest, and test
manifest digest. URLs are transport details; digests are identities.

The weighted design must not use the existing Platform fallback to a local
CSPRNG. Corpus commitment precedes selection, selection uses a predetermined
future block, and validators verify the canonical block hash independently.

## Runtime phases

1. Materialize one visible task in a fresh repository tree with no `.git`,
   remotes, hooks, network, credentials, or host mounts.
2. Run the screened miner image with read-only root and one bounded writable
   workspace. The active user is lease-derived and cannot be caller-selected.
3. Stop authoring. Capture modified, deleted, mode-changed, and untracked files;
   reject unsafe paths, symlinks, device files, count/byte overflow, and protected
   test/runner changes. Bind patch and final-tree digests.
4. Destroy the authoring container.
5. Apply the frozen patch to a pristine base in a fresh networkless grader.
   Inject the digest-pinned grader bundle only at this phase.
6. Emit canonical typed evidence. Never accept miner-reported test results.

## Task result

One valid task scores exactly `1_000_000` micros only when the patch applies,
the build succeeds, all fail-to-pass, protected regression, hidden, and
adversarial groups pass, and no integrity/resource/policy violation occurred.
Every other valid attempted repair scores zero. Partial counts are diagnostic;
patch similarity, patch size, prose, and exact trajectory never score.

The mandatory correctness gates are therefore:

- every fail-to-pass test passes;
- every protected pass-to-pass regression remains passing;
- hidden and adversarial edge cases pass;
- the declared build succeeds in the digest-pinned environment;
- protected tests, judge files, runners, and dependency policy are intact;
- the candidate did not skip tests, hard-code hidden outputs, escape its
  workspace, or exceed its resource contract.

Memory-policy and tool-use measurements remain shadow diagnostics in this
contract. A future weighted formula requires calibration and a new, separately
reviewed activation; neither diagnostic can rescue an incorrect patch.

Terminal domains are disjoint:

- `repair_failure`: candidate compile/test failure, calibrated timeout/OOM,
  protected-path change, or dependency/network violation.
- `validator_infrastructure`: capsule transport, host, daemon, or grader startup
  failed before candidate execution became authoritative.
- `task_invalid`: gold/base validation or curator metadata failed; quarantine
  the task and do not charge the miner.
- `integrity_incident`: digest/signature mismatch; fail closed without an
  unbounded retry loop.

## Signed evidence root

Per-task evidence binds the task capsule, agent artifact, base tree, frozen
patch, final tree, changed-path root, memory view and observed memory trace,
authoring transcript, resource profile, grader image/contract, hidden-test
manifest, test-integrity before/after digests, exact pass counts, terminal
domain, and integer repair score.

The run root binds the sorted per-task evidence root, task-set manifest, binary
task vector, pass/fail/invalid counts, and `repair_mean_micros`. Its digest must
become a first-class score-signature field before activation; it must not live
only in advisory `ScoreReport.details`.

## Quorum and rollout

All k=3 validators grade the same task-set manifest. Champion and challenger use
the same task capsules, memory views, resource profile, inference grant, and
grader digests so comparison is paired task-by-task.

Rollout ladder:

1. public 3x3 local practice;
2. one-task private shadow, weight zero;
3. measured two-to-three-task shadow blocks;
4. no-memory, correct-memory, stale-memory, hardcoded-reference, no-op,
   test-deletion, hidden-test-inspection, timeout/OOM, and independent-valid-fix
   calibration;
5. only then allocate the next unused immutable production `bench_version`.
