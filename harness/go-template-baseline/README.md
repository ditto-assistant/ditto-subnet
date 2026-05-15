# DittoBench reference baseline harness

A self-contained reference miner that actually attempts the two
DittoBench mechanisms:

- **Core** — forwards each `ChallengeRequest.Prompt` plus the supplied
  tool schemas to an OpenAI-compatible chat completions endpoint and
  echoes the model's emitted `tool_calls` back as `MinerResponse.ToolCalls`.
  Source: [`internal/core/openai.go`](internal/core/openai.go).
- **Retrieval** — lazily indexes the validator-mounted fixture corpus
  with an in-memory BM25 over the pair content and returns the top
  `req.K` pair IDs ranked by score. Pure Go, no third-party deps.
  Source: [`internal/retrieval/bm25.go`](internal/retrieval/bm25.go).

This is the "did I beat the baseline by X%?" anchor miners should fork
and improve, not a competitive submission. The stub at
[`../go-template/`](../go-template) remains the right starting point if
you want to write everything from scratch.

## Quickstart

```sh
# Build the image (build context is the repo root so the in-tree
# go.mod replace directive resolves).
docker build -t ditto-baseline:dev -f harness/go-template-baseline/Dockerfile .

# Drive it against the public split. Without OPENAI_API_KEY the Core
# handler refuses cleanly; retrieval still ranks if the validator
# mounted a fixture corpus.
uv run python -m ditto.bench.runner \
  --image ditto-baseline:dev \
  --mechanism all \
  --visibility public \
  --sample 10 \
  --report out/baseline-report.json
```

## Environment

| Variable              | Purpose                                                                        |
| --------------------- | ------------------------------------------------------------------------------ |
| `OPENAI_API_KEY`      | Bearer token for the Core baseline. Empty key -> Core refuses.                 |
| `OPENAI_BASE_URL`     | Override the chat completions endpoint (defaults to OpenAI's production URL).  |
| `OPENAI_MODEL`        | Model id sent in the request body (defaults to `gpt-4o-mini`).                 |
| `DITTO_FIXTURES_PATH` | Mount point for the validator's seeded corpus. Defaults to `/fixtures`.        |

## Published baseline

The latest published baseline image digest and the fixture manifest hash
it was scored against live in [`BASELINE.md`](BASELINE.md). To regenerate
them locally run `make baseline-bench` at the repo root; that target
builds the image, runs the validator self-test pipeline over the full
public split, and rewrites `BASELINE.md` with the new numbers.

When a real release goes out a CI job (see the follow-up plan) pushes
the image to `gcr.io/heyditto-public/ditto-baseline` and updates the
image-digest line in this directory. Until that lands, contributors
should regenerate `BASELINE.md` locally before claiming a beat.

## Layout

```
harness/go-template-baseline/
  cmd/harness/main.go       Stdio dispatch (identical to go-template)
  internal/core/openai.go   Core baseline (HTTP -> chat completions)
  internal/retrieval/bm25.go Retrieval baseline (in-memory BM25)
  Dockerfile                Multi-stage build, distroless runtime
  README.md                 (this file)
  BASELINE.md               Latest published score + image digest
```
