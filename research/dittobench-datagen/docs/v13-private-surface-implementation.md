# V13 Platform-owned private surface implementation

Status: implementation in progress; **not enabled or qualified**. Owner selected
Platform-owned generation instead of validator commit/reveal. This does not
depend on the separate coding competition or the offline curator signing key.

## Implemented foundation

`gen.ApplyPrivateSurface` is separate from the legacy `TranslationPass` and the
public generation entry points. It requires a salted V13 artifact, a transformer,
and an independent semantic validator. It returns a detached artifact only after
all surfaces pass. Errors redact provider payloads. Duplicate pair attachments
within one user graph share one result; conflicting input copies fail. Different
graphs do not share a rewrite merely because their pair IDs match.

Callbacks see only protected values already present in their source text, not
the rest of the grading answer set. Mechanical checks reject changed counts or
introductions of protected grading values, malformed/oversized output, and a
completely unchanged artifact. Only prompts, questions, and pair text can change.
Grading metadata, graph identities, fixtures, catalog and ordering remain fixed.

`gen.DecodePrivateArtifact` checks exact downloaded bytes against an independently
trusted lease SHA, identity/profile, and immutable fields against the salted base
generator. It returns the received artifact, not a regenerated public substitute.
It is a consumer integrity primitive, not yet wired into the running scorer.

Neither primitive proves semantic equivalence or resistance to inversion. A
whitespace-only rewrite can pass mechanical checks. Test callbacks explicitly
exercise plumbing, not qualification. No production provider or semantic
validator is selected by this change. Existing public and pre-V13 paths are
unchanged; there is no new activation flag in this foundation.

## Remaining implementation sequence

### 1. Durable Platform artifact ownership

- Create an immutable dataset identity including benchmark, profile, seed and
  transformation-profile digest. Allocate secret randomness once with a
  cryptographic RNG; never derive it from the public seed.
- Store base bytes, transformed bytes and provenance in restricted storage.
  Retain source/output SHA-256, provider/model revision, prompt/schema revision,
  semantic-validation receipt and qualification-profile identity. Do not log
  prompts, answers or salt. Provider credentials stay Platform-side.
- Pin one successfully committed object using create-only writes and a database
  uniqueness/CAS boundary. Concurrent generation losers reuse the committed
  object. Retries/restarts fetch bytes; they never rerun an LLM to reconstruct a
  previously pinned object. Test crash and concurrent-writer recovery.
- Extend Platform generation without switching public versions or existing
  tickets. Current `DatasetGenerator.generate` keeps only a response-header hash;
  private generation must verify the body hash and persist it before leasing.

### 2. Authenticated delivery and exact execution

- Bind artifact access to the authenticated validator and exact live ticket,
  dataset hash and expiry; no arbitrary URL fetching or public seed lookup.
- Add an explicit private-dataset capability to the signed fleet contract.
  Route private tickets only to compatible validators/scorers. Old consumers
  ignoring additive fields must not receive private work.
- Wire delivery through Python validator to trusted Go scorer. Parse bounded
  bytes with `DecodePrivateArtifact`, then use its tool cases, memory cases,
  waves and catalog for execution and harness projection. Preserve staged
  evidence IDs and isolation scheduling. The current scorer locally regenerates
  all of those; replacing only the hash check is incorrect.
- Bind the **raw stored-byte hash** to the report and signed result. Never
  recompute that pin from reserialized JSON. Persist private material only under
  restricted storage, never the generic public artifact directory or harness.
- Cover normal jobs, confirmation/CRN, canary jobs, cancellation and retry.
  Define confirmation identity so compared agents really share the same private
  dataset. Do not independently paraphrase each side of a CRN comparison.
- Replace private finalized reveal's regeneration with exact pinned-object
  retrieval. Reveal only after every work item authorized to reuse that private
  object is closed; an agent's finalized score alone is not sufficient for CRN.

### 3. Actual transformation and qualification

- Select an approved provider/retention policy and immutable transformation
  profile. Independent semantic validation must check facts, negation, ordering,
  units, scope, instruction priority and output-language requirements. Exact
  value counts alone cannot validate these.
- Establish whether untranslated catalog/subject/fixture surfaces still permit
  inversion. If so, extend the producer and consumer contract together rather
  than marking prompt-only rewriting sufficient.
- Run fresh-seed generator-inversion adversaries and the real locked-model
  honest harness. Retain raw receipts, image/source hashes, dataset commitments,
  per-gate false-zero breakdown, cost and coverage. No mocked qualification.
- Enforce the benchmark's documented acceptance limits (including honest gate
  loss <=0.02 and adversaries <= honest starter-kit composite minus 0.05), plus
  required tool/control baseline evidence. Record failures, not only aggregate
  successes. Qualification must bind the exact transform/runtime profile.

### 4. Operational gating and rollout

- Expose read-only readiness through Backroom, with missing prerequisites.
- Fail closed for private V13 issuance if artifacts, qualification or fleet
  compatibility are missing. No public-generator fallback.
- After review/deployment and actual qualification, issue one supported isolated
  private canary. Verify end-to-end signed acceptance and non-leakage.
- Only then request activation approval and start the supported rollout. Starting
  that rollout can activate automatically once quorum conditions are met, so it
  is not a harmless readiness probe. Keep V12 serving meanwhile.

## Validation boundaries

Unit tests cover atomic failure, unchanged-input rejection, protected-value
mutation/introduction, provider-error redaction, cancellation, repeated records,
graph scoping, real generated-artifact traversal, immutable-field tampering and
raw-byte digest mismatch. Run `go test ./...` in the datagen module for frozen
version regressions. Full delivery tests, real provider runs and fleet/canary
evidence remain necessary; this document is not an activation receipt.
