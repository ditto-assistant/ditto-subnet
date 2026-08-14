# model-relay

Go replacement for the Python platform's `DITTO_ROLE=relay` process: the
SN118 inference plane (`/health`, `/metrics`, `/api/v1/inference/*`). The
platform role (`apps/platform`, Python) is untouched; this binary replaces
only the relay process on the relay hosts.

## Contract anchors

- **Env compatibility**: reads the exact variable names the Python relay
  reads (host `.env` + `.env.deploy`); platform-only variables are tolerated
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
- `internal/server` — `/health`, `/metrics`, and the inference handler
  registration point (`server.InferenceHandlers`).
- `internal/inference` — the inference plane: `POST /api/v1/inference/
  {exchange,chat/completions,embeddings,confirmation/chat/completions,
  confirmation/embeddings}` handlers, the admission
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
- `internal/chain` — Pylon client: `/health` block probe and the
  validator-permit check (`/block/recent/neurons`) used by `/exchange`.
- `internal/postgres` — sqlc layer: hand-written `*_queries.sql`, generated
  `*.sql.go`/`models.go`/`db.go`, hand-written `connection.go`.
- `internal/testutil` — real-Postgres test harness (fresh database per test
  on the monorepo test container at `localhost:15433`; skips when
  unavailable).

## Developing

```bash
make build vet test          # go build/vet/test (pg tests skip without Postgres)
make gen-schema              # re-render db/schema.sql from the Alembic chain
make sqlc-generate           # gen-schema + sqlc (pinned v1.30.0 via go.mod tool)
make sqlc-check              # CI drift check
make release-build           # CGO_ENABLED=0 linux/amd64 binary named model-relay

# Reuse the monorepo test Postgres explicitly:
TEST_POSTGRES_URI="postgres://ditto_test:ditto_test@localhost:15433/postgres?sslmode=disable" go test ./...
```

After editing any `*_queries.sql`, add new files to the `queries:` list in
`internal/postgres/sqlc.yaml` and run `make sqlc-generate`.
