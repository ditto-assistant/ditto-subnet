# Platform-hosted private Coding execution v2

Status: proposed normative architecture delta. Runtime implementation and
activation remain pending. This document installs no service, grants no data
access, and changes no existing v1 wire contract.

## Decision and scope

Private Coding v2 evaluations MUST execute inside infrastructure operated by
the trusted Platform execution service. Validator operators request evaluations
and verify result envelopes; their machines MUST NOT receive private task or
grader bundles. Miner developers use only public practice data for local tests.

For this deployment profile, this delta takes precedence over earlier Coding
documents wherever they place private execution, plaintext delivery, or raw
evidence on a validator-owned host. Existing v1 behavior remains documented for
compatibility and public/synthetic testing. Enabling its worker or remote mode
alone does not satisfy this v2 profile.

Hippius remains the sole remote object store for encrypted private inputs and
sealed evidence. PostgreSQL owns release registration, assignments, attempt
state, evidence acceptance, and eventual scoring state. Neither object storage
nor a validator's claimed result can establish those authorities.

## Trust and visibility

| Principal | Permitted information | Excluded information |
| --- | --- | --- |
| Miner developer/local runner | Ten public tasks, public memories/tests/graders, local diagnostics | Private release downloads, private evaluation transcripts and patches |
| Validator operator/process | Its authorized opaque evaluation handle, artifact identity, bounded status, approved signed result | Private issues, source snapshots, memories, task groups/conditions, hidden tests, patches, raw logs, storage keys/URLs, unwrap grants |
| Candidate harness inside Platform | Selected issue, scoped raw memory, typed repository tools, scoped inference route | Full catalog, unselected tasks, hidden grader, reference solution, host access, storage credentials |
| Platform authoring supervisor | Exact assignment and selected authoring inputs, observed tool/inference use, frozen candidate patch | Hidden grader contents during authoring, curator signing key, storage administration |
| Platform grader | Frozen patch, pristine base, selected hidden grader and grading authority | Candidate credentials, arbitrary private catalog access |
| Platform retrieval/evidence mediators | Exact authorized objects, phase-bound key-service capabilities | Arbitrary caller-selected object access or storage administration |
| Curator and key-service operators | Release preparation or key custody under their assigned role | Automatic permission to act as a miner or validator |

“Validators cannot see the dataset” means no private material or download
capability is delivered to validator-controlled processes or hosts. It does
not mean the candidate can solve an issue without observing its required
inputs. The candidate runs on Platform infrastructure and its developer must
not receive a task-level export afterward.

Platform host administrators remain trusted with plaintext. Ordinary containers
do not hide process memory from the host operator. Confidential execution on
validator-owned machines would require a separate attestation and key-release
design; it is outside this profile.

Approved inference providers can receive task excerpts through the scoped
relay. Their data handling, retention, and route configuration are part of the
trusted processing boundary. A local embedding strategy remains miner-owned;
this milestone adds no external embedding capability.

## Evaluation lifecycle

1. Platform freezes and verifies the screened miner artifact digest and
   qualification state before deriving the private selection.
2. An authenticated validator requests or claims only its assigned evaluation
   handle. The request binds validator identity, artifact digest, contract and
   policy revision, deadline, request nonce, and idempotency identity. It cannot
   name a task, condition, bucket, object, execution host, command, or callback.
3. Platform commits the private selection and attempt identity in PostgreSQL.
   A duplicate request returns the existing attempt; it cannot reroll the task
   sample or grant another candidate attempt.
4. The Platform supervisor authenticates to retrieval and the unwrap service
   using its own identity. Only an admitted release and the exact assigned
   authoring phase permit reads. A validator credential cannot obtain a key or
   object capability by forwarding the same handle.
5. The mediator performs complete ciphertext size/hash verification,
   authenticated decryption, and plaintext identity verification before
   projecting the selected issue, memory, and repository tools to the harness.
   The complete catalog and hidden grader are excluded from that projection.
6. Platform durably records the start boundary before candidate code runs.
   Enforce pinned model/tool/runtime policies, token and request budgets,
   wall-clock and CPU/memory/disk limits, and default-deny candidate egress.
   Arbitrary network calls, host mounts, Docker sockets, and cloud metadata
   access are prohibited.
7. Platform revokes authoring capabilities and freezes the patch. Only the
   accepted freeze permits a distinct grading process to obtain hidden tests
   and grade a pristine workspace. Hidden test output cannot return to the
   candidate harness or its inference session.
8. The evidence mediator seals private transcripts, patch and grading records,
   verifies storage readback, and finalizes their reserved identities.
   PostgreSQL accepts the terminal result only after required evidence is
   finalized. Public or validator receipts contain no data-retrieval capability.
9. Validators verify the approved signed result against their assignment and
   submit an acknowledgement bound to its digest. Candidate state and grants
   are destroyed/revoked at termination; private retained evidence follows the
   declared audit and retention policy.

