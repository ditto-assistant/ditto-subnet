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

The implementation ownership is not "validator code": `services/dittobench-api`
owns the versioned coding-runner sidecar, freezer, grader, sandbox/egress
policy, and canonical runtime evidence. `ditto/validator` only orchestrates
those digest-pinned ephemeral services, independently verifies evidence, and
signs its root. Platform owns durable catalog, lease, exposure, and evidence
state. No coding task, workspace, transcript, or retry state is durable on a
validator host.

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
- authorize general miner-harness egress: the harness network is default-deny
  and may reach only its case-scoped runner capability and inference broker;
- assign reward to an LLM judge.

## Version and activation

The public practice and private shadow lanes begin with:

```json
{
  "bench_family": "coding",
  "coding_contract_version": 1,
  "weight_eligible": false
}
```

`coding_contract_version` identifies the coding wire, workspace, grading, and
evidence semantics. It is independent of DittoBench `bench_version`; a future
weighted integration maps a reviewed coding contract to a then-unused immutable
benchmark version. No existing benchmark version is reinterpreted.

Contract v1 and every `*-v1` evidence schema are permanently shadow-only.
Weighted activation requires `coding_contract_version=2`, a reviewed
`weight_eligible=true` schema, and an explicit `bench_family="coding"`
discriminator. Existing open-ended version floors remain open-ended only within
`bench_family="memory"`; LongMem confirmation, memory scoregates, efficiency,
and receipt contracts must not match a coding-family version by numeric floor.
The activation audit must make every family switch exhaustive rather than
restoring enumerated version pins.

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
| Repository mutations and tool events | DittoBench coding-runner sidecar | Miner final report |
| Frozen patch and final tree | DittoBench workspace freezer | Miner patch field |
| Correctness | DittoBench fresh networkless grader | Miner test claims |
| Harness network policy | DittoBench sandbox and egress proxy | Miner destination request |
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
selection_chain_genesis_hash
coding_contract_version
grader_contract_digest
inference_grant_digest
public verification key
```

The catalog commitment precedes every candidate artifact. After one miner
artifact is immutable, Platform assigns that artifact a fresh predetermined
future chain height. Selection is derived per submission from a
domain-separated tuple containing the corpus release/root, immutable agent
artifact SHA-256, coding run ID, selection derivation ID, and independently
fetched canonical block hash at that height. Re-hashing a Platform-supplied
string is insufficient. Different artifact digests therefore receive different
selections; all k=3 validators for one artifact receive the identical resulting
manifest.

The first shadow run selects exactly one task. Later task counts are determined
from measured runtime, cost, reliability, and score variance rather than fixed
in this contract.

The public shadow selector core implements the deterministic verification half
of this rule. It accepts a content-addressed future-height assignment, fetches
finalized genesis and that finalized height independently, derives an
artifact-bound affine permutation over catalog indexes, and verifies a
position-bound Merkle proof before producing the shared run and exposure
projections. Platform's append-only assignment ledger commits a finalized
anchor plus fixed delay before the future hash is available. Platform's shadow
issuer waits for the selected height to become finalized, proves the assignment
row's immutable database insertion time predates its timestamp, then atomically
inserts the selected run, irreversible exposures, and assignment-to-run link.

Platform's private catalog transport resolves one selected record at a time
from a separately credentialed, non-public S3-compatible store. Contract v1
uses the fixed content-addressed object key:

```text
coding-catalog/v1/<catalog-commitment-sha256>/records/<six-digit-index>.json
```

The object is a bounded `dittobench-coding-private-catalog-record-v1` envelope:

```json
{
  "schema": "dittobench-coding-private-catalog-record-v1",
  "catalog_commitment_sha256": "lowercase-sha256",
  "task_version": {},
  "membership_proof": {},
  "issue": {},
  "runtime_policy": {},
  "budgets": {}
}
```

Callers cannot provide a bucket, prefix, URL, or arbitrary object key, and the
source has no catalog-list operation. The loader rejects duplicate fields,
non-finite JSON, excessive nesting, oversized bodies, task/proof digest drift,
wrong release/index/count/root, issue/runtime-policy/budget digest drift, and
invalid position-bound membership. Unknown fields remain non-authoritative for
rolling compatibility. Issue text retains tabs, newlines, and carriage returns,
while other control characters are rejected so the bounded record cannot
expand beyond its transport ceiling during JSON encoding. Missing objects,
store failures, and timeouts are retryable transport errors; malformed or
non-member objects are non-retryable control-plane integrity errors. The
separate miner-upload object store is never a fallback.

The private catalog leaf also commits the repository epoch plus canonical issue,
model-visible runtime-policy, and model/tool/wall-budget digests. These fields
are omitted from the public run manifest but retained in the private task-set
identity, preventing task transport from changing user constraints,
stale-memory interpretation, or resource fairness without changing the selected
root.

Every scoring validator and every paired champion/challenger comparison receives
the same canonical `CodingRunManifest`:

```json
{
  "schema": "dittobench-coding-run-manifest-v1",
  "bench_family": "coding",
  "coding_contract_version": 1,
  "weight_eligible": false,
  "coding_run_id": "opaque-shared-run-id",
  "agent_id": "opaque-agent-id",
  "agent_artifact_sha256": "lowercase-sha256",
  "corpus_release_id": "private-coding-corpus-v1",
  "catalog_merkle_root": "lowercase-sha256",
  "selection_derivation_id": "coding-selection-v1",
  "selection_chain_genesis_hash": "canonical-chain-genesis-hash",
  "selection_block_number": 1,
  "selection_block_hash": "canonical-chain-hash",
  "inference_grant_sha256": "lowercase-sha256",
  "grader_contract_sha256": "lowercase-sha256",
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
      "grader_platform": "linux/amd64",
      "test_manifest_sha256": "lowercase-sha256",
      "grader_plan_sha256": "lowercase-sha256"
    }
  ]
}
```

URLs are short-lived transport details and do not enter identity. Digests are
the authority.

Platform derives artifact transport capabilities only after reconstructing the
ticket-bound task lease. Contract v1 permits exactly four digest-selected GET
capabilities: visible bundle, scoped memory bundle, resource profile, and
private grader bundle. Their storage keys are fixed as
`coding-artifacts/v1/<artifact-kind>/sha256/<digest>`; callers never supply a
key, prefix, bucket, URL, or kind. Before signing, Platform verifies positive
bounded object size plus matching `sha256` and `artifact-kind` metadata. Each
URL expires at the smaller of the configured short ceiling and the remaining
ticket lifetime; Platform validates that bound against the signer's encoded S3
expiry. A capability is never persisted or logged and is not part of signed
identity. OCI environment and grader images remain digest-pinned image
identities rather than presigned objects.

Object metadata is only a signing preflight. The consuming validator service
must stream within the declared bound and verify the full downloaded SHA-256
before extracting or using bytes; the artifact namespace also requires
immutable-write storage policy.

The complete capability set stays server-internal. Delivery projects each URL
only to its trusted consumer: visible materializer, memory seed projector,
resource-enforcing supervisor, or protected grader. The grader capability is
not released until authoring is frozen, and no artifact bearer URL enters the
miner harness or model context.

The trusted DittoBench artifact fetcher independently revalidates the ticket
deadline, audience/kind mapping, fixed content-addressed path, signed expiry,
size, and full downloaded SHA-256. It disables redirects, ambient proxies, and
transparent decoding, applies dial-time SSRF protection, stages mode-`0600`
bytes, deletes partial or closed files, and never includes the signed URL in an
error. It returns opaque readers only: archive extraction and interpretation
remain with the runner, seed projector, resource supervisor, or grader.
DNS validation is request-bounded, capability logging is redacted, and the
server-internal consumer type fails closed rather than JSON-serializing a
bearer URL.

The single-capability delivery wire is
`dittobench-coding-artifact-capability-v1`, frozen by the public synthetic
Python/Go vector `coding_artifact_capability_v1.json`. Authoring permits only
visible, memory, and resource artifacts; grading permits only visible,
resource, and grader artifacts. The vector URL is transport-only, excluded
from canonical identity, and never consumed by Rust or miner-facing code.

The authoring delivery request is signed over validator hotkey, ticket UUID,
one-use nonce, and UTC request time. Platform authenticates and authorizes the
ticket before private-catalog access, then returns the visible issue and
runtime/budget material plus exactly the visible, memory, and resource
capabilities. Its authoring-only minter does not inspect or sign the grader
object, and grader capability is structurally absent. The route remains unused
until a separate scheduler and validator-orchestration review.

After runner and relay revocation, the validator signs the complete canonical
authoring evidence plus content-addressed transcript and frozen-submission
references. Platform persists one immutable freeze per ticket; exact replay is
idempotent and drift conflicts. This record is an audit and phase authority,
not independent proof of validator-local process termination, and it releases
no grader capability by itself.

The grading delivery request is separately signed over validator, agent, run,
ticket, freeze, authoring-evidence digest, one-use nonce, and UTC request time.
Platform requires a nonempty immutable freeze with complete model usage, a
changed path, intact protected paths, an active ticket, and no terminal result
before private-catalog access. It returns the frozen patch identity plus exactly
the visible, resource, and grader capabilities. Its grading-only minter never
checks or signs memory, and Platform reauthorizes the freeze after URL minting
along with the active ticket and exact artifact certification before returning
the `no-store` response. This transport route remains unused until the
validator supervisor and pristine grader are wired in a separate review.
The validator client compares the response patch identity to its locally frozen
patch before accepting any grading capability.

After pristine grading and canonical aggregation, the validator signs the
terminal run-evidence digest together with the exact agent, run, ticket,
deadline, artifact, screened image, and benchmark authority. Its bounded client
first replays the aggregate against the run manifest and per-task evidence,
refuses redirects, and accepts Platform's idempotent response only when the
agent, run, ticket, and coding-run identities match. This transport exists for
the future shadow worker and does not write the ordinary score ledger.

`coding_run_id` and the manifest are shared across k=3. Validator-specific
ticket IDs, deadlines, hotkeys, and transport capabilities remain in each lease
envelope and signed validator evidence; they cannot make the selected task-set
manifest differ between validators.

`grader_plan_sha256` is the canonical digest of the public
`dittobench-coding-grader-plan-v1` projection demonstrated in the shared
contract vectors. It binds the selected case/variant, visible capsule and base
tree, grader identities, execution timeout, exact commands/counts, and fail-fast
order. The execution timeout starts only after setup and pristine
materialization; parent or lease deadlines remain validator infrastructure.

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

Private corpus v1 must therefore use genuine post-commit seeded semantic
generation over private/post-cutoff source or transformations whose sampled
specification derives identifiers, values, structure, bug site, and grader
expectations. Recognizable historical OSS issues are calibration material, not
secret production tasks. Related work includes SWE-bench Pro's held-out sets,
SWE-Bench-CL and MemoryAgentBench's memory evaluation, and R2E-Gym's procedural
task generation. DittoBench Coding's intended differentiator is V0-V4 selective
memory use and rejection under adversarial subnet economics, not opaque IDs.

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

V3 is not purely a metadata filter. Some calibration cases may expose an
explicit expired epoch, but a measured fraction must have non-decisive validity
metadata and require comparing memory content with current repository state or
current-user instructions. Required constraints are sampled per instance and
must not repeat hidden-test literals or patch-equivalent answers. Variant labels
must not be inferable solely from bundle metadata.

## Harness protocol

Coding uses separate endpoints so long-running, stateful repository work cannot
silently change the existing `/seed` and `/run` contracts.

The harness container runs behind a coding-specific fail-closed egress proxy
and host firewall. Its only destinations are the exact case-bound
`workspace_capability_url` and ticket-bound `inference_base_url`; direct DNS,
IP, CONNECT, metadata-service, or arbitrary Internet access is denied. The
proxy binds the ticket/source identity and records attempted destinations. Any
miner-attributable bypass or disallowed egress attempt is `candidate_integrity`.

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

### Active coding capability certification

`/coding/health` is capability discovery only. A trusted certifier must bind
the exact screened artifact and run one content-addressed public canary through
`health -> seed -> identical seed replay -> run -> revoke -> freeze -> pristine
grade`. Before certification is issued, the exact canonical tool transcript
must be durably stored and prove the required read/edit/test/diff sequence, and
the replayable frozen submission must enter a validator-local durable outbox.
The trusted relay must also finalize complete evidence for the manifest-bound
Luna grant. Certification expires and is invalidated by any artifact change.

An absent endpoint or missing capability means `coding_supported=false`: the
miner remains in the existing tool-and-memory pipeline and does not receive an
artificial zero folded into that score. A miner that advertises coding enters
the coding pipeline only after the active canary resolves. Validator
infrastructure, invalid task material, and control-plane integrity never count
as miner certification failures. In particular, a transport failure before the
first authoritative workspace or relay event is infrastructure; after an event,
the scorer freezes and grades without granting a clean retry.

Contract v1 certification is still `weight_eligible=false`. Platform
persistence, rolling core qualification, a separate shadow coding ledger, and
any coding emissions allocation require later reviewed contracts.

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

The scorer—not the untrusted harness—owns `wall_time_seconds` and the transport
deadline. Contract v1 uses one bounded synchronous/long-poll POST, while runner
and relay events are committed independently. After the first authoritative
tool or relay event, response loss, client disconnect, or deadline expiry never
earns a clean retry: the scorer revokes capabilities, freezes, and grades the
workspace exactly as if the advisory response arrived. `final_report` is simply
absent from evidence. Only a transport failure before any candidate execution
became authoritative is `validator_infrastructure`.

```json
{
  "coding_contract_version": 1,
  "ticket_id": "opaque-ticket-id",
  "case_id": "opaque-case-id",
  "profile_capability_id": "opaque-profile-id",
  "repository_epoch": "opaque-repository-epoch",
  "visible_bundle_sha256": "lowercase-sha256",
  "issue": {
    "title": "Correct streaming boundary handling",
    "description": "The parser drops an incomplete trailing sequence.",
    "constraints": ["Do not add a runtime dependency."]
  },
  "runtime_policy": {
    "editable_paths": ["src/parser.py"],
    "test_command_ids": ["visible-parser-tests"],
    "build_command_ids": ["typecheck-parser"]
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

`repository_epoch` is the opaque current snapshot identity used to interpret
visible memories' `valid_from_epoch` and `valid_until_epoch`. It is not a public
commit hash and must match the selected visible bundle.

`runtime_policy` tells the model which visible paths and opaque command IDs are
available. It is bounded advisory context, not an authorization decision: the
signed task manifest and validator-owned runner independently enforce the
actual path and command allowlists.

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

Canonical model evidence retains the expected model/route/grant identity even
when the miner never invokes the provider. `usage_status=not_invoked` requires
zero requests, tokens, retries, cost, and no receipt root. Invoked and
provider-failure forms require a provider receipt-set root, declare
`fallback_used=false`, and identify provider-receipt USD cost as the accounting
source. This makes no-op, hardcoded, and pre-model failures representable
without fabricating provider use.

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
repo.create_file
repo.delete_file
tests.run(command_id)
build.run(command_id)
git.status
git.diff
```

Test/build command IDs resolve through the signed task manifest. There is no
general `sh -c` or caller-selected executable. Each operation enforces safe
workspace-relative paths, input/output bounds, deadlines, process limits, a
scrubbed environment, and network denial.

Create/delete are reserved contract-v1 names but remain disabled by the public
practice runtime policy. A private task must explicitly allow each created or
deleted path. Rename is represented as one allowed delete plus one allowed
create and is frozen as such.

The runner cannot access hidden tests, grader files, validator credentials,
provider keys, host mounts, Docker, metadata services, sibling containers, or
another ticket's workspace. Its event sequence is monotonic and hash-chained.
Calls are serial-only. `call_id` is an idempotency key: retrying the exact same
typed request returns the cached response and sequence verbatim, while reusing
an ID with different bytes fails as candidate integrity. A lost HTTP response
is retried with the same ID; clients never guess or skip the authoritative
sequence.

## Authoring and grading lifecycle

The validator's shadow attempt coordinator encodes this order through injected
Platform and runtime ports. Runtime exceptions trigger shielded phase cleanup;
normal authoring and grading outcomes are accepted only after capability
revocation and environment destruction are attested. The coordinator remains
unwired and covers the complete gradeable path only; terminal failure
classification remains a later worker layer.

1. Verify the signed run manifest, chain block, corpus root, and every selected
   capsule digest.
2. Materialize one visible base without `.git`, remotes, hooks, credentials,
   hidden tests, or network-dependent installation.
3. Start a fresh miner harness and seed only the assigned memory bundle.
4. Start the bounded coding-runner workspace and ticket-scoped Luna relay.
5. Execute `/coding/run`; record authoritative runner and relay events.
6. Stop authoring and revoke every task capability.
7. Freeze canonical UTF-8 added, modified, and deleted file transitions. New
   files use mode `0644`; modified and deleted files preserve their base mode.
   Reject traversal, mode changes, symlinks, special files, protected paths,
   or count/byte overflow.
8. Bind the base tree, frozen patch, changed-path root, and final tree digests.
9. Destroy the authoring environment.
10. Request the grading lease for the exact stored freeze and independently
    verify the visible, resource, and grader artifact bytes against their
    content digests. Do not request or deliver memory in this phase.
11. Apply the frozen patch to a pristine base in a fresh networkless grader.
12. Inject the digest-pinned private grader bundle only after the patch is fixed.
13. Start the plan-bound candidate execution timeout, then run build,
    fail-to-pass, pass-to-pass, hidden, adversarial, and integrity
    in that fail-fast order, then emit typed canonical evidence. Evidence keeps
    the five test groups in canonical lexical order.

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

## Terminal domains

Terminal outcomes are mutually exclusive:

- `resolved`: authoritative authoring and fresh-grader evidence proves the
  complete repair; this is the only domain with
  `repair_score_micros = 1_000_000`;
- `repair_failure`: candidate build/test failure, protected-path change,
  dependency-policy violation, or calibrated candidate timeout/OOM;
- `validator_infrastructure`: capsule transport, daemon, host, runner, broker,
  or grader startup failed before candidate execution became authoritative;
- `task_invalid`: base/gold validation, environment, test, or curator metadata
  is defective; quarantine the task and do not charge the miner;
- `candidate_integrity`: authoritative authoring proves miner-attributable
  protected-path, cross-user, capability, egress, network, or workspace abuse;
  score a zero and retain bounded evidence;
- `control_plane_integrity`: catalog, signature, transport, grader, lease, or
  validator-controlled digest mismatch; fail closed, quarantine/retry by stage,
  and do not charge the miner.

`failure_code` is null only for `resolved`; every non-resolved terminal domain
has a bounded machine-readable failure code. Infrastructure, task-invalid, and
control-plane-integrity outcomes are excluded from the repair mean. Only
candidate-integrity incidents are attributable scoreable zeroes. The repair
mean is integer floor division over the scoreable binary task vector.

Retry policy is bounded by the ticket lease and terminal domain. A repair
failure is not retried as infrastructure, and an invalid task is not scored as a
miner failure.

## Signed evidence

Each `CodingTaskEvidence` binds:

```text
coding_contract_version and weight_eligible
shared coding run, validator ticket, agent artifact, corpus release, task set, case, and variant identities
visible bundle, base tree, memory bundle, resource profile, and environment digests
model, provider route/profile, reasoning, prompt/tool-schema, usage, and retry identity
authoring event root and bounded transcript digest
frozen patch, final tree, changed-path root, and protected-path verdict
grader bundle/image/platform/test-manifest, canonical grader plan, resource profile,
ordered execution-receipt root/count, and before/after integrity digests
exact build and test counts
terminal domain and integer repair_score_micros
```

`CodingRunEvidence` binds the sorted task evidence roots, task-set manifest,
binary task vector, pass/fail/invalid counts, and integer repair mean. Its digest
must become a first-class score-signature field before any weighted activation;
placing it only in advisory report details is insufficient.

Contract v1 canonical bytes are the validated known-field JSON projection with
lexicographically sorted object keys, compact separators, UTF-8 encoding, and
one trailing newline. Evidence decoders reject duplicate fields; harness
decoders reject duplicate known fields. All decoders reject missing known
fields, ignore unknown fields for rolling compatibility, and exclude unknown
fields from the canonical digest. JSON input is bounded to 4 MiB and 32 nesting
levels. The public Python/Go/Rust vectors under
`packages/dittobench-coding-contract/testdata` are the cross-language authority
for these bytes and roots, including Unicode separators and nullable fields.

Task evidence is not independently signable: its digest requires the exact run
manifest and validator ticket. Run-evidence digesting additionally replays every
manifest task, task-evidence root, terminal domain, score, and aggregate count.

## Retirement

Platform maintains an append-only exposure ledger. Issuing a selected visible
capsule or grader bundle marks that task/version consumed even if the run later
fails. Weighted selection excludes consumed tasks; reuse requires a new hidden
and adversarial grader version with a new commitment. The one fixed weight-zero
shadow task is an explicit non-weighted exception.

While a corpus release can affect a private shadow or future weighted run,
public surfaces expose only schemas, commitments, bounded redacted telemetry,
and retired evidence approved for release. Active repository bundles, task
manifests, memory mappings, policies, hidden tests, and reference fixes remain
private. A retired release may be published only after an explicit Platform
retirement transition and leakage review.

Before activation, winning coding harnesses must flow through the existing
king-only `artifact_release` path. Active task identities, hidden tests, and
answer-bearing traces stay redacted; approved unredacted evidence may be
released only after the associated task version is retired.

## Implementation sequence

This ADR enables, but does not combine, the following PRs:

1. task/memory/run/evidence schemas and public vectors;
2. DittoBench-owned coding-runner workspace tools and freezer;
3. DittoBench-owned pristine deterministic grader;
4. coding-specific default-deny sandbox executor and egress allowlist
   (`services/dittobench-api/internal/codingexecutor` owns the public adapter;
   the public supervisor/certification image proves the wire, while each
   repository-specific trusted test driver remains a separately certified
   digest-pinned artifact);
5. Luna Platform route and ticket-scoped broker capability;
6. scoped miner-memory seed and deterministic policy diagnostics;
7. private catalog, per-artifact selector, and exposure ledger;
8. validator/Platform shadow leases, evidence persistence, and king artifact release;
9. one-task calibration against no-memory, always-use, random-memory,
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
