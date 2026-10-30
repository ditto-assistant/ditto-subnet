# model-relay

Go request plane that began as the replacement for the Python platform's
`DITTO_ROLE=relay` process. It serves the SN118 inference plane plus the first
upload strangler slice: `GET /api/v1/upload/eval-pricing` and ordinary
`POST /api/v1/upload/check`. Finalized-payment recovery is forwarded to the
Python platform, and multipart `/api/v1/upload/agent` remains Python until its
chain, storage, fingerprinting, and atomic-commit contracts have parity tests.

## Contract anchors

- **Env compatibility**: reads the exact variable names the Python processes
  read (host `.env` + `.env.deploy`); unused platform variables are tolerated
  and ignored. Missing required values fail boot loudly
  (`internal/config`).
- **Wire compatibility**: request-ID middleware, the
  `{"error_code", "message", "request_id"}` error envelope with the numeric
  codes (3000/3001/3002/4000/41xx), and the decline vocabulary live in
  `internal/relayhttp`. The numeric `error_code` is the authoritative
  discriminator; never change a status/code pairing.
- **Schema ownership**: apps/platform's Alembic chain owns the schema. This
  service only reads/writes existing tables. `db/schema.sql` is a generated
  read-only mirror produced by `scripts/gen-schema.sh` (real Alembic run +
  `pg_dump --schema-only`), consumed by sqlc — never hand-edit it and never
  apply it to production.
- **Lock order** (repo-wide, hot tables): `validator_tickets` →
  `inference_grants` → `inference_requests`. Route/policy rows are locked
  singly, after the hot three. See the doc comments in
  `internal/postgres/*_queries.sql`.

## Layout

- `cmd/model-relay` — entrypoint (config → pgx pool → HTTP server on
  `API_HOST:API_PORT`, graceful shutdown on SIGINT/SIGTERM).
- `internal/config` — env parsing (fails boot on missing/invalid values).
- `internal/relayhttp` — middleware + error envelope.
- `internal/server` — `/health`, `/metrics`, inference, and upload-admission
  handler registration points.