Recovery operates on the same durable attempt. After the start boundary,
timeouts, disconnection or missing acknowledgements cannot cause a fresh
authoring run. Any permitted infrastructure retry needs a separately recorded
reason and attempt lineage; ambiguous state cannot silently become a new run.

## Validator result projection

A new versioned request/result schema MUST be reviewed before runtime wiring.
Do not reuse a v1 envelope that carries execution bundles or transcript bytes.
The result's canonical signed projection MUST bind:

- schema and contract/profile versions;
- validator audience, opaque evaluation/attempt IDs, and request digest;
- immutable candidate artifact digest;
- opaque release/selection commitments without task-group mappings;
- execution and grading policy digests and deployed runtime identity;
- bounded terminal classification and approved aggregate outcome/cost fields;
- opaque evidence commitment, issuance/expiry, and signing-key identity; and
- `shadow_only=true` and `weight_eligible=false` for the initial implementation.

Use a fixed allowlist of outward fields. Never copy or serialize a private
runtime object into a response, error, log, callback, metrics label, or tracing
attribute. Unknown request fields may be ignored for compatibility but must
not be signed as authority, persisted as private inputs, or echoed outward.
Keep private task IDs, conditions, filenames, test names, tool output, patch
bytes, storage coordinates and secret values out of every outward projection.

An evidence digest identifies retained evidence; it does not grant access to
its preimage. Signature verification proves origin and integrity, not that
execution occurred correctly. Trust keys must come from the configured trusted
Platform identity, never solely from the response being verified.

Responses and errors MUST be `no-store`. Status polling uses coarse states
with bounded diagnostics. Per-condition rates and detailed timing remain
withheld until disclosure policy has established a sufficient aggregation
threshold. A single-task canary must not reveal hidden test details through an
error string or a supposedly aggregate report.

## Quorum and benchmark semantics

Several validators checking one Platform receipt provide multiple checks of
that receipt, not independent execution. If a quorum requires repeated runs,
Platform must allocate distinct isolated attempts with explicit replicate IDs,
the same artifact/selection/policies, fresh workspaces and inference sessions,
and retained execution evidence for each. These remain under a common Platform
operator and therefore share infrastructure and trust failure modes.

Qualification, scoring and reward activation require a separate reviewed
policy. Local public scores never contribute to consensus, ranking, weights,
or emissions. Causal memory lift remains a diagnostic; a competitive formula
must not reward deliberately failing the no-memory control.

The sampling unit remains a separate decision: 30 unique groups with one
condition each and ten groups with three matched conditions are different
experiments. The future selection contract must fix that choice, balancing,
randomness commitment, submission limits and disclosure policy before runs
can become competitive evidence.

## Required acceptance evidence

Implementation PRs must execute the following tests with synthetic secrets
and private-data markers; this document itself does not provide runtime proof.

| Scenario | Required result |
| --- | --- |
| Validator requests private bundle, unwrap grant or raw evidence using a valid handle | Rejected before private object/key access; safe error |
| Wrong audience, artifact, signature/key, expiry or replayed request | Rejected or exact idempotent replay; no additional attempt or data access |
| Candidate prints task or grader markers in output, patch, error or trace | Markers absent from validator/miner responses, external logs and metrics |
| Ciphertext, plaintext, object size or assignment digest is altered | Failure before candidate materialization |
| Candidate tries network, filesystem or hidden-test access | Boundary denies access; no grader response reaches candidate |
| Poll/reconnect after patch freeze or lost acknowledgement | Original attempt and patch recovered; authoring not rerun |
| Two validators receive evidence from the same execution | Shared attempt detected; cannot count as two independent runs |
| Registered release is quarantined/retired before admission | No new execution or unwrap grant; in-flight disposition follows explicit policy |
| Local practice result is submitted as private result evidence | Rejected from private result admission and reward paths |
| Full one-task hosted canary | Frozen patch graded, evidence finalized, signed result verified, validator-host artifacts contain no private markers |

Use deterministic synthetic fixtures and fault injection in CI. A live canary
must additionally bind the deployed service identity, provider receipt, key
custody, registered release and exact screened artifact. Neither a green docs
check nor registry insertion meets that gate.

## Implementation order

1. Replace the nine-task public practice pack with ten small repository-hosted
   tasks and datagen execution; migrate consumers and retire Hugging Face
   publication in that replacement PR. Dataset replacement does not remove v1
   protocol compatibility automatically.
2. Review the private release registry against this profile. Registration
   records remain non-selectable and cannot authorize unwrap or execution.
3. Implement versioned validator request/result projections and their tests.
4. Connect Platform-only retrieval, phase-scoped unwrap, hosted execution and
   evidence mediation; migrate private orchestration off validator hosts.
5. Demonstrate one hosted private shadow evaluation and validator verification.
6. Calibrate repeated evaluation, scoring, exposure limits and release rotation
   before a separate decision on competitive activation.
