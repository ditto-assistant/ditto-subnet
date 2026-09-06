# Native runtime budget profile

Status: concrete, policy-bound conservative estimator and synthetic conformance
tests. No approved live profile, tokenizer equivalence, provider probe, credential
loading, worker activation, scoring, weights or emissions are supplied here.

## First algorithm: reviewed billed-input cap

The first estimator is `provider-billed-input-cap-v1`, **not** a local tokenizer
or a characters-per-token approximation. Before every serial model request it
reserves the full reviewed maximum billed prompt-token count, plus the requested
maximum completion tokens. Provider framing, tools, reasoning accounting and
rejected/error requests must all be covered by the review. An accepted-context
limit alone does not prove a maximum billed count for every outcome.

This is deliberately conservative: if remaining input allowance is below the
reviewed per-request cap, the ledger refuses another request even when its actual
prompt might fit. Verified settlement refunds unused reservations as before.
Unknown outcomes retain all ceilings. This is an admission bound, not an estimate
of observed model efficiency or a new scoring metric.

A tighter tokenizer-based implementation needs its own reviewed algorithm,
tokenizer/model/framing identity and conformance evidence. Do not silently use a
nearby model's tokenizer or call this first algorithm exact token counting.

## Bound identity and arithmetic

`HostedBudgetProfile` fixes the native model, OpenRouter route, route profile,
algorithm, billing caps, upper-bound unit prices, fixed request charges, review
evidence digests and a validity window of at most 24 hours. All arithmetic inputs
are bounded integers. Price units are **USD nanodollars per token**, not dollars
per million tokens or floating-point rates. Each component rounds up separately:

```text
reserved microdollars =
    ceil(prompt_cap × prompt_nanodollars_per_token / 1000)
  + ceil(requested_completion_cap × completion_nanodollars_per_token / 1000)
  + fixed_request_microdollars
```

Prices must cover every possible billing tier and surcharge during the validity
window, including long-context/reasoning accounting and fees. No cache discount,
promotion or blended/catalogue minimum is assumed. Positive upper bounds are
required; a discount cannot lower a committed cap in flight.

Before settlement, reported actual usage is checked against the same unit-price
ceilings and fixed-charge allowance. A price increase cannot hide behind the
larger full-input reservation. Cheaper observed usage still settles normally.

The approved native policy commits `runtime_profile_sha256`, which hashes the
profile's canonical known-field projection. That policy digest already flows
through immutable assignment, grant, reservation and settlement authority. No
new database schema is required. A new profile requires a new policy/assignment;
there is no in-place grant refresh. Unknown profile fields remain non-authority.

Old unprofiled policy serialization/digests remain unchanged when the optional
field is absent/null, preserving shadow fixtures. However, real network transport
now requires `ProfiledBudgetEstimator` bound to that exact policy. Generic
callbacks are restricted to the explicit `httpx.MockTransport` test seam; passing
a real transport through that seam is rejected. No production fallback exists.

Validity is checked at construction, relay authorization, estimation, immediately
after reservation commit before dispatch, and before output release. The HTTP
timeout is also capped by remaining profile validity. Clock rollback, nonfinite
time, not-yet-valid or expired profiles fail closed. Receipt settlement may still
record actual billing after expiry, but late output is suppressed. Expiry during
commit leaves a conservative pending reservation, not fabricated zero usage.

## Review is separate from a digest

The three review digests reference provider/account posture, token accounting and
pricing evidence. They do not themselves prove review or constitute a signature.
The existing trusted operator approval of the native policy must verify the
referenced evidence before approving its profile digest. The worker must preserve
the canonical profile (`canonical_profile()`) with its private evidence.

OpenRouter describes [provider-specific limits and pricing](https://openrouter.ai/docs/guides/overview/models)
and [provider routing controls](https://openrouter.ai/docs/guides/routing/provider-selection).
Catalogue data and documentation alone are not proof of the deployed account's
billing ceiling or regional/ZDR behavior. A Hippius storage probe is unrelated to
this model-provider review. This PR contains no production price/cap defaults.

The unchanged locked model request does not add provider-side `max_price`
controls. Therefore the local cost reservation is conditional on the reviewed
billing bounds; it is **not** a guarantee against a provider billing outside its
contract. An over-limit receipt fails settlement and leaves unresolved evidence;
it cannot undo a charge. If the required bounds cannot be established for the
deployment, do not activate this algorithm. Adding provider-enforced price
controls requires a separately reviewed native request contract change.

## Verification and next step

Tests pin canonical identity and integer rounding, reject profile/policy drift,
exercise expiry/clock failure and run the real PostgreSQL reservation/refund path
with synthetic receipts. The full relay/provider regression suite remains valid.
These tests establish implementation behavior, not provider capabilities.

Next: the durable evidence publisher and full worker orchestration, using an
operator-approved live profile only after its review evidence is available.