- `internal/inference` — the inference plane: `POST /api/v1/inference/
  {exchange,chat/completions,embeddings,confirmation/chat/completions,
  confirmation/embeddings,coding/chat/completions}` handlers, the admission
  (`begin_inference_request` 17-step gate order, plus the step-13b cost
  ceiling) and settlement
  (`finish_inference_request` + `record_route_observation`) transaction
  orchestration, the OpenRouter/Perplexity provider calls with the bounded
  retry/recovery ladder, sr25519 + Ed25519 verification, and the 5s-TTL
  concurrency-settings resolver. Endpoint-level transaction semantics
  mirror the deployed Python exactly: admission DECLINES roll the admission
  transaction back; settlement always runs (detached from the request
  context, so a client disconnect neither cancels the upstream call nor skips
  accounting). No route streams: `stream: true` is refused legibly and
  upstream responses are fully buffered, sanitized, and re-serialized.
  The coding route is a separate disabled-by-default shadow lane; see
  [`CODING-SHADOW.md`](CODING-SHADOW.md).
  `POST /api/v1/inference/decisions` is the dark decision-model lane; see
  [Decision lane](#decision-lane-dark).
- `internal/chain` — Pylon client: `/health` block probe and the
  validator-permit and registered-owner checks (`/block/recent/neurons`), with
  one-block snapshot caching and single-flight refreshes.
- `internal/upload` — pricing and common pre-payment admission, including
  sr25519 ownership, registration, ban, duplicate, cooldown, and atomic
  reservation checks. Paid recovery is disabled; failures park the ticket for
  an audited Backroom retry.
- `internal/postgres` — sqlc layer: hand-written `*_queries.sql`, generated
  `*.sql.go`/`models.go`/`db.go`, hand-written `connection.go`.
- `internal/traces` — inference trace capture: record schema, disk spool,
  zstd upload to N S3-compatible sinks (presigned SigV4). `internal/tracebackfill`
  exports the historical ledger through the same pipeline
  (`model-relay trace-backfill`).
- `internal/testutil` — real-Postgres test harness (fresh database per test
  on the monorepo test container at `localhost:15433`; skips when
  unavailable).

## Per-run cost ceiling (#3271)

One dollar-denominated ceiling per scoring run, the same whatever mix of
models a harness uses. Unit walls (chat tokens, embedding tokens, request
counts) stay as they are.

**Ledger cost, not the provider bill.** At settlement every request books
`ledger_cost_microusd` = receipted (already clamped) units × the list price
**stamped on the grant at mint**, onto the request row and added to the
grant's total:

- chat: `ledger_prompt_price_per_token` / `ledger_completion_price_per_token`
  (USD per token). Mint stamps the bound route's prices; the aggregate route
  has none, so mint stamps the median over the model's priced, non-offline
  discovered provider endpoints. A grant minted with no list price at all
  falls back to the trusted provider bill so the ceiling still binds.
- embeddings: `embedding_price_microusd_per_token`, default `0.004`
  ($0.004 / 1M input tokens). It replaces the constant the relay hard-coded,
  so the embedding `cost_microusd` is unchanged at the default stamp.

`cost_microusd` / `embedding_cost_microusd` remain the provider bill.
Missing usage books zero; untrusted usage is priced at the clamp; stale
reclamation and rotation cancel with zero ledger cost. A future lane joins
the ceiling purely by adding to `inference_grants.ledger_cost_microusd` at its
settlement. The dark decision lane does exactly that: its settlement adds the
same input-token × `decision_input_microusd_per_mtok` figure it books as
`decision_cost_microusd` to `ledger_cost_microusd`, and its admission runs the
same step-8/13b checks, so a ceiling reached on any lane refuses chat,
embeddings and decisions alike with 4110.

**Knobs** live on the inference concurrency board
(`InferenceConcurrencySettings`, written through Backroom with revision,
actor, reason and checksum): `run_cost_ceiling_microusd` (null = none, max
$1,000), `cost_ceiling_mode` (`off` | `shadow` | `enforce`, default `off`;
a live mode requires a ceiling) and `case_cost_report_microusd` (report-only,
never an admission input). Mint stamps `cost_ceiling_microusd` and
`cost_ceiling_mode` onto the grant only when the mode is `shadow` or
`enforce`, so a revision never changes a live run and "a ceiling is stamped"
means "the ceiling is live".

**Admission** (step 13b, after the token wall), from settled ledger cost
only — the call that crosses the ceiling finishes and is billed:

| Mode | `ledger_cost_microusd >= ceiling` |
|---|---|
| `off`, or null ceiling | nothing; byte-identical to a grant minted before #3271 |
| `shadow` | admit; `cost_ceiling_would_decline += 1` on the grant and `ditto_inference_cost_ceiling_would_decline_total{lane}` |
| `enforce` | decline **4110 `COST_CEILING_EXHAUSTED`** (HTTP 429, terminal) on every lane |

The settle transaction is the committed writer of `status = 'exhausted'`
once settled ledger cost meets an enforced ceiling (on whichever lane
crossed it). Step 8 checks the cost wall first, so every later chat,
embedding **and** decision call is 4110, not 4102/4104.

**Wire evidence** (all absent unless the grant has a live ceiling):

- `/exchange` body `cost_ceiling_microusd` (integer) and `cost_ceiling_mode`
  (`shadow` | `enforce`), plus fallback headers
  `X-Ditto-Cost-Ceiling-Microusd` / `X-Ditto-Cost-Ceiling-Mode`.
- Every authenticated proxied response on every lane — successes, provider
  failures, non-deliverable 409s and terminal 429 declines — carries
  `X-Ditto-Ledger-Cost-Microusd: <decimal>`: the grant's total after this
  request's settlement, or the current settled total on a decline. The
  anonymous 4100 never carries it.

**Rollout order.** (1) Ship the scorer first: `declineFaultFor[4110] = agent`
only when its own header evidence shows spent ≥ ceiling. (2) Run `shadow`
until Backroom shows the validator fleet runs a 4110-aware scorer, and size
the ceiling from the ledger-cost distribution and `would_decline` counts.
(3) Only then switch to `enforce`. An old broker treats an unknown 4110 as
platform fault and would re-lease the run indefinitely.

## Decision lane (dark)

`POST /api/v1/inference/decisions` brokers a decision model (TypeSafe Jev)
through OpenRouter's System One endpoint,
`POST https://openrouter.ai/api/v1/systemone`, with the existing
`OPENROUTER_API_KEY`. No other credential exists for it.
`DITTO_DECISION_UPSTREAM_URL` may override the URL, but only to that same
pinned OpenRouter path (it is a credential boundary like the chat and
embedding URLs).

The lane is off until the operator turns on `decision_lane` on the inference
concurrency board (Backroom `set_inference_concurrency_settings`) with
`enabled: true` and an exact `model`, e.g. `typesafe/jev-1.14` (never
`~typesafe/jev-latest`). While it is off, the route answers the same 404 an
unmounted path does, and grants are minted with no decision pin. The board
also sets the stamped price (`input_microusd_per_mtok`, default 42000 =
$0.042 per million input tokens), the per-run `request_budget` and
`token_budget`, and the live concurrency and requests-per-minute rails. The
model, price and walls are stamped onto each grant at mint, and `enabled` is
a live kill switch.

The body is the System One body (`{state, questions, assets?}`) passed
through nearly verbatim. Harnesses may use any question type the provider
supports (choice, score, noul, box), as many questions as the provider
allows, and any state. The relay forces `model` to the grant's pin. It
refuses only the fields it owns (`provider`, `session_id`, `user`,
`trace`). It answers
`{model, answers, usage: {input_tokens, output_tokens}}`, with `answers`
passed through verbatim.

The relay's controls are:

- **Pin verification.** A response served by a model other than the pin
  (or a dated snapshot of it, such as `typesafe/jev-1.13-20260917`) is an
  uncharged infrastructure failure (502, `decision_model_mismatch`).
- **Metering.** Receipted `usage.input_tokens` × the stamped price is booked
  as `decision_cost_microusd`, which is ledger cost, never the provider
  bill. Output is free.
- **Walls.** The lane has its own request and token walls (4102/4104). They
  never mark the grant exhausted, so chat survives a spent decision wall.
  The run's cost ceiling is different: decision spend counts toward it, and
  an enforced ceiling refuses every lane with 4110.
- **Outages.** These use the existing 502/503/504 shapes, which the scorer
  maps to the no-fault `model_relay_unavailable`.
- **Provider rejections.** A provider 400/422 on the harness's own questions
  is returned as an uncharged 400.

No new decline or infra codes are introduced. The lane carries no score.

## Inference trace capture (`internal/traces`)

Every call the relay brokers is the training data DittoBench produces, and
until this package existed none of it was persisted: `inference_requests` is
metadata only (and load-bearing for admission, replay protection and
accounting, so it stays short-retention). With `INFERENCE_TRACE_ENABLED=true`
each settled call — and each authenticated call the gate declines — becomes one
JSONL record (`ditto.inference.trace.v1`): the miner's request body as
received, the locked payload sent upstream, every provider phase's raw answer
and headers, the sanitized response returned, usage, timing, and the grant
context (agent, bench version, validator, slot, route). Records are appended to
per-stream spool files on local disk (`INFERENCE_TRACE_SPOOL_DIR`), rotated by
size/age, zstd-compressed and PUT to every configured sink under

```
traces/v1/lane=<inference|confirmation>/kind=<chat|embedding>/dt=YYYY-MM-DD/hour=HH/<relay>-<first>-<last>-<id>.jsonl.zst
```

A successful upstream result is not proof of delivery: inspect
`response.http_status` and `response.deliverable` alongside `outcome.status`.
When the admission succeeded but the settlement transaction failed, the relay
returns HTTP 500 and includes a private `settlement_failure` diagnostic in the
trace. Its `kind` distinguishes PostgreSQL, deadline, cancellation, and other
internal errors; PostgreSQL errors include a validated five-character `sqlstate`.
It contains no exception message, SQL, query arguments, or connection string.
Use Backroom `peek_inference_trace` with `includeBodies=true` to inspect it. Chat
settlement log entries also carry the request ID, grant ID, and nonce so operators
can correlate the exact database exception. This diagnostic survives transaction
rollback because the trace spool is independent of the database.

The scorer counts chat HTTP 500 responses from its authenticated Platform
transport as `platform_internal_failures`. A later successful call does not make
such a run scoreable: finalization reports the existing
`validator_infrastructure / model_relay_unavailable` failure. This works with old
relays without new response headers. Ordinary provider 502/504 and miner-owned
structured-output failures retain their existing recovery semantics. The change
does not replay paid requests, change numeric scoring, or invalidate previously
accepted scores. Roll out the scorer guard and relay diagnostics, establish a
healthy route, then use a separately reviewed recovery for affected historical
samples; canonical-score replacement is not continual-retest replacement.

Sinks are S3-compatible buckets addressed by presigned SigV4 URLs (Hippius
rejects header-signed PUTs; Backblaze B2 and AWS accept both).
`INFERENCE_TRACE_SINKS=hippius,backblaze` names them; the `hippius` sink
inherits the Platform's `HIPPIUS_*` credentials and defaults its bucket to
`ditto-subnet-traces`; every other sink is configured with
`INFERENCE_TRACE_SINK_<NAME>_{ENDPOINT,REGION,BUCKET,ACCESS_KEY_ID,SECRET_ACCESS_KEY,REQUIRED,PREFIX}`.
Each record also carries the broker's `X-Ditto-Trace-Context` (`request.context`,
with `run_id`/`case_id` lifted): which run, agent, slot and benchmark case the
call served — exact for serial runs and confirmation windows, a candidate set
(`cases_in_flight`) under concurrent `/run` unless the harness claimed a case
with `X-Ditto-Case-Id` (see dittobench-api PROTOCOL.md). Check
`context.case_verified` before trusting `case_id`; the header is advisory and
is dropped (never a 4xx) when oversized or not a JSON object.
A file leaves the disk only when every *required* sink holds it; per-sink
completion lives in a sidecar so a sink outage never re-sends to the others
and a restart resumes where it stopped. Capture never blocks an inference
call: a full queue or a spool over `INFERENCE_TRACE_MAX_SPOOL_BYTES` drops and
counts (`ditto_inference_trace_dropped_total{reason}`); the other families are
`ditto_inference_trace_{records,rotations,uploads,upload_failures,files_released}_total`
and `ditto_inference_trace_spool_bytes`. The bucket MUST stay private: traces
carry benchmark case text and miners' agent prompts.

