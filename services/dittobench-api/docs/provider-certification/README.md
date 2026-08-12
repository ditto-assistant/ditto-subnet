# OpenRouter provider certification for Qwen3-32B

This directory contains the reusable, local-only certification tooling and
non-sensitive results for comparing OpenRouter providers with DittoBench's
locked Chutes model contract. It does not change the production validator,
assignments, weights, or model lock.

## Model contract

Production validators force all harness chat requests through
`cmd/model-relay` with:

- harness model: `qwen/qwen3-32b`;
- Chutes upstream model: `Qwen/Qwen3-32B-TEE`;
- Chutes endpoint: `https://llm.chutes.ai/v1/chat/completions`;
- thinking: disabled with `chat_template_kwargs.enable_thinking=false`;
- streaming: disabled by the relay.

OpenRouter's current equivalent slug is `qwen/qwen3-32b`. This is the same
open-weight model family, not proof that a provider's serving stack, template,
quantization, or TEE behavior is equivalent to Chutes.

## Inventory and routing

The committed matrix captured OpenRouter's public endpoint inventory at
2026-07-17T19:48:35Z. It found DeepInfra (`deepinfra/fp8`), Nebius
(`nebius/base`), Alibaba (`alibaba`), SiliconFlow (`siliconflow/fp8`), and Groq
(`groq`). Every request was pinned with both `provider.only` and
`provider.order` containing one exact provider slug and
`allow_fallbacks=false`. Automatic routing was never used.

All OpenRouter inventory and inference requests identify the application with
`HTTP-Referer: https://heyditto.ai`, `X-OpenRouter-Title: Ditto`, and the
backward-compatible exact requested header `X-Title: Ditto`, following
OpenRouter's app-attribution contract.

The harness sends both OpenRouter's normalized `reasoning.enabled=false` and
the Chutes-compatible `chat_template_kwargs.enable_thinking=false`. Parameter
strictness stays off because it is a routing filter, not a request-conformance
check; the harness validates the returned shape and behavior directly.

## Scenarios

Each provider receives three repetitions of seven deterministic fixtures:

1. exact plain-text completion with thinking disabled;
2. a specifically forced function call;
3. an automatic function call;
4. two independent parallel calls;
5. a streamed tool call with delta reassembly and usage;
6. a tool-result continuation that must incorporate the result;
7. an error-result continuation that must recover explicitly.

Validation covers HTTP and OpenRouter errors, normalized/native finish reasons,
provider attribution, tool call IDs and names, JSON arguments, usage and cost,
reasoning fields/tokens, streaming reconstruction, time to first token, total
latency, bounded retry behavior, and malformed response handling. Unit tests use
local servers and never need credentials.

## Run locally

Inventory is free and does not require a key:

```sh
go run ./cmd/provider-cert -inventory-only
```

Run the full live matrix (the command reads but never prints the key):

```sh
OPENROUTER_API_KEY=... go run ./cmd/provider-cert \
  -runs 3 \
  -output provider-matrix.json
```

Resume without repeating finished cells, then merge reports:

```sh
OPENROUTER_API_KEY=... go run ./cmd/provider-cert \
  -runs 2 -run-offset 1 -output repeats-2-3.json
go run ./cmd/provider-analyze \
  -output provider-matrix.json smoke.json repeats-2-3.json
```

For a local artifact benchmark, keep the key on the host and expose the pinned
relay to the sandbox exactly as the Chutes relay is exposed:

```sh
OPENROUTER_API_KEY=... go run ./cmd/provider-relay \
  -provider deepinfra -port 11435
```

For secret-manager-backed local qualification, prefer
`PROVIDER_RELAY_API_KEY_FILE` over a process environment value. The relay
accepts only a regular file with no group/other permissions (mode `0600`), and
the file remains on the trusted host; never mount it into the harness container.

The relay forces `qwen/qwen3-32b`, disables thinking and streaming, overwrites
any caller routing fields, disables fallbacks, and replaces the sandbox's
authorization header with the host key. Do not give the key to a harness
container.

When the trusted local validator and untrusted harness run as separate
containers on an isolated network, set
`DITTOBENCH_LOCAL_TOOL_HOST=<validator-network-alias>` on the validator together
with `DITTOBENCH_ALLOW_PRIVATE_HARNESS=1`. This advertises the validator's
observed-tool callback by its internal alias instead of container loopback. It
does not affect production or hosted runs and prevents a local topology defect
from silently capping tool scores.

After collecting sanitized `top3-<provider>-<agent-id>-run<N>.json` score
reports and matching `-relay.json` telemetry files, reproduce the statistical
summary with:

```sh
go run ./cmd/provider-score-analyze \
  -baseline docs/provider-certification/top3-baseline-2026-07-17.json \
  -results /path/to/sanitized-results \
  -expected-runs 3 \
  -output provider-score-summary.json
```

The analyzer validates every run's agent, seed, run size, and generated dataset
SHA-256 against the Chutes baseline; it ranks only matrices with exactly three
runs for each of all three agents. It reports per-agent score deltas, exact
run-level distributions, cross-run variance, per-category changes, cost,
attribution, errors, latency, and effective end-to-end throughput.

Run a bounded, no-retry concurrency ramp against one OpenAI-compatible
endpoint with:

