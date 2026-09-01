# DittoBench Coding contract vectors

This directory is the language-neutral public authority for DittoBench Coding
wire examples and canonical digest vectors. Contract v1 is shadow-only and
hard-codes `weight_eligible=false`.

Consumers verify the vectors they are authorized to see. Python and Go verify
run manifests, evidence, grader plans, resource profiles, and ordered execution
receipts. The Rust miner verifies only miner-facing seed/run and memory vectors;
it must never receive grader plans or receipts. A contract change is incomplete
until every affected consumer passes the same relevant vectors and boundaries.

`coding_catalog_v1.json` freezes the public known-field catalog commitment,
signing-message digest, and a synthetic private-exposure projection. The
exposure contains only opaque IDs and content digests; it is a contract vector,
not a usable task, repository, memory bundle, grader, or Merkle proof.

`coding_selection_v1.json` freezes the synthetic finalized anchor and
future-height assignment,
position-bound catalog membership proof, deterministic selected probe, private
task-set identity, shared public run manifest, Platform run authority, and
exposure projection. It contains digest-only synthetic records and no usable
private corpus material.

`coding_artifact_capability_v1.json` freezes the single-capability delivery
wire shared by Platform and the trusted Go artifact fetcher. It covers every
allowed authoring/grading phase, kind, audience, and size-policy combination.
Its URLs use the reserved `.invalid` domain and synthetic signatures. They are
transport examples, never identity, credentials, or usable capabilities. Rust
and miner-facing code must not consume this validator-only vector.

`coding_sealed_evidence_upload_v1.json` freezes the signed validator requests,
redacted S3 PUT capability, and finalized acknowledgement for one sealed coding
evidence object. It binds a ticket, active claim generation, upload UUID, kind,
whole-object SHA-256, exact size, and fixed content type. The capability URL is
transport only; no key, bucket, credential, or object version enters a signed
request, finalization, or miner-facing wire. Python and Go consume this vector;
Rust and miner-facing code must not.

`coding_authoring_freeze_v1.json` freezes the validator-only authoring evidence
digest, content-addressed evidence references, signed phase-transition message,
and accepted response. It contains no patch or transcript bytes and grants no
grader capability.

`coding_grading_lease_v1.json` freezes the signed request and freeze-bound
grading response shared by Platform and the validator client. The response
contains exactly visible, resource, and grader capabilities; memory is
structurally absent. Its URLs use reserved `.invalid` transport examples and
are never usable credentials.

`coding_shadow_result_submission_v1.json` freezes one authority-replayable
validator-infrastructure submission and accepted response shared by Platform
and the validator client. It contains synthetic manifests and evidence only,
no repository, patch, transcript, memory, grader, or private task bytes.

`coding_attempt_supervisor_v1.json` freezes the synthetic private control wire
shared by the Go supervisor handler and Python runtime client. It covers broker
preparation, a revocation-capable synthetic grant handoff, ticket-bound
screened-harness launch authority, authoring, grading, both abort operations,
and restart recovery. Its lease and
evidence objects are deliberately synthetic placeholders: the vector proves
outer operation/identity/nullability compatibility and contains no artifact
task, patch, transcript, provider credential, live control token, broker private
key, or usable capability. All URLs use reserved `.invalid` hosts and all
bearer/key material is visibly synthetic.

`coding_execution_plan_v1.json` freezes the missing task-static execution
preimages required by the trusted Go runtime. Its authoring runner plan contains
only visible workspace path policy, visible test/build commands, and candidate
limits. The phase-separated grader plan and resource profile contain synthetic
protected command paths and are validator-only. Python and Go independently
recompute all three digests and reject cross-phase identity, command, path,
limit, resource, or compiled-grader drift. Rust and miner-facing code must never
consume this vector. `generate_execution_delivery_vectors.py` binds the runner
digest through the selected task commitment and keeps the related selection,
artifact-capability, and grading-lease fixtures coherent. Platform returns only
the runner plan in authoring and only protected plan/resource fields in grading.

`coding_inference_miner_v1.json` freezes two synthetic, miner-visible Luna Chat
Completions turns using the public coding prompt and ordered workspace-tool
schemas. Rust, Python, and Go use it to prove the reference harness emits the
agreed request shape and can consume the miner-facing response. It contains no
grant, route policy, receipt authority, cost evidence, URL, or credential.

`coding_inference_policy_v1.json` is validator/curator authority. It freezes the
known-field preimage of `inference_grant_sha256`: model, provider route/profile,
medium-reasoning contract, ZDR and data-collection posture, no-fallback rails,
resource ceilings, and receipt-free retry policy. It also freezes synthetic
complete, retry-complete, provider-failure, and not-invoked evidence plus the
ordered provider settlement and receipt-set digests. Rust must never consume
this vector.

`coding_inference_policy_locked_v1.json` is the exact standalone policy
projection extracted from that vector. Platform and the default-off Go coding
host parse this bounded file directly, avoiding a deployment-time extraction
or a second policy serialization. It contains no ticket, grant ID, URL,
provider receipt, bearer, or credential. Rust must not consume it.

The three files are generated by `generate_inference_vectors.py`. CI runs the
generator in `--check` mode; changing the public prompt, tools, policy, receipt
grammar, or fixture requires regenerating and reviewing all outputs together.

Vector files are independent synthetic scenarios unless a file explicitly
imports another file's expected digest. Older catalog, certification,
authoring-freeze, and aggregate vectors intentionally use distinct synthetic
policy/prompt/tool hashes to test binding transport; they are not implicit join
keys to `coding_inference_policy_v1.json`. A real catalog must retain the exact
policy preimage whose canonical digest it commits and deliver that preimage to
every validator consumer.

The vectors contain only synthetic identifiers, digests, policies, and reserved
domain transport examples. Private catalog records, repository bundles, hidden
tests, policy labels, provider credentials, signing keys, and usable bearer
capabilities must never enter this package.

Canonical JSON uses lexicographically sorted object keys, compact separators,
UTF-8, no unpaired surrogate escapes, at most 32 nesting levels, escaped
U+2028/U+2029 separators, and one trailing newline. Evidence
roots additionally require the selected run manifest and validator lease ticket;
raw task or run evidence is not independently signable.