### Backfilling the historical ledger

`model-relay trace-backfill` walks `inference_requests` and
`confirmation_inference_requests` (joined to their grants) in
`(started_at, grant_id, nonce)` order and ships them through the same sinks
under `ledger/v1/lane=/kind=/dt=/hour=` (one UTC day per object). The records
are `ledger.backfill` events with no bodies (none were ever stored). It reads
the relay's environment for Postgres and the sinks, keeps a cursor file, and is
safe to re-run or interrupt:

```sh
set -a; . /opt/ditto-subnet/apps/platform/.env; set +a
/opt/ditto-platform-relay/releases/<sha>/model-relay trace-backfill \
  --spool-dir /opt/ditto-platform-relay/trace-backfill \
  --until 2026-08-21T00:00:00Z --batch-rows 5000
```

`--delete` removes the exported rows afterwards — only inside the key ranges
this run shipped, only after every required sink confirmed them, and never a
row younger than `--retain-hours` (floor 24), still `started`, or under an
unexpired grant. Deletion is deliberately opt-in; the live ledger's retention
policy is a separate decision.

## Developing

```bash
make build vet test          # go build/vet/test (pg tests skip without Postgres)
make gen-schema              # re-render db/schema.sql from the Alembic chain
make sqlc-generate           # gen-schema + sqlc (pinned v1.30.0 via go.mod tool)
make sqlc-check              # CI drift check
make release-build           # CGO_ENABLED=0 linux/amd64 binary named model-relay
go run ./cmd/pprofctl list --probe  # inspect deployed loopback pprof listeners

# Reuse the monorepo test Postgres explicitly:
TEST_POSTGRES_URI="postgres://ditto_test:ditto_test@localhost:15433/postgres?sslmode=disable" go test ./...
```

