# DittoBench harness interface

A miner submission is a single OCI image whose entry point reads
`ChallengeRequest` JSON lines from stdin and writes `MinerResponse` JSON
lines to stdout, one per challenge, in order. This document specifies the
Go interfaces miners are expected to implement, the Docker contract the
image must satisfy, and the budget enforcement the validator applies.

The reference skeleton lives in-repo at
[`../../../harness/go-template/`](../../../harness/go-template). It ships a
stdio framing loop, JSON encoding/decoding, and stubbed implementations of
both interfaces so miners can `go build` and `docker run` within minutes —
see [`harness/go-template/README.md`](../../../harness/go-template/README.md).

## Go interfaces

Miners implement one or both interfaces and wire them into a small `main`
that drives the stdio loop. Both interfaces use the canonical JSON shapes
defined in [`../schemas/`](../schemas).

```go
// CoreHarness handles ditto_core (Mechanism 0) challenges.
//
// Implementations own their own LLM client, tool dispatcher, and any
// internal caching. The validator does NOT run the Ditto tool surface for
// them; the harness must execute or simulate tool calls and return the
// observed trace in MinerResponse.ToolCalls.
type CoreHarness interface {
    HandleCore(ctx context.Context, req CoreChallenge) (MinerResponse, error)
}

// RetrievalHarness handles ditto_retrieval (Mechanism 1) challenges.
//
// Implementations own their own memory store, embedding model, and
// retriever. The fixture user identified by req.UserFixtureID has a
// validator-provided seeded corpus mounted into the container (see the
// "Filesystem and seeded fixtures" section below).
type RetrievalHarness interface {
    HandleRetrieval(ctx context.Context, req RetrievalChallenge) (MinerResponse, error)
}
```

`CoreChallenge` and `RetrievalChallenge` are typed views of the same
on-the-wire `ChallengeRequest` envelope; the harness template provides
constructors that pick the right view from a raw request based on the
`mechanism` field.

A miner that implements only one mechanism returns a `Refusal` from the
other handler:

```go
return MinerResponse{Refusal: "mechanism_unsupported"}, nil
```

The validator scores refusals as zero for the case at hand but does not
penalise the miner's other mechanism.

## Process entry point

A correct entry point looks like:

```go
func main() {
    h := myHarness{} // implements CoreHarness, RetrievalHarness, or both
    scanner := bufio.NewScanner(os.Stdin)
    scanner.Buffer(make([]byte, 0, 64*1024), 4*1024*1024)
    enc := json.NewEncoder(os.Stdout)
    for scanner.Scan() {
        var req ChallengeRequest
        if err := json.Unmarshal(scanner.Bytes(), &req); err != nil {
            // protocol violation: log to stderr and exit non-zero
            os.Exit(2)
        }
        resp := dispatch(h, req)
        _ = enc.Encode(resp)
    }
}
```

The reference template provides this loop verbatim plus context propagation
that respects the validator's `deadline_ms`.

### I/O framing rules (recap)

- One JSON object per line on stdin and stdout, terminated by `\n`.
- Exactly one response per request, in request order.
- Stdout is reserved for protocol responses; **all logging goes to stderr**.
- Flush stdout after each response (`json.Encoder.Encode` does this in Go).
- When stdin is closed, the harness should drain in-flight work and exit
  with status `0`.

## Docker contract

Validators launch each miner image with:

```
docker run --rm -i \
  --network=none \
  --cpus=<N> --memory=<MB>m \
  --read-only --tmpfs /tmp:rw,size=<MB>m \
  -e DITTO_LLM_ENDPOINT=<url> \
  -e DITTO_LLM_API_KEY=<key> \
  --mount type=bind,source=<fixture-bundle>,target=/fixtures,readonly \
  <image>
```

Required image properties:

1. **Reproducible build.** The image must build deterministically from a
   public Dockerfile checked into the miner's submission repo. Validators
   may rebuild and compare digests as an audit step.
2. **No network access at validation time.** `--network=none` is the
   default. The only exception is a single configurable OpenAI-compatible
   chat endpoint URL passed via `DITTO_LLM_ENDPOINT`; if the miner needs
   that endpoint they must add the validator's allow-listed network alias
   when present.
3. **Read-only root filesystem.** Writes are confined to `/tmp` (sized by
   the validator) and `/dev/stdout`. Persistent state across cases is
   forbidden; rotate container instances if you need a cold start.
4. **Deterministic seed.** The harness MUST seed any RNG from
   `req.validator_seed` (a 64-character hex string). Two validators
   issuing the same challenge to the same image digest must observe the
   same response.
5. **No background goroutines past `Handle*` return.** All work for a
   challenge must complete before the response line is written so the
   recorded `total_latency_ms` reflects real wall-clock cost.

### Environment variables

| Variable               | Required | Meaning                                                  |
|------------------------|----------|----------------------------------------------------------|
| `DITTO_LLM_ENDPOINT`   | optional | OpenAI-compatible base URL for the harness to call.      |
| `DITTO_LLM_API_KEY`    | optional | Token paired with the endpoint above.                    |
| `DITTO_VALIDATOR_HK`   | yes      | Validator hotkey (echoed for forensic auditing).         |
| `DITTO_TEMPO_ID`       | yes      | Opaque tempo identifier; useful for cache invalidation.  |
| `DITTO_FIXTURES_PATH`  | optional | Defaults to `/fixtures`; mount point of the seeded data. |

Miners MUST NOT introduce additional configuration via files baked into
the image. Anything mutable must come through env or the request line.

### Filesystem and seeded fixtures

For `ditto_retrieval` cases the validator mounts a validator-controlled
seeded corpus at `DITTO_FIXTURES_PATH` (default `/fixtures`). The corpus
layout is opaque to the miner: a `manifest.json` file describes which
files belong to which `user_fixture_id`. Miners are expected to ingest the
seeded data from the mount before processing the first retrieval
challenge.

`fixture_bundle` in the request, when present, is an alternative
content-addressed handle (e.g. `ipfs://...`) for the same bundle; miners
that don't trust the local mount may verify the hash but MUST NOT fetch
the bundle from the network at validation time.

## Budget enforcement

For each challenge the validator enforces:

- **Wall-clock budget.** `deadline_ms` (default 8000ms) is the hard
  wall-clock limit. Responses received after the budget are scored as
  zero, even if the JSON is otherwise valid. The reference Python driver
  enforces this via stdout `select(2)` polling; production validators MAY
  also use `docker stop --time=…` after a soft warning.
- **Compute budget.** CPU and memory limits are passed via `--cpus` and
  `--memory`. Miners that OOM are scored as zero for the case and the
  container is replaced.
- **Cost budget.** `prompt_tokens` and `output_tokens` (plus
  `estimated_cost_usd`) are required fields on every response and feed the
  `latency_score` plus the optional cost-discount overlays defined in
  [`scoring.md`](scoring.md).

A miner that suspects a case is harder than the budget allows MAY return a
`refusal` early; that is preferable to a timeout because it frees the
validator to move to the next case without the full deadline penalty.

## Versioning

The `schema_version` field is `dittobench/1` and MUST be echoed verbatim
on every response. Validators reject responses with a mismatched schema
version. The Python source of truth for the constant is
[`../__init__.py`](../__init__.py); the Go reference template re-exports
it from a generated `const SchemaVersion = "dittobench/1"`.

Backward-incompatible changes bump the major number; backward-compatible
additions ship under the same version with a new tempo announcement.
