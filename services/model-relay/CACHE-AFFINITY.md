# Optional OpenRouter cache affinity

`DITTO_INFERENCE_OPENROUTER_SESSION_AFFINITY` defaults to false. It is an
experimental routing hint, not a completion cache. Keep it off until a matched
workload demonstrates lower receipt cost without worse benchmark results,
evaluation latency, failures or deadline success.

When enabled on the ordinary hosted chat lane, the relay derives an opaque
`X-Session-Id` from the authenticated grant UUID, generation and model. The ID
survives provider retries but changes on lease generation/model/grant changes.
Caller session headers are not forwarded. Messages, tools, seed, sampling,
provider privacy/filter/fallback policy, and output budgets remain unchanged.
Every admitted call still executes independently and settles its own receipt.
Confirmation and coding lanes are outside this experiment.

OpenRouter may use the hint for provider cache locality. A hint is not proof of
cached tokens or a discount. Provider order overrides affinity; availability
fallbacks can change providers. Measure actual provider identity, cached input,
receipt cost, errors and full-run timing. A short replay or identical generated
text does not establish benchmark non-inferiority or sustained fleet capacity.

Reference: https://openrouter.ai/docs/guides/best-practices/prompt-caching

## Activation and rollback

No deployment config enables the flag. A release containing the code therefore
leaves routing unchanged. Before a separately reviewed activation:

1. Run paired closed-loop benchmarks across representative artifacts and seeds,
   retaining independent quorum and fresh cases. Compare baseline repeat
   variance, score strata and crown/cutoff decisions.
2. Compare mean/p95 full-evaluation latency and deadline success at the effective
   case concurrency, then run representative sustained fleet load and overflow.
3. Reconcile provider receipts, subtract existing cache discounts once, and price
   failures, retries and idle capacity. Require a repeatable net saving.
4. Record the exact served relay revision and scoped candidate env diff. Start
   with a bounded cohort. Roll back by removing the flag or setting it false,
   restarting the affected relay and verifying the served revision and receipts.

A negative cache probe is a reason to retain the default, not to assume savings
from shared prompt bytes.
