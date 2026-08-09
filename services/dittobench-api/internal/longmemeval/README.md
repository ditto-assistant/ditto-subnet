# LongMemEval v9 confirmation core

This package is the bounded Go runtime for issue #385. It remains a shadow-only
confirmation dimension until calibration and activation work explicitly promote
a profile. It does not change v8 scoring or run for ordinary base-v9 jobs.

## Imported provenance

The runtime preserves the LongMemEval adapter condition already audited in this
monorepo under `integrations/longmemeval` and dittobench-api#78:

- cleaned LongMemEval-S SHA-256:
  `d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`;
- Hugging Face revision:
  `98d7416c24c778c2fee6e6f3006e7a073259d48f`;
- LongMemEval source revision:
  `9e0b455f4ef0e2ab8f2e582289761153549043fc`;
- public harness revision:
  `c3caa8e2c19f8a41a0610b9f7db774f97643dd9c`;
- public tool-catalog dependency revision: `ef3af0387b46`; and
- adapter condition: `longmemeval-s-cleaned-native-memory-tools-v2`.

The loader verifies the complete source bytes against the frozen profile before
returning a selected case set. It streams metadata and then retains only the
selected rows, avoiding expansion of the roughly 265 MiB JSON file into the
multi-gigabyte in-memory form seen by the research Python adapter.

## Preserved adapter behavior

- Each selected question receives one isolated user/store namespace.
- User, case, session, and pair identifiers are keyed UUID-shaped aliases.
- Case execution order is independently keyed; history order is not changed.
- Assistant-first, user-only, odd-length, and same-role-adjacent histories are
  represented without inventing turns.
- Contentless pairs are removed.
- Reused logical pair identities use last-write-wins, matching harness upsert
  behavior.
- `/run` receives the original question, the question date in the established
  system prompt, and the exact four native memory tools.
- Question type, question ID, answer-session IDs, `has_answer`, source session
  IDs, answer provenance, profile/dataset identity, and grader state never cross
  the harness boundary.

The `Judge` interface is deliberately trusted and receives the complete private
row plus hypothesis. Its production implementation must call the pinned,
unmodified official LongMemEval evaluator behavior. The core does not invent a
replacement heuristic or edit the official prompt.

## Budget and evidence boundary

`Executor.Execute` requires:

- a caller-supplied cryptographic projection key of at least 32 bytes;
- a positive wall-clock limit derived by #387 from the remaining ticket TTL;
- frozen request, prompt-token, completion-token, total-token, and authoritative
  USD-micro cost limits in the profile; and
- a dedicated trusted `ProviderMeter` session beginning at zero.

The meter is sampled after every seed, run, and judge operation. Provider/model
identity drift, fallback, missing receipts, non-authoritative cost, counter
rollback, or a cap overrun aborts the bundle. Harness-reported token counts are
ignored. No partial score or evidence is returned.

Signed evidence uses schema version 2. Its canonical typed JSON binds the
validator-observed total `latency_ms` together with artifact, bench version,
profile, dataset, case-set, score, ordered capability, and every provider lane
field. Decoding is presence-sensitive: a measured score or provider cost of
zero is valid, while an omitted/null zero-valued field, unknown or duplicate
field, or trailing JSON is rejected before signature verification.

The final scheduler/signer integration is intentionally small: #387 opens a
dedicated provider-accounting session, supplies the remaining ticket duration,
the content-addressed dataset reader, artifact SHA-256, frozen shadow profile,
and projection key, then calls `Execute`. `ExecutionResult` retains the raw
selection privately and exposes validation-gated `Validate`/`Digest` methods;
only its `Evidence` field may enter the signed report. The durable cache key remains
`(artifact_sha256, bench_version, profile_checksum)`.

The checked-in `testdata/profile-v1.json` caps are fixture values, not launch
calibration. No p50/p95 runtime, provider cost, or composite weight is claimed
here.

## Trusted provider runtime

`NewProviderSession` is the production transport boundary for one confirmation
bundle. A session exposes an OpenAI-compatible reader handler for the screened
harness, the pinned official LongMemEval judge, and the `ProviderMeter` sampled
by `Executor`. The reader and judge lanes each require:

- the exact model and profile revision frozen by the LongMemEval profile;
- an exact OpenRouter route and exact response-provider identity;
- `provider.only` and `provider.order` with fallbacks disabled and provider data
  collection denied;
- a unique response ID plus authoritative prompt, completion, total-token, and
  `usage.cost` fields on every request; and
- the profile's request, token, integer USD-micro cost, and per-request deadline
  caps.

Receipt cost is accumulated as an exact rational from the provider response and
conservatively rounded up only when projected to signed integer USD micros. The
receipt-set digest commits the exact rational cost and every other receipt
field. A missing, duplicated, ambiguous, identity-drifting, or over-budget
receipt permanently poisons that session; no evidence can be produced.
Before authorization or provider contact, the session reserves request count,
the JSON body byte length as a conservative prompt-token upper bound, the
requested completion-token maximum, and their total against the remaining
frozen caps. If the public starter harness omits a completion maximum, the
relay injects the deterministic nonzero floor of the signed reader policy's
`MaxCompletionTokens / MaxRequests`; explicit maxima must be positive,
consistent, and no larger than that same signed-policy-derived limit. This
behavior adds no activation-only runtime knob. Concurrent calls reserve
independently. Exact provider
cost and exact prompt tokenization exist only on the authoritative receipt, so
a single dispatched request can still cross the remaining cost cap (or reveal
a provider contract violation); that unavoidable overshoot poisons the session
and can never become signed evidence.

The official judge reproduces the prompt branches, frozen GPT-4o model,
temperature, and output cap from LongMemEval revision
`9e0b455f4ef0e2ab8f2e582289761153549043fc`. It accepts only a normalized exact
`yes` or `no` response, failing closed on ambiguous extra text. Reference
answers and question types remain inside the trusted judge and never cross the
harness reader route.

Provider credentials are not accepted in execution profiles, environment
variables, CLI flags, or job payloads. `GCPSecretManagerAuthorizer` takes a
server-owned, numeric-version `SecretManagerReference`, obtains a workload identity token
from the GCE metadata service, validates the exact Secret Manager resource and
CRC32C, and applies the decoded value directly to the outbound request. The
outbound authorization header necessarily holds the value until that request's
transport completes; the authorizer and runtime configuration retain no copy.
It does not cache the provider credential, follow redirects, return upstream
bodies, or serialize the reference in runtime configuration. Tests use
loopback fake servers only; no test reads a real secret.

The same implementation satisfies `SecretBytesResolver` for non-provider
confirmation keys such as the LongMem projection key and ablation selection or
projection keys. `Resolve` accepts only an immutable numeric-version reference,
returns a fresh caller-owned byte slice, and masks reference, transport,
resource-identity, payload, and CRC failures behind one non-sensitive error.
Callers must `defer ZeroSecretBytes(value)` immediately after resolution, copy
only into runtime-owned ephemeral buffers, and zero those buffers during
runtime cleanup. Plaintext keys do not belong in environment variables,
execution profiles, job payloads, readiness output, or logs.
