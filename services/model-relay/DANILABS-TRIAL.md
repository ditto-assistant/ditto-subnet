# DaniLabs GPT-OSS trial route

The `danilabs_percent` inference board field defaults to zero. It samples
eligible aggregate GPT-OSS-20B chat requests, not all calls or all tokens.
Embeddings, confirmations, decision and coding lanes are unchanged. Requests
with bodies above 8 KiB or output allowances above 8192 tokens remain on the
existing route; this conservative envelope is provisional, not a tokenizer.
The Ditto Router competition lane retains precedence when enabled.

Selection also requires the server-only `DANILABS_API_KEY`, an RFC3339
`DANILABS_FREE_TRIAL_UNTIL` within seven hours, and `DANILABS_TRIAL_SCOPE` set
to `canary` (benchmark canary tickets only) or `fleet`. The deadline must
remain open for the full configured provider timeout. Zero on the board is
the live brake; a missing key, invalid scope or expired deadline also selects
OpenRouter. Older Platform Python relays continue using OpenRouter.

The direct call is single-shot. It does not retry or fall back after dispatch,
since a timeout or rejection may have incurred provider work. Successful usage
is recorded with zero cost under the free-trial assumption, and provider
identity is recorded as `danilabs`; it does not update the OpenRouter health
circuit or route observation. A direct failure does not trip or close that
circuit. Existing route profile and score attribution are not a qualification
of this provider.

Confirmation inference uses its own handler and grant path; it does not enter
this route. Avoid running the bf16 price-order canary and the DaniLabs canary
at the same time, because a split canary population would confound comparisons.

Before enabling any live traffic, validate the actual endpoint with synthetic
requests including tools, reasoning and output formats, response and usage
shapes, cancellation, input limits, latency under concurrency, privacy and
free-trial terms. Compare benchmark canaries and verify score fairness and
provenance. Confirm the exact trial window with the provider. The advertised
32K context includes input and output; this provisional guard is not proof of
the gateway's tokenizer or its template overhead. Deployment and a nonzero
setting do not establish that this qualification has passed.
