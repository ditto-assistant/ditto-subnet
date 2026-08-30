# No-automatic-retry policy

SN118 cost-bearing work is fail-once. A provider, transport, contract,
screening, scoring, or confirmation failure parks the enclosing attempt. The
system must not issue another paid request, choose a fallback provider, reclaim
the failed row, or grant another validator lease until an operator authorizes
that retry through Backroom.

The hosted chat relay has one narrow in-request exception: it may repeat once
after a strict receipt-free 429 or generic HTTP 502. The proof rejects malformed
or additive bodies and any completion, provider, usage, cost, or receipt field.
Known generated-output 502 envelopes remain miner-owned and are returned with
their private recovery class instead of being replayed by the relay. The retry
stays inside the same signed inference request, grant, lease, and cumulative
budget lineage; it never creates a new scoring ticket or provider fallback.

This policy applies even when the failure is classified as infrastructure and
even when an identical request would be idempotent. Infrastructure
classification remains evidence; it is not retry authority.

## Enforced paths

| Process | Automatic behavior after failure | Manual Backroom control |
| --- | --- | --- |
| Screening attempt, including image build, runtime smoke, source review, L2/critic/adjudication, verdict delivery, and deferred ATH deep review | The exact attempt becomes terminal and is not claimable again | `retry_failed_screening_now` authorizes the exact latest terminal attempt; its confirmed `forceFullReview` option forces only that retry through the full review lane without changing global admission policy; `expire_running_screening` first parks a stuck live attempt |
| Rejected or quarantined screening | Remains terminal | `rescreen_rejected_submission`, `resolve_screening_quarantine`, and the guarded batch quarantine tools authorize a new attempt |
| Trusted screener image build | `failed`, `fallback_required`, and `canceled` rows remain parked | `get_screener_capacity` supplies the build/status/attempt guards; `retry_trusted_image_build` requeues that exact build and appends an audit event |
| Screening compute provider | Only the first configured provider is authoritative; no Targon/GCP or model failover occurs | `get_screener_capacity` and the screener-provider settings controls select the one provider before a manual retry |
| Validator scoring ticket | The first failed/expired lease exhausts its base budget; infrastructure grants do not wake it | `get_validation_retry`, `retry_validator_evaluation`, and `batch_retry_validator_evaluation` grant exactly one future lease per selected exhausted ticket |
| Hosted chat inference | One provider dispatch plus at most one same-route replay for a strict receipt-free 429 or generic HTTP 502; generated-output 502s are returned to the miner; all ambiguous failures and fallbacks remain single-shot | Retry the enclosing validator evaluation only after the bounded in-request recovery is exhausted |
| Hosted embedding inference | One provider dispatch, fallbacks disabled; any status, malformed response, timeout, transport error, or capacity rejection parks the score attempt | Retry the enclosing validator evaluation; an individual embedding request is never replayed independently |
| Harness `/run`, LongMem seed/run, provider certification, and model relay | One dispatch; failures are returned as terminal evidence | Retry the enclosing validator evaluation or explicitly rerun the operator-owned certification job |
| Score submission and score replacement | One delivery; a failed replacement request releases its claim without another automatic attempt | `queue_validator_score_retests` creates a new audited replacement request; canonical scoring uses the validator retry controls |
| Bench v9 confirmation and confirmation ablation | One confirmation attempt and one attempt per case | `authorize_confirmation_bundle_retest` creates the audited retest authority |

Manual controls use current-state guards (artifact digest, terminal attempt ID,
ticket snapshot, build status and attempt count, or confirmation evidence) so a
stale operator request cannot authorize newly changed work. Attempt history is
append-only. Trusted-build attempt counts have no manual ceiling; the database
keeps only the nonnegative invariant.

## Deliberate non-retry loops

The following recurring activity does not repeat failed cost-bearing work:

- polling observes a provider job, rental, or benchmark run that was already
  created; it does not create or dispatch another one;
- a shared capacity gate may delay a request before its first dispatch based on
  a sibling's prior saturation signal; the request itself is dispatched once;
- best-effort deletion and reconciliation inspect or remove an already-created
  resource so an ambiguous response cannot create a duplicate paid resource;
- daemon sweeps continue to discover fresh queued work, but terminal screening,
  build, scoring, and confirmation rows are excluded until manual authority is
  recorded;
- enrollment/bootstrap, telemetry export, public-cache refresh, price-oracle
  reads, deployment migration locks, and chain weight publication are outside
  screening/scoring attempt authority. They must not create a second provider,
  model, harness, screening, or scoring attempt.

Any new retry beyond the hosted-chat exception above, fallback, alternate
provider phase, lease compensation, or failed-row reclaim in a cost-bearing
path requires a Backroom read surface, an audited manual write with concurrency
guards, and a regression test proving the failure is otherwise single-shot.