See [`../../docs/PERFORMANCE-PROFILING.md`](../../docs/PERFORMANCE-PROFILING.md)
for CPU, heap, goroutine, diff, and Python sampling workflows.

After editing any `*_queries.sql`, add new files to the `queries:` list in
`internal/postgres/sqlc.yaml` and run `make sqlc-generate`.

## Upload activation and rollback

The binary exposes the two upload-admission routes as soon as it starts, but
Caddy keeps them on Python while
`platform_upload_admission_relay_enabled: false`. Activate only after both
relay slots report the intended release SHA from `/health`, then set the flag
true and converge the `platform_app` role. Roll back only this slice by setting
the flag false and reconverging; inference routing is unaffected.

## Ditto Router upstream (dogfood, off by default)

The relay can forward a miner's chat inference to **Ditto's own Router** instead
of OpenRouter, for miners who linked a Ditto account (`miner_ditto_links`,
written by apps/platform's Sign in with Ditto flow). Attribution rides on
`X-Ditto-On-Behalf-Of: <ditto_user_id>` read from the link row, never from the
request, and the Router re-checks that user's own `credits:spend` consent (a
402 comes back as `ditto_router_payment_required`, distinct from an outage,
and is never retried). Everything else — unlinked miners, disabled lanes,
lookup errors — takes the centralized OpenRouter path byte for byte.

