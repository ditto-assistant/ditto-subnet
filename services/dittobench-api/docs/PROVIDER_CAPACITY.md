# Provider capacity studies

These commands separate three questions that should not be conflated:

1. Does a model satisfy the benchmark's chat and tool contract?
2. How much public fleet headroom is visible over repeated snapshots?
3. How does a specific route behave under a small controlled load ramp?

Passing a single request is not evidence of capacity. A high model score is also
not evidence of low variance or available inference.

## Offline Chutes capacity score

Capture utilization snapshots separately, then score an explicit public model
set offline:

```bash
go run ./cmd/chutes-capacity \
  -models 'publisher/model-a,publisher/model-b' \
  -output capacity.json \
  snapshot-1.json snapshot-2.json snapshot-3.json
```

Every selected model must occur exactly once in every input snapshot. This
prevents a missing or renamed row from silently receiving zero load.

The score is capacity-first and ranges from 0 to 100:

- 30 points: median active instances plus median scale allowance, normalized
  against the largest selected fleet;
- 5 points: median scale allowance, normalized against the largest allowance;
- 35 points: conservative headroom. For each snapshot, utilization is the
  maximum of current, 5-minute, and 1-hour utilization; the median of those
  maxima is subtracted from one;
- 20 points: log-scaled median completed requests/hour, normalized against the
  largest selected sustained load;
- 10 points: median one-hour completion ratio.

The score is a ranking aid, not a service-level guarantee. Public utilization
is observational, can change quickly, and does not expose account quotas or
provider queue topology.

## Bounded streaming load ramp

`provider-load` sends a deterministic streaming tool-call fixture and records
only sanitized timing, routing, usage, cost, conformance, and error metadata.
It never writes credentials or response content.

For OpenRouter, route by the provider slug accepted by `provider.only`. Record
the inventory endpoint tag separately and add a quantization filter when the
inventory exposes one:

```bash
PROVIDER_LOAD_API_KEY=... go run ./cmd/provider-load \
  -endpoint https://openrouter.ai/api/v1/chat/completions \
  -model publisher/model \
  -provider provider-slug \
  -stream \
  -fixture tool \
  -concurrency 1,2,4,8 \
  -requests-per-level 8 \
  -timeout 90s \
  -output load.json
```

OpenRouter requests always set:

- `HTTP-Referer: https://heyditto.ai`
- `X-OpenRouter-Title: Ditto`
- `X-Title: Ditto` for backward compatibility

The request pins `provider.only` and `provider.order` to the same slug and sets
`allow_fallbacks` to `false`. `require_parameters` defaults to `false` because
OpenRouter endpoint tags and quantization are inventory evidence, but the API's
provider-only pin does not select one of multiple endpoint tags owned by the
same provider. Treat duplicate-provider measurements as provider-level unless
OpenRouter adds an exact endpoint selector.

For a direct OpenAI-compatible endpoint, omit `-provider` and set its catalog
prices when the stream does not return cost:

```bash
PROVIDER_LOAD_API_KEY=... go run ./cmd/provider-load \
  -endpoint https://provider.example/v1/chat/completions \
  -model publisher/model \
  -input-price-per-million 0.10 \
  -output-price-per-million 0.40 \
  -concurrency 1,2,4,8 \
  -requests-per-level 8 \
  -output load.json
```

The command caps concurrency at 64 and fixed requests per level at 100. Reports
include p50/p95/p99 end-to-end latency and time to first streamed event,
completed requests/second, completion tokens/second, cost, completion and tool
conformance rates, reasoning leakage, HTTP 429/5xx counts, timeouts, and other
errors. `SIGINT` and `SIGTERM` produce an interrupted partial report rather than
discarding completed levels.

Run ramps in an agreed exclusive window. Record a utilization snapshot before
and after direct-provider tests, and do not compare routes that overlapped with
another benchmark load without marking the confounder.
