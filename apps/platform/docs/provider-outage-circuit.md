# Provider outage circuit

OpenRouter overload is a provider-capacity event, not an agent failure. The
model relay owns detection because it sees the provider response, routing
metadata, and receipt boundary. Platform consumes the durable circuit state to
control both scoring and source-review leases.

## Detection contract

The relay opens the `openrouter` circuit only after its bounded in-place retry
sequence is exhausted entirely by canonical, receipt-free HTTP 429 or 503
chat responses. Pinned embedding-model backpressure is scoped to the failed
embedding request: it cannot open the provider-wide chat circuit or close an
existing chat outage on an embedding success. Its evaluation remains an
infrastructure failure without a score. Timeouts, transport ambiguity,
receipt-bearing responses, and ordinary provider errors do not open the
chat circuit.

Targon source review calls OpenRouter directly. Its short-lived Platform job
capability may therefore report an exhausted HTTP 429 through the relay's
`/api/v1/inference/source-review/provider-event` route. The relay validates the
capability against the source-review row and remains the only component that
opens or closes the circuit.

The durable row records an outage epoch, cooldown, latest failure, and a single
half-open probe lease. A successful request closes the circuit only when that
request started after the latest recorded failure; a late success from old work
cannot heal a current outage.

## Platform lease behavior

While the circuit is open, Platform has zero inference-dependent lease
capacity:

- issued scoring tickets are expired, their inference grants are revoked, and
  the agent is parked until the circuit cooldown;
- leased or running Targon source reviews are returned to the queue, their job
  capabilities are invalidated, and their temporary rentals are deleted;
- queued source reviews left with a provider resource are also cleaned up, so
  temporary lease count scales down instead of idling paid capacity.

Parking writes the circuit epoch onto the ticket or source-review row. It does
not increment an attempt counter, mint an infrastructure retry grant, or spend
the inconclusive-expiry cap. When cooldown ends, the first scoring or screening
claim atomically becomes the one half-open probe. All other work remains parked
until that probe succeeds and the relay closes the circuit. A failed probe
reopens the cooldown and clears the probe slot.

The parked epoch is a single no-fault resume, and a scoring ticket may consume
it once. A resumed lease that is parked again, in the same or a later epoch, is
charged against the finite attempt budget, so a flapping provider cannot mint
unlimited paid re-leases. That includes a lease an operator grant authorized:
once the ticket has spent its resume, the next park consumes the grant. Source
reviews keep their per-epoch refund.

Validation-retry triage therefore reports `recommended_action: null` instead of
`retry` in two windows (ditto-subnet#2087):

- **while the circuit is open**, for every recoverable below-quorum row. The
  park filters on nothing but the live half-open probe, so a grant spent now is
  parked whatever failed the slot before;
- **after it closes**, for the rows the outage itself parked, until the
  provider has recorded no failure for `PROVIDER_RECOVERY_QUIET_WINDOW` (30
  minutes). The relay cooldown is two minutes, so a still-overloaded provider
  flaps between open, half-open, and closed: one successful half-open probe
  closes the circuit even when the next request re-opens it, and a grant
  landing in that gap is parked again and charged.

The detail and list responses carry `provider_outage_blocks_retry` and the
`provider_outage` circuit snapshot so the wait is explained rather than
inferred. While blocked, a plain grant is refused; a deliberate operator
override must set `acknowledge_provider_outage=true`. Provider outage parking
is infrastructure and never agent-attributable, so it never recommends
withdrawal.

Valid verdicts remain authoritative even if another request opens the circuit
at the same time. An exhausted source-review 429 completion is instead stored
as evidence and re-queued under the current outage epoch before screening can
finalize it as an inconclusive result.

## Rollout and observability

Deploy the Platform migration and Platform API/model-relay release before the
new screener worker. Older workers do not emit source-review provider events;
the endpoint is additive and workers treat notification failure as best effort
so a rolling relay cannot consume a screening attempt.

Backroom's inference runtime metrics include the current provider circuit
snapshot (`state`, `epoch`, cooldown, failure count, and probe ownership). This
is the authoritative operator view; dashboards and process logs are supporting
telemetry only. Backroom must declare `provider_outage` in its response
schema: zod strips undeclared keys, and before ditto-subnet#2087 the MCP tool
silently dropped the snapshot the Platform served. `get_validation_retry` and
`list_stuck_submissions` carry the same circuit row beside their outage-aware
recommendation.
