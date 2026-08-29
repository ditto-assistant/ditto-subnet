# Provider outage circuit

OpenRouter overload is a provider-capacity event, not an agent failure. The
model relay owns detection because it sees the provider response, routing
metadata, and receipt boundary. Platform consumes the durable circuit state to
control both scoring and source-review leases.

## Detection contract

The relay opens the `openrouter` circuit only after its bounded in-place retry
sequence is exhausted entirely by canonical, receipt-free HTTP 429 or 503
responses. Timeouts, transport ambiguity, receipt-bearing responses, and
ordinary provider errors do not open the circuit.

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
the inconclusive-expiry cap. Each lease may consume that refund only once per
outage epoch; another resume after the same outage keeps failing uses the
existing finite attempt budget instead of receiving unlimited retries. When
cooldown ends, the first scoring or screening claim atomically becomes the one
half-open probe. All other work remains parked until that probe succeeds and
the relay closes the circuit. A failed probe reopens the cooldown and clears
the probe slot.

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
telemetry only.