| Env | Default | Meaning |
| --- | --- | --- |
| `DITTO_ROUTER_UPSTREAM_ENABLED` | `false` | Master switch. |
| `DITTO_ROUTER_UPSTREAM_URL` | `https://inference.heyditto.ai/v1/chat/completions` | Pinned to Ditto inference hosts (`router.heyditto.ai`, its `inference.heyditto.ai` compatibility host, staging, `pr-N`/`be-N` previews) and to the exact `/v1/chat/completions` path; anything else fails boot. |
| `DITTO_ROUTER_API_KEY` | — | Owner-minted key on the DittoBench app endpoint. Required when enabled. |
| `DITTO_ROUTER_LANES` | *(empty)* | csv of `screener`, `competition`. Empty routes nothing. |
| `DITTO_ROUTER_COMPETITION_ACK` | — | Must be `memories-off-locked-model` before `competition` is accepted. |
| `DITTO_ROUTER_ON_BEHALF_OF_HEADER` | `X-Ditto-On-Behalf-Of` | Header the Router reads the user id from. |

Two Router endpoints back the two lanes, and the difference is the point:

- **`dittobench-screener`** — memories **on** (`recall_enabled`, the screener's
  KG), used by screener-side source review. Not wired in this service yet;
  the screener worker has its own provider client.
- **`dittobench-competition`** — memories **off**, `record_enabled` off, model
  **locked** to the benchmark's pinned model, `model_mode` fixed. The scored
  benchmark must measure the miner's agent, not Ditto's memory, so the ack
  is required and the endpoint posture is part of the bench contract.

Live parity (same model, same usage accounting, receipts on the Ditto side)
must be shown on a canary before `competition` is enabled in production.
