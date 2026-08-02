# Hermes Agent on DittoBench v6

This directory records four retained full DittoBench v6 measurements of Nous
Research Hermes Agent on the same seed and generated dataset:

- the original controlled baseline in
  [`results/2026-07-22-hermes-openrouter-v6.json`](results/2026-07-22-hermes-openrouter-v6.json);
- a separately labeled Hermes-favorable replay in
  [`results/2026-07-22-hermes-favorable-openrouter-v6.json`](results/2026-07-22-hermes-favorable-openrouter-v6.json);
- a same-model native-session run in
  [`results/2026-07-22-hermes-native-session-qwen-openrouter-v6.json`](results/2026-07-22-hermes-native-session-qwen-openrouter-v6.json);
- a stronger-model native-session upper bound in
  [`results/2026-07-22-hermes-native-session-claude-openrouter-v6.json`](results/2026-07-22-hermes-native-session-claude-openrouter-v6.json).

The adapter is under [`integrations/hermes`](../../integrations/hermes/README.md).
The first result remains unchanged.

The later same-model, pinned-provider v2-v6 matrix is recorded separately in
[`docs/third-party-benchmark-timeline`](../third-party-benchmark-timeline/README.md).
It preserves every result above and adds historical-contract measurements
rather than replacing the original v6 evidence.

## Native Hermes follow-up

The original profiles expose DittoBench's memory tool names as aliases over
Hermes' native FTS handler. That preserves the retrieval engine, but it is not
the cleanest answer to “how does Hermes itself use memory?” The new
`native-session` condition instead:

- imports every benchmark conversation verbatim as a native Hermes session;
- exposes Hermes' upstream `session_search` name, schema, and result shape;
- preserves Hermes' normal evaluation-session persistence;
- removes all seven Ditto memory read/write names from the model's tool list;
- retains the actual `session_search` calls in the transcript;
- adds no skill, answer router, embedding model, reranker, graph, or external
  memory provider.

| Condition | Model | Native memory trace | Memory mean | Correct |
| --- | --- | --- | ---: | ---: |
| Original alias baseline | Qwen 3 32B | Ditto aliases over SessionDB | 0.239583 | 23/96 |
| Alias favorable | Qwen 3 32B | Ditto aliases over SessionDB | 0.260417 | 25/96 |
| Native session | Qwen 3 32B | Hermes `session_search` | 0.229167 | 22/96 |
| Native session, stronger model | Claude Sonnet 4.6 | Hermes `session_search` | **0.302083** | **29/96** |

The same-model native result is the closest model-controlled comparison. The
Claude result is intentionally a generous upper bound: it improves native
Hermes by seven correct cases over native Qwen and four over the prior
alias-favorable result, but it does **not** share the miners' locked model and
must not be used as a causal model-controlled comparison. Its memory subscore
is suitable for a clearly labeled off-network reference chart; its Ditto tool
score and composite are not, because Hermes emits native `session_search`
rather than the benchmark's expected memory tool names.

The trace explains part of the gap. Native Qwen called `session_search` in 45
of 96 memory cases; Claude did so in 63. Neither condition solved Hermes'
missing subject graph or temporal/multi-hop retrieval semantics. Both scored
zero on isolation, multi-session, temporal-depth, multi-hop, and multi-query
recall. Claude improved single-session recall from 1/15 to 4/15 and lifecycle
write/read from 1/5 to 2/5.

At the public snapshot generated `2026-07-22T20:10:12Z`, the strongest
finalized v6 miner memory subscore was 0.947917, 3.14 times the Claude-driven
Hermes upper bound. This is observational context: miner scores are
three-validator production medians with independent provider routes and seeds.

## Paired result

