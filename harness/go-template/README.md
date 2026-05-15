# DittoBench reference Go harness

This is the in-repo reference miner for the DittoBench subnet. It compiles
to a single static Linux binary that reads `ChallengeRequest` JSON lines on
stdin and writes `MinerResponse` JSON lines on stdout, exactly as specified
in [`ditto/bench/docs/protocol.md`](../../ditto/bench/docs/protocol.md) and
[`ditto/bench/docs/harness_interface.md`](../../ditto/bench/docs/harness_interface.md).

## Quickstart

```sh
# 1. Build the binary locally to confirm everything compiles.
cd harness/go-template
go build ./...

# 2. Build the container image. The Dockerfile expects the build context to
#    be the repository root so the in-tree replace directive in go.mod resolves.
cd ../..
docker build -t ditto-harness:dev -f harness/go-template/Dockerfile .

# 3. Smoke-test the protocol round-trip via the Python runner.
uv run python -m ditto.bench.runner \
  --image ditto-harness:dev \
  --mechanism ditto_core \
  --sample 3 \
  --visibility public \
  --report out/report.json
```

The stub handlers in
[`internal/core/handler.go`](internal/core/handler.go) and
[`internal/retrieval/handler.go`](internal/retrieval/handler.go) return a
`mechanism_unsupported` refusal, so the report records zeros for every case.
That is the expected baseline; you compete by replacing the stubs.

## Where to plug in your code

| File | Replace with |
|------|--------------|
| `internal/core/handler.go` | Your LLM-driven tool-routing loop. Read `req.Prompt`, optional `req.STMContext`, and `req.ToolSchemas`; return the observed `tool_calls` trace. |
| `internal/retrieval/handler.go` | Your memory index. Load the seeded corpus from `DITTO_FIXTURES_PATH` on the first call, then return ranked `evidence_ids` for each `req.Query`. |

The stdio framing loop in
[`cmd/harness/main.go`](cmd/harness/main.go) and the protocol envelope
stamping (`schema_version`, `challenge_id`, `validator_seed`, timing
fields) are reusable — you usually do not need to change them.

## Example I/O

Validators send one JSON object per line on stdin:

```json
{"schema_version":"dittobench/1","challenge_id":"01HXYZ","mechanism":"ditto_core","case_id":"search-memories-basic","prompt":"Remind me of my recipe bookmarks.","validator_seed":"a1b2c3d4e5f60718","issued_at":"2026-05-15T17:00:00Z","deadline_ms":8000}
```

The harness responds with one JSON object per line on stdout, in request order:

```json
{"schema_version":"dittobench/1","challenge_id":"01HXYZ","validator_seed":"a1b2c3d4e5f60718","tool_calls":[{"hop":1,"name":"search_memories","args":"{\"queries\":[\"recipe bookmarks\"]}"}],"started_at":"2026-05-15T17:00:00.123Z","finished_at":"2026-05-15T17:00:00.987Z","total_latency_ms":864,"prompt_tokens":1240,"output_tokens":96}
```

Refusals are scored as zero for the case at hand but never penalise the
other mechanism:

```json
{"schema_version":"dittobench/1","challenge_id":"01HXYZ","validator_seed":"a1b2c3d4e5f60718","refusal":"mechanism_unsupported"}
```

## Docker contract

The validator launches each container with:

```sh
docker run --rm -i \
  --network=none \
  --cpus=2 --memory=4096m \
  --read-only --tmpfs /tmp:rw,size=512m \
  -e DITTO_LLM_ENDPOINT=... \
  -e DITTO_LLM_API_KEY=... \
  -e DITTO_VALIDATOR_HK=... \
  -e DITTO_TEMPO_ID=... \
  --mount type=bind,source=<bundle>,target=/fixtures,readonly \
  ditto-harness:dev
```

The reference image is a `distroless/static:nonroot` runtime with a static
Go binary — no shell, no writable root, ready for `--network=none` and
`--read-only`. See [`Dockerfile`](Dockerfile).

## Anti-gaming and image digest pinning

Validators pin one image digest per miner hotkey per submission window. Any
rebuild — even with no source changes — invalidates the prior commitment
and resets the canary baseline. See
[`ditto/bench/docs/anti_gaming.md`](../../ditto/bench/docs/anti_gaming.md)
for the full control set.
