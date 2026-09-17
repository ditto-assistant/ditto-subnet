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
independent semantic check. This prevents the rewrite model from spelling-
correcting a benchmark feature. A semantic rejection permits at most five
candidates, each subject to the same judge. The receipt retains rejected-call
provenance; exhausted retries still fail. The last candidate explicitly requests
verbatim source preservation, but still goes through the independent judge and
all artifact checks. Before/after hashes expose unchanged surfaces; they must
not be counted as demonstrated private coverage. A completely unchanged artifact
still fails. Transport failures do not retry.
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

Launch still requires real complete artifact production, semantic negative
controls, measured honest and adversarial runs, durable preparation, closure and
reveal, guarded readiness, reviewed deployment and a private end-to-end canary.