| Metric | Baseline | Hermes-favorable | Delta |
| --- | ---: | ---: | ---: |
| Composite | 0.241189 | **0.246898** | +0.005709 |
| Tool mean | 0.862424 | **0.884242** | +0.021818 |
| Memory mean | 0.239583 | **0.260417** | +0.020833 |
| Composite standard error | 0.011398 | 0.011159 | -0.000239 |
| Median case latency | 8,410 ms | 7,052 ms | -1,358 ms |
| Metamorphic consistency | 0.818182 | 0.727273 | -0.090909 |
| Tool efficiency | 1.0 | 0.999537 | -0.000463 |
| Conversational sanity | 0.0 | 0.0 | 0.0 |

Both runs used all 206 cases generated from seed `3058240546919425205`.
Their dataset SHA-256 is identical. The favorable profile produced five memory
case improvements and three regressions, for a net gain of two correct cases
out of 96. Single-session recall improved from 0.20 to 0.333333 and the canary
from 0 to 1; multi-query recall regressed from 0.50 to 0. The other memory
categories had no net change.

This is useful negative evidence against the claim that the original result was
mostly an artificially tight adapter configuration. The boost helped, but it
did not repair Hermes' missing graph, temporal, lifecycle, or defensive recall
semantics: temporal depth, multi-hop relations, multi-session recall, isolation
recall, injection resistance, and declarative write/read remained at 0. The
favorable memory mean was still below the 0.50 minimum in the recorded
20-miner public snapshot; the 0.9375 leader remained 3.6 times higher.

It is still one stochastic replay, not a confidence interval over repeated
model samples. The baseline relay recorded four upstream errors and four
timeout/no-response cases, while the favorable replay recorded no upstream
errors and one timeout/no-response case. The tool delta in particular should
not be attributed solely to the profile. Additional repeated paired runs would
be required to isolate sampling and transport variance.

## What the favorable profile changes

The `favorable` profile uses three general Hermes-facing changes:

- Hermes' documented default 90 agent-loop iterations instead of eight;
- the native `session_search` ceiling of 10 results instead of five;
- generic recall guidance adapted to the benchmark's memory-tool aliases.

It does **not** contain expected answers, case IDs, benchmark categories,
answer extraction, query rewriting, embeddings, reranking, a graph index, or a
benchmark-specific skill. Qwen still chose every tool name and argument through
the ordinary Hermes agent loop. Both profiles used the same model, disabled
thinking, non-streaming relay shape, public tool catalog, observed tool
endpoint, container sandbox, seed, and deterministic grader.

The original profile requested 20 results, but Hermes 0.19.0 clamps discovery
limits to 10. The committed result metadata now records both values; the score
itself is unchanged.

This is deliberately a generous **native SessionDB** profile, not a claim to
cover every third-party memory provider Hermes supports. Provider-assisted
variants should be reported as separate products because they introduce a new
retrieval backend, credentials, and data-processing behavior.

## Exact contract

Shared contract:

- DittoBench datagen: `55343ff961e36ebaa1c7db378dd7e5e0663ff5f3`
- Hermes Agent: `e57918ac800121cf9c2956fe55e27df3ea80b562`
  (`0.19.0`)
- benchmark: v6, `full`, 206 cases (110 tool and 96 memory)
- seed: `3058240546919425205`
- dataset SHA-256: `f9b8e6eea10f20316a6fc82c2f6b0af0f0cf0bc3f263cf96a760e68cdaa3ea2d`
- model: `qwen/qwen3-32b`, thinking disabled, non-streaming
- route: OpenRouter automatic routing

Baseline:

- DittoBench API: `a0a0bca0460620cb5dcd381b1802a04567a6c762`
- adapter source: `d952e0a9e38df9898bfa8d6ce4b9df5f59c7a61a`
- run: `712a8c3c-860c-4962-ac07-0d2e83a57b59`
- transcript SHA-256: `0e263ad754b7fdec35d196d44d3cfcf571c7b2a2f1465a25413d6a9d1c30b00b`
- providers: 404 SiliconFlow responses and one DeepInfra response
- relay: 409 requests, 2,001,492 tokens, four upstream errors, $0.286825
- elapsed: 11m33s

Hermes-favorable:

