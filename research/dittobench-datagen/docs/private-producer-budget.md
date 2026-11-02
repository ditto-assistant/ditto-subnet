# Private producer spending guard

Inference requires an explicit `-max-cost-usd` allocation. `-profile-sha` remains
free and does not need one. Allocate from the **remaining total** across all
invocations, not the original approval again on every retry. The native producer
does not own an account-wide or rolling-day quota.

The initial reviewed routes are `openai/gpt-4.1` through `azure` and
`google/gemini-2.5-flash` through `google-vertex`. Other routes fail closed until
their billing bounds are reviewed. Requests remain text-only, strict-schema,
zero-retention-requested and exclusive-provider, with no fallback, plugins or
native web tools. Provider retention compliance still needs operational approval.

Before every outbound request, including retries, the client serializes admission
under a mutex and writes a durable, owner-only `spend.json` checkpoint. It reserves
one input token per serialized request byte plus 4,096 framing tokens and 65,536
output/reasoning tokens, at a provider price ceiling of $20 per million on each
axis. The requested output limit is still 4,096; the larger reservation covers
hidden reasoning and the reviewed Google endpoint's 65,535 completion ceiling.
GPT-4.1 is non-reasoning and uses the requested limit. Current route limits and
pricing must be checked before a paid qualification campaign:

- [Google endpoint metadata](https://openrouter.ai/api/v1/models/google/gemini-2.5-flash/endpoints)
- [GPT-4.1 endpoint metadata](https://openrouter.ai/api/v1/models/openai/gpt-4.1/endpoints)
- [Provider price caps](https://openrouter.ai/docs/guides/routing/provider-selection)

A valid billed-cost receipt reconciles the reservation, rounded up to a
microdollar. Transport errors, cancellations, malformed envelopes and unknown
costs are **not free**: their reservations remain charged. Missing, negative,
nonfinite or over-bound billing receipts stop further admission. Checkpoint
failure also stops admission; no request is sent before its reservation persists.
`reported_usd` alone is not remaining-budget evidence. Subtract
`charged_or_reserved_usd`, including unfinished reservations, from the campaign
allocation before another invocation. Reconcile unknown charges with provider
billing before reclaiming them. Retain a safety buffer for provider/account fees.

Platform workers require `DITTO_PRIVATE_PRODUCER_MAX_COST_USD`, passed to the
native process. The existing queue limits count datasets, not dollars. Deployment
still needs a dedicated account-level quota and explicit total authorization;
this guard does not authorize a recurring worker, qualification, pin or rollout.
