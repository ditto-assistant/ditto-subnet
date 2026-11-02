# Private surface producer (not qualified)

This package and `cmd/private-produce` implement a trusted operator-side
candidate producer. They do not issue tickets, change production configuration,
publish answers, or activate a benchmark. The production preparation queue and
qualification gate must be integrated before use for scored work.

Each profile binds explicit rewrite/validator model and provider IDs, both prompt
versions, decoding parameters and privacy routing. Requests require OpenRouter
`zdr=true`, `data_collection=deny`, strict structured outputs, an exclusive
provider and no fallback. These are requested routing policies, not independent
proof of a provider's retention behavior. The profile cannot use the same model
for rewriting and validation. Receipts record the actual returned identities,
request/response digests, token usage and reported cost.

Synthetic records are data, including embedded hostile instructions. Rewrites
must preserve meaning, constraints, exact values, language, trust boundaries and
intentional misspellings. An independent model judges each rewrite. A separate
generator pass checks protected-value counts and immutable artifact fields.
Typo provenance is observed without changing the base artifact; those exact
tokens and graded values are masked during rewriting and restored before the
independent semantic check. The writer also receives the same original source
as reference-only context so opaque markers do not hide grammatical roles; it
never receives answer values absent from that source. This prevents spelling-
correcting a benchmark feature. A semantic rejection permits at most five
candidates, each subject to the same judge. The receipt retains rejected-call
provenance; exhausted retries still fail. The last candidate explicitly requests
explicit source preservation using `{"text":null}` rather than retyping masked
text. Null is accepted only in a valid writer response with provenance, never
as an error fallback; missing fields and malformed responses still fail.
Exact byte equality is validated deterministically
and recorded as `exact-byte-identity-v1`, without claiming an LLM validation.
Every actual change still requires the independent judge; all candidates still
pass the mechanical and artifact checks. Before/after hashes expose unchanged surfaces; they must
not be counted as demonstrated private coverage. A completely unchanged artifact
still fails. Transient network errors and provider 429/502/503/504 responses
share the same five-total-candidate budget, with bounded cancellation-aware
backoff and recorded sanitized failure reasons. Authentication, missing routes,
malformed JSON, filtering and token-limit failures do not retry. No extra retry
budget is hidden inside an individual provider call. Reported call costs exclude
any failed request for which the provider did not return usage.
Neither check proves the benchmark qualification gates. Failed requests do not
fall back to public generation or weaker validation.

The CLI makes a new 0700 output directory, draws a nonzero cryptographic salt,
and writes 0600 files with exclusive creation. Credentials come only from the
trusted process's `OPENROUTER_API_KEY` environment. Never put that key, base
artifact, candidate or probe files into a miner image or a public repository.
`-probe-surfaces` is diagnostic only and saves rejected candidate text privately;
it cannot produce an accepted artifact. Ordinary mode bounds concurrency 1..16,
per-request timeout 90 seconds and total duration two hours. A candidate must be
durably pinned before leasing; do not rerun this CLI to reconstruct a pinned
object. The trusted preparation worker passes its reserved 8-byte nonzero
big-endian entropy through an owner-only `-salt-file`; malformed/zero files fail
without drawing replacement entropy. `-profile-sha` inspects the profile without
credentials or inference. Explicit reasoning efforts, when configured, replace
temperature and are bound into the profile digest.
Restricted diagnostics retain candidate text, including failed attempts' final
candidate; they are not public artifacts or qualification evidence. The bounded
semantic receipt may be up to 4 MiB for a full-profile run.

## Evidence so far (2026-09-17)

- Real synthetic six-surface samples with Gemini 2.5 Flash rewriting and GPT-4.1
  validating accepted 4/6 and 5/6. Rejections included correcting intentional
  misspellings and changing ambiguous relationships.
- Reversing the model roles accepted 6/6 sampled surfaces. This is only a small
  semantic probe, not a complete approved dataset or proof of semantic fidelity.
- Full profile planning for seed 4242 has 1,708 unique surfaces and a 666,525-byte
  base artifact. Generation therefore belongs outside lease transactions and
  the current 60-second production generate-service request window.
- Unit tests use fake inference strictly for privacy-request, failure, receipt,
  cancellation and traversal contracts. Those fixtures are not qualification.
- The 40-seed full public GIH control on 2026-09-17 scored 0.237 on tool prompts
  and 0.77926078 on quantity, below the required 0.90 control floor. Aggregate
  memory was 0.90005; it does not excuse failed slices. No private resistance
  claim can be made from this incomplete-strength control. Raw local report
  SHA-256: `3686b2b5d3b4421e4d088be54b541a60fd612e165242a4879e5749d0fbe27202`.

Launch still requires real complete artifact production, semantic negative
controls, measured honest and adversarial runs, durable preparation, closure and
reveal, guarded readiness, reviewed deployment and a private end-to-end canary.
# Complete-checkpoint recovery

`cmd/private-recover` is an offline **trusted-operator** finalization tool for
an owner-only checkpoint written by `ProduceWithDiagnostics`. It is not an
upload API, a semantic validator, or a way to approve failed/partial generation.
Diagnostics are unsigned local evidence; only the operator's own authenticated
checkpoint may be used. Never accept a miner-provided checkpoint.

Pin its SHA-256, the original profile digest and profile JSON. The tool requires
those pins, rejects shared/symlinked/oversized inputs, regenerates the original
base byte-for-byte, requires exactly one successful diagnostic per surface,
checks all surface hashes and completion identities, and reruns mechanical
artifact validation. It emits to a new owner-only directory without network
calls. Missing, rejected or inconsistent surfaces fail closed.

Full-profile retry audits can exceed 4 MiB even when every surface succeeds.
The producer and Platform now share a 32 MiB receipt bound; Platform's appended
migration preserves all content-hash and immutability constraints. The full
rejected-attempt audit is retained, not dropped to fit. A recovered candidate
still needs adversarial qualification and normal Platform pinning/issuance.