```sh
PROVIDER_LOAD_API_KEY=... go run ./cmd/provider-load \
  -endpoint https://openrouter.ai/api/v1/chat/completions \
  -model qwen/qwen3-32b \
  -provider nebius \
  -concurrency 1,2,4,8,16 \
  -waves 3 \
  -output provider-load.json
```

Omit `-provider` for Chutes and use its exact upstream model identifier. The
command performs no automatic retries, caps concurrency at 64, uses unique
fixed-token prompts to avoid cache-biased ramps, and records every request plus
success rate, 429/5xx/schema errors, p50/p95/p99 latency, requests per second,
completion tokens per second, cost, and provider attribution. Run competing
endpoints sequentially so the client and local network do not contaminate each
other.

## 2026-07-17 compatibility results

The matrix contains 21 observations per provider: seven scenarios times three
repetitions. Classification was identical in all three repetitions.

| Provider | Overall | Tool paths | p50 / p95 latency | Effective output tok/s | Cost | Verdict |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| SiliconFlow | 21/21 | 18/18 | 1498 / 2697 ms | 14.7 | $0.000953 | Only fully conformant endpoint in this matrix; slowest. |
| DeepInfra | 18/21 | 18/18 | 155 / 348 ms | 191.2 | $0.000590 | Fastest conformant tool path; plain text emitted reasoning despite both disable controls. |
| Nebius | 18/21 | 18/18 | 497 / 1047 ms | 68.6 | $0.000785 | Tool-compatible; plain text emitted reasoning and did not return the exact requested text. |
| Alibaba | 18/21 | 15/18 | 643 / 1160 ms | 31.7 | $0.000681 | Stable except it returned one call for every two-call parallel fixture. |
| Groq | 0/21 | 0/18 | 387 / 663 ms | 194.8 | $0.001969 | Incompatible: no advertised tool support, failed/omitted calls, thinking-disable violations, and upstream function-call errors. |

Total measured cost was $0.004977. Latency measures the generalized API
fixtures, not a full 114-case agent run. In particular, Groq was not the fastest
provider by end-to-end latency in this controlled matrix; DeepInfra was faster
and tool-compatible. Effective output throughput is completion tokens divided by
request wall time. Groq's high figure is not useful throughput: those tokens came
from nonconformant reasoning/text responses and every Groq cell failed.

The raw sanitized observations, endpoint metadata, routing configuration,
usage, costs, and errors are in
`results/2026-07-17-qwen3-32b-provider-matrix.json`.

## Top-three benchmark results

The public production API resolved the current top three finalized, registered,
eligible agents and their exact score run IDs, validator hotkeys, artifact
SHA-256 values, seeds, seed blocks/hashes, dataset hashes, and Chutes baselines
at 2026-07-17T19:53Z. The sanitized snapshot is
`top3-baseline-2026-07-17.json`. Artifacts were downloaded only through the
audited Backroom artifact action, hash-verified, inspected before build, and run
without secrets, privilege, host mounts, writable roots, or chain access.

Each provider has three full, exact-seed repetitions for each of all three
agents: 45 valid 114-case runs. The analyzer independently verifies the
generated dataset hash in every report. Chutes was not rerun; its already
recorded production scores are the baseline.

| Provider | Mean absolute Chutes delta | Mean run SD | Relay errors | p50 / p95 relay latency | Valid cost | Decision |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| Nebius | 0.03577 | 0.01166 | 0.32% | 1664 / 7119 ms | $0.8028 | Best close, stable, fully tool-compatible route. |
| Alibaba | 0.03626 | 0.01097 | 0.32% | 1038 / 2839 ms | $0.8144 | Numerically close, but fails every deterministic parallel-tool fixture. |
| DeepInfra | 0.04605 | 0.01198 | 0.50% | 741 / 3497 ms | $0.6320 | Fastest and cheapest fully tool-compatible alternative; emits plain-text reasoning despite disable controls. |
| SiliconFlow | 0.06811 | 0.05736 | 1.83% | 2526 / 9029 ms | $1.0955 | API-conformant but slow and highly agent-dependent; leading-agent SD was 0.14966. |
| Groq | 0.33992 | 0.04430 | 28.86% | 603 / 2732 ms | $1.1321 | Reject for this contract: no tool support and severe agent-score collapse. |

Valid full-run cost was $4.4768; the small generalized API matrix cost
$0.004977. Five excluded runs remain documented in
`results/2026-07-17-excluded-top3-runs.json`: one local callback topology
defect, two invalid-credential 401 runs, and two SiliconFlow cells overlapped by
a separately controlled provider load probe. The latter showed 39 and 10
upstream errors and were replaced exactly once in an exclusive window; their
raw local evidence was preserved rather than overwritten.

Nebius wins the stated Qwen3-32B closeness/stability gate once API compatibility
is mandatory. Alibaba's slightly lower variance does not override its parallel
tool failure. DeepInfra is the preferred speed/cost fallback. A planned live
Chutes-versus-Nebius load ramp was cancelled after the user accepted the
score/compatibility decision; sequential results must not be presented as a
concurrent scalability comparison.

The complete run-level distributions, per-agent and per-category deltas,
provider attribution, usage, costs, errors, and latency are in
`results/2026-07-17-top3-openrouter-summary.json`. Exact production score-time
API commit SHAs are not exposed by the public API, and the local host was arm64;
those remain reproducibility limits.