- DittoBench API and adapter profile: `609a01e435f53dea0922d5cc99c45f1cc1eec6da`
- run: `745a769b-0183-40f4-912b-155ac79dc1c5`
- transcript SHA-256: `bc74b821ad41e72e500d5aa4d8fd8d4d2ea79743599b03cb10202dd5eed9b821`
- provider: 439 SiliconFlow responses
- relay: 439 requests, 2,224,942 tokens, no upstream errors, $0.318771
- elapsed: 7m43s

Native session, same-model:

- adapter implementation: `b89b1bd2bc3e62a4dea65768774bcbeec70925a2`
- DittoBench API engine: `8df6ba37a3c4e46ede9db1cb6014551d49ac6ba8`
- run: `1e0acc3c-23e3-41e8-bb6f-805612d276e1`
- model: `qwen/qwen3-32b`; provider: 379 SiliconFlow responses
- transcript SHA-256: `0a0dba9720be31eb399c0ecfd03d022eac2f5d663e9cc4929076feed6f46522b`
- relay: 379 requests, 2,144,771 tokens, no upstream/API/HTTP errors,
  $0.307092
- elapsed: 7m08s

Native session, stronger-model upper bound:

- adapter implementation: `b89b1bd2bc3e62a4dea65768774bcbeec70925a2`
- DittoBench API engine: `8df6ba37a3c4e46ede9db1cb6014551d49ac6ba8`
- run: `b171417a-9d45-415e-8573-de2f06e82dba`
- model: `anthropic/claude-sonnet-4.6`; provider: 481 Anthropic responses
- transcript SHA-256: `09dc8b3335bca96dfac5de933d012c059c9dfd9e346ccd88de030604f6968130`
- relay: 481 requests, 3,079,876 tokens, no upstream/API/HTTP errors,
  $9.982308
- elapsed: 8m36s

The practice report's built-in model metadata remains the benchmark's Qwen
lock label even for the explicit Claude experiment. The Claude evidence record
therefore binds the actual model independently through both the adapter model
setting and the host relay's forced upstream model. This exception is why that
run is labeled an upper bound rather than a matched-model result.

The host-side relay kept the OpenRouter key out of both containers. Practice
scope cannot bind trusted relay accounting into the score, so the token
efficiency multiplier remained neutral. Memory accuracy is deterministically
graded and unaffected by that neutral multiplier.

## Adapter boundary

Hermes documents two native memory layers: small curated `MEMORY.md`/`USER.md`
files and full session history searched through SQLite FTS5. Importing hundreds
of benchmark pairs into the curated files would evaluate an adapter-written
summarizer rather than Hermes. Both profiles therefore:

- import each benchmark conversation as a native Hermes session;
- map benchmark memory-read aliases to native `session_search`;
- use one physical Hermes database per benchmark user;
- forward every non-memory tool to the validator-observed endpoint;
- keep evaluation questions out of seeded history;
- accept subjects and links without inventing graph support;
- do not reinterpret lifecycle tool calls as edits to imported history.

Claims should say “Hermes Agent 0.19.0 native SessionDB through this adapter,”
not “all possible Hermes memory providers.”

## Validation and excluded attempt

Validation included ten adapter unit tests, an unprivileged read-only Docker
smoke, native FTS cross-user isolation, live Qwen tool continuation through the
observed callback, and complete repository Go tests. The favorable run used the
same dataset hash as the baseline, and the committed aggregates were checked
against the retained local run, transcript, relay, and dataset artifacts.

Two earlier runs remain excluded. `6967bd8c-3a76-4a42-9c84-49153dc708df`
was cancelled after exposing adapter transport defects that were fixed before
the published alias measurements. `e1c5baed-5fcf-40cf-90fd-60ed27187574`
completed, but the local validator had advertised its observed-tool endpoint
as container-local loopback. Its reachability preflight failed, so its 0.15625
memory result is not used or published as a retained measurement. The raw
anti-cheat datasets and transcripts are
not committed; content hashes bind the retained local evidence without
publishing fresh evaluation material.
