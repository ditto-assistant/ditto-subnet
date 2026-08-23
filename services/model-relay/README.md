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
  (`begin_inference_request` 17-step gate order) and settlement
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
