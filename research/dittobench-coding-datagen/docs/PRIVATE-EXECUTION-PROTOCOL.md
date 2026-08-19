# DittoBench Coding private execution protocol

Status: proposed shadow contract. This document defines interfaces and authority;
it does not implement a service, alter the active DittoBench protocol, reserve a
`bench_version`, or make coding weight-eligible.

Project: [DittoBench Coding](https://github.com/orgs/ditto-assistant/projects/7)

## Decision

DittoBench Coding will run beside the current short-case `/run` protocol under
an independent `coding_contract_version`. Platform owns the complete private
catalog and selects a committed task set after the miner artifact is fixed. An
untrusted miner harness receives only selected visible capsules and scoped
user-memory bundles, indexes and retrieves those memories using its own
implementation, and drives validator-owned repository tools. The validator
freezes the resulting workspace and a fresh, networkless grader determines
patch correctness from executable evidence.

The first private rollout is one task, weight zero. Memory, tool, efficiency,
and any LLM review remain diagnostics until separately calibrated and activated.

## Non-goals

This contract does not:

- change the active `bench_version` or the current tool/memory composite;
- add a coding `CaseScore.kind` to the existing aggregator;
- put private corpus bytes, production identities, hidden tests, or provider
  credentials in public Git or an ordinary image build context;
- claim that an ordinary validator host hides the selected bytes it executes;
- make a miner-reported patch, test result, trace ID, ranking score, or memory-use
  declaration authoritative;
- authorize an unrestricted shell, a host checkout mount, a Docker socket, or
  network access in the coding workspace;
- assign reward to an LLM judge.

## Version and activation

The public practice and private shadow lanes begin with:

```json
{
  "coding_contract_version": 1,
  "weight_eligible": false
}
```

`coding_contract_version` identifies the coding wire, workspace, grading, and
evidence semantics. It is independent of DittoBench `bench_version`; a future
weighted integration maps a reviewed coding contract to a then-unused immutable
benchmark version. No existing benchmark version is reinterpreted.

Unknown JSON fields are ignored for rolling compatibility, but every known
field is strictly typed and bounded. Signed evidence is computed from a
canonical known-field projection, never by reserializing an untrusted object.

## Authority map

| Concern | Authority | Untrusted or diagnostic input |
|---|---|---|
| Complete private catalog | Platform private task service | Validator cache |
| Catalog commitment and selection rule | Platform plus independently verified chain block | Caller seed |
| Selected run manifest | Platform lease, identical for k=3 | Validator task choice |
| Scoped memory indexing and retrieval | Miner harness | Self-reported rankings |
| Model and provider route | Platform ticket grant | Miner model request |
| Repository mutations and tool events | Coding-runner sidecar | Miner final report |
| Frozen patch and final tree | Validator-owned workspace freezer | Miner patch field |
| Correctness | Fresh networkless grader | Miner test claims |
| Score signature | Validator key over typed evidence roots | Advisory details |
| Final queue and weight state | Platform ledger and validator fold | Dashboard state |

Platform retains the whole catalog. A validator receives only the capsules in
one leased run manifest. This prevents bulk distribution, not inspection of a
selected task by that validator's operator. Hiding even selected task bytes
requires a remote signed grader or an attested confidential runtime and is a
separate trust-model decision.

## Catalog commitment and selection

Before any submission can target a corpus release, Platform publishes or
otherwise binds:

```text
corpus_release_id
catalog_merkle_root
selection_derivation_id
coding_contract_version
grader_contract_digest
public verification key
```

The catalog commitment precedes a predetermined future chain height. After the
miner artifact is immutable, Platform derives selection from the canonical
block hash at that height. Validators independently fetch and verify that block
hash; re-hashing a Platform-supplied string is insufficient.

The first shadow run selects exactly one task. Later task counts are determined
from measured runtime, cost, reliability, and score variance rather than fixed
in this contract.

Every scoring validator and every paired champion/challenger comparison receives
the same canonical `CodingRunManifest`:

```json
{
  "schema": "dittobench-coding-run-manifest-v1",
  "coding_contract_version": 1,
  "weight_eligible": false,
  "ticket_id": "opaque-ticket-id",
  "agent_id": "opaque-agent-id",
  "agent_artifact_sha256": "lowercase-sha256",
  "corpus_release_id": "private-coding-corpus-v1",
  "catalog_merkle_root": "lowercase-sha256",
  "selection_derivation_id": "coding-selection-v1",
  "selection_block_number": 1,
  "selection_block_hash": "canonical-chain-hash",
  "task_set_id": "opaque-task-set-id",
  "task_set_manifest_sha256": "lowercase-sha256",
  "tasks": [
    {
      "case_id": "opaque-case-id",
      "variant_id": "opaque-variant-id",
      "profile_capability_id": "opaque-profile-id",
      "visible_bundle_sha256": "lowercase-sha256",
      "base_tree_sha256": "lowercase-sha256",
      "memory_bundle_sha256": "lowercase-sha256",
      "environment_image_digest": "sha256:oci-digest",
      "environment_platform": "linux/amd64",
      "resource_profile_sha256": "lowercase-sha256",
      "grader_bundle_sha256": "lowercase-sha256",
      "grader_image_digest": "sha256:oci-digest",
      "test_manifest_sha256": "lowercase-sha256"
    }
  ]
}
```

URLs are short-lived transport details and do not enter identity. Digests are
the authority.

## Scoped miner memory

The benchmark continues to evaluate miner memory systems. Platform therefore
does not rank production memories for the miner. For each selected task it
delivers one visible, task-scoped user bundle to the harness, which builds its
own index and decides what to retrieve.

The visible memory record contains only information needed for retrieval and
freshness reasoning:

```json
{
  "memory_id": "opaque-memory-id",
  "repository_capability_id": "opaque-repository-id",
  "fact_group_id": "opaque-fact-group-id",
  "scope": "module",
  "type": "previous_bug_fix",
  "content": "Partial input must remain buffered between calls.",
  "valid_from_epoch": "repository-epoch-2",
  "valid_until_epoch": null,
  "supersedes": [],
  "confidence_micros": 960000
}
```

Real source issue IDs, public URLs, commit hashes, target-task mappings, policy
labels, gold patches, and curator notes remain in the private view. Opaque IDs
reduce direct lookup but do not make recognizable open-source code anonymous.

The miner may inspect every record in its assigned bundle. It never receives
another profile, an unselected task bundle, hidden policy labels, or the full
catalog. Cross-user and cross-ticket access must fail closed.

Miner-internal search scores and tool logs are diagnostic: a miner controls its
own database and can scan or inject the bundle without calling a named memory
tool. Authoritative memory evidence is therefore limited to:

- the exact scoped seed manifest;
- bounded model inputs observed by the trusted relay;
- known memory IDs and content digests appearing in those inputs;
- cross-user, stale/conflicting, irrelevant-volume, and unknown-ID violations;
- executable patch outcomes across independently isolated V0-V4 variants.

The five variant conditions are:

```text
V0 no memory
V1 relevant memory
V2 irrelevant memory
V3 stale or conflicting memory
V4 relevant memory plus an explicit current-user override
```

Only one variant of a base task is exposed in one task session. Each variant
uses a fresh harness state and workspace unless it belongs to a separately
versioned sequential-memory track.

## Harness protocol

Coding uses separate endpoints so long-running, stateful repository work cannot
silently change the existing `/seed` and `/run` contracts.

### `GET /coding/health`

The harness returns supported coding contracts and capabilities. Health proves
readiness only, not correctness or eligibility.

```json
{
  "status": "ok",
  "supported_coding_contract_versions": [1],
  "capabilities": [
    "scoped_memory_seed_v1",
    "coding_runner_tools_v1",
    "case_scoped_inference_v1"
  ]
}
```

### `POST /coding/seed`

The scorer sends one profile capability and its visible memory records. Calls
are idempotent for `(ticket_id, case_id, profile_capability_id,
memory_bundle_sha256)` and reject any identity change.

```json
{
  "coding_contract_version": 1,
  "ticket_id": "opaque-ticket-id",
  "case_id": "opaque-case-id",
  "profile_capability_id": "opaque-profile-id",
  "memory_bundle_sha256": "lowercase-sha256",
  "memories": []
}
```

The response reports counts and the verified bundle digest. It does not report
hidden policy or grader state.

### `POST /coding/run`

The scorer gives the harness visible issue text and expiring, task-scoped
capabilities. The active user and task are lease-derived and cannot be selected
by the caller.

```json
{
  "coding_contract_version": 1,
  "ticket_id": "opaque-ticket-id",
  "case_id": "opaque-case-id",
  "profile_capability_id": "opaque-profile-id",
  "visible_bundle_sha256": "lowercase-sha256",
  "issue": {
    "title": "Correct streaming boundary handling",
    "description": "The parser drops an incomplete trailing sequence.",
    "constraints": ["Do not add a runtime dependency."]
  },
  "workspace_capability_url": "http://coding-runner.invalid/capability",
  "inference_base_url": "http://ticket-broker.invalid/v1/inference",
  "budgets": {
    "model_input_tokens": 200000,
    "model_output_tokens": 30000,
    "workspace_tool_calls": 150,
    "wall_time_seconds": 1800
  }
}
```

The harness response is advisory and bounded:

```json
{
  "case_id": "opaque-case-id",
  "final_report": {
    "summary": "Preserved incomplete input until the next call.",
    "remaining_risks": []
  }
}
```

It does not carry the authoritative patch, test evidence, memory/tool trace IDs,
or score. The validator-owned runner and freezer produce those values.

## Locked solver model

The initial shadow candidate is:

```json
{
  "model": "openai/gpt-5.6-luna",
  "api": "openai-compatible-chat-completions",
  "reasoning": {"effort": "medium"}
}
```

The Platform inference grant binds the exact model, provider route, route/profile
revision, reasoning contract, request/token/cost budgets, output limit, and
retry policy. The OpenRouter credential remains in Platform secrets. The miner
receives only a source-bound ticket capability and placeholder compatibility
keys.

Scored routing disables unbound provider fallback. Requests require supported
parameters, deny data collection, and require a reviewed zero-data-retention
endpoint. Model/provider identity and usage returned by the upstream are
verified and recorded. A provider or model-revision change requires a new route
profile and calibration; an alias alone is not treated as an immutable model
snapshot.

The existing Chat Completions broker is the compatibility baseline. A Responses
API transport requires its own reviewed relay contract and is not implied by
the model's feature set.

## Coding-runner tools

The coding-runner sidecar owns a fresh writable workspace and exposes typed,
task-scoped operations:

```text
repo.list_tree
repo.search
repo.read_file
repo.read_range
repo.apply_patch
tests.run(command_id)
build.run(command_id)
git.status
git.diff
```

Test/build command IDs resolve through the signed task manifest. There is no
general `sh -c` or caller-selected executable. Each operation enforces safe
workspace-relative paths, input/output bounds, deadlines, process limits, a
scrubbed environment, and network denial.

The runner cannot access hidden tests, grader files, validator credentials,
provider keys, host mounts, Docker, metadata services, sibling containers, or
another ticket's workspace. Its event sequence is monotonic and hash-chained.

## Authoring and grading lifecycle

1. Verify the signed run manifest, chain block, corpus root, and every selected
   capsule digest.
2. Materialize one visible base without `.git`, remotes, hooks, credentials,
   hidden tests, or network-dependent installation.
3. Start a fresh miner harness and seed only the assigned memory bundle.
4. Start the bounded coding-runner workspace and ticket-scoped Luna relay.
5. Execute `/coding/run`; record authoritative runner and relay events.
6. Stop authoring and revoke every task capability.
7. Freeze added, modified, deleted, mode-changed, and untracked paths. Reject
   traversal, symlinks, special files, protected paths, or count/byte overflow.
8. Bind the base tree, frozen patch, changed-path root, and final tree digests.
9. Destroy the authoring environment.
10. Apply the frozen patch to a pristine base in a fresh networkless grader.
11. Inject the digest-pinned private grader bundle only after the patch is fixed.
12. Run build, fail-to-pass, pass-to-pass, hidden, adversarial, and integrity
    groups, then emit typed canonical evidence.

The candidate may not modify visible tests, task runners, dependency policy, or
any protected path unless a task manifest explicitly allows that path. The
grader hashes protected material before and after execution.

## Correctness and diagnostics

One valid attempted task has:

```text
repair_score_micros = 1_000_000
```

only when the patch applies, the declared build succeeds, all mandatory test
groups pass, protected material is intact, and no resource or integrity rule is
violated. Every other valid attempted repair scores zero. Partial test counts,
patch size, similarity, prose, timing, memory behavior, and tool behavior are
diagnostics and cannot rescue a failed patch.

The candidate composite remains a shadow report only:

```text
candidate_task_score = R * (1 + 0.5*M + 0.5*T)
candidate_normalized = candidate_task_score / 2
```

where `R` is binary and `M` and `T` are deterministic diagnostics. The formula
has no reward or weight effect in contract v1. Any LLM memory/engineering review
has weight zero; it may identify curation defects but never decides correctness,
waives integrity, or supplies partial repair credit.

## Failure domains

Terminal outcomes are mutually exclusive:

- `repair_failure`: candidate build/test failure, protected-path change,
  dependency-policy violation, or calibrated candidate timeout/OOM;
- `validator_infrastructure`: capsule transport, daemon, host, runner, broker,
  or grader startup failed before candidate execution became authoritative;
- `task_invalid`: base/gold validation, environment, test, or curator metadata
  is defective; quarantine the task and do not charge the miner;
- `integrity_incident`: digest, signature, capability, cross-user, or protected
  material mismatch; fail closed and retain bounded evidence.

Retry policy is bounded by the ticket lease and terminal domain. A repair
failure is not retried as infrastructure, and an invalid task is not scored as a
miner failure.

## Signed evidence

Each `CodingTaskEvidence` binds:

```text
coding_contract_version and weight_eligible
ticket, agent artifact, corpus release, task set, case, and variant identities
visible bundle, base tree, memory bundle, resource profile, and environment digests
model, provider route/profile, reasoning, prompt/tool-schema, usage, and retry identity
authoring event root and bounded transcript digest
frozen patch, final tree, changed-path root, and protected-path verdict
grader bundle/image/test-manifest and before/after integrity digests
exact build and test counts
terminal domain and integer repair_score_micros
```

`CodingRunEvidence` binds the sorted task evidence roots, task-set manifest,
binary task vector, pass/fail/invalid counts, and integer repair mean. Its digest
must become a first-class score-signature field before any weighted activation;
placing it only in advisory report details is insufficient.

## Retirement

While a corpus release can affect a private shadow or future weighted run,
public surfaces expose only schemas, commitments, bounded redacted telemetry,
and retired evidence approved for release. Active repository bundles, task
manifests, memory mappings, policies, hidden tests, and reference fixes remain
private. A retired release may be published only after an explicit Platform
retirement transition and leakage review.

## Implementation sequence

This ADR enables, but does not combine, the following PRs:

1. task/memory/run/evidence schemas and public vectors;
2. coding-runner workspace tools and freezer;
3. pristine deterministic grader;
4. Luna Platform route and ticket-scoped broker capability;
5. scoped miner-memory seed and deterministic policy diagnostics;
6. private catalog service and post-commit shared-manifest selector;
7. validator/Platform shadow leases and evidence persistence;
8. one-task calibration against no-memory, always-use, random-memory,
   selective-memory, no-op, test-tampering, known-bad, and valid-repair probes.

No step may activate coding weights. Activation requires a separate immutable
benchmark-version proposal backed by reproducibility, runtime, cost, reliability,
security, and score-separation evidence.

## Rejected alternatives

- **Modify existing `/run`:** rejected because coding is stateful and long-lived,
  while current tool/memory cases have frozen compatibility semantics.
- **Give every validator the full corpus:** rejected because it accelerates bulk
  disclosure and cannot satisfy validator blindness.
- **Validator-owned memory ranking:** rejected because it stops evaluating the
  miner's memory implementation.
- **Trust miner patch or trace fields:** rejected because the submitting process
  controls them.
- **Grade in the authoring workspace:** rejected because hidden material and
  mutable tests would share a trust boundary with candidate code.
- **Use an LLM correctness judge:** rejected because correctness must be
  reproducible from executable evidence.
- **Expose a general shell:** rejected because manifest-addressed build/test
  commands and typed file tools cover the benchmark without an open command
  execution surface.
