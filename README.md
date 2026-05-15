# Ditto Subnet (Bittensor SN118)

DittoBench is the public benchmark and incentive layer for Ditto's memory
stack on Bittensor SN118. Miners submit a Go harness (packaged as an OCI
image) implementing the two on-chain mechanisms; validators run each image
in a sandboxed Docker container, score per-case correctness, latency, and
anti-gaming gates, and emissions follow the resulting weight vector.

## Repository layout

```
ditto/
  bench/
    docs/         Wire protocol, harness interface, scoring, anti-gaming, coverage
    fixtures/     Public JSONL fixture corpus (toolcall/, retrieval/, longmemeval/)
    loader/       Python dataclasses + JSONL loaders (canonical taxonomy strings)
    runner/       Python runner, scorer, and anti-gaming helpers
    schemas/      JSON schemas for ChallengeRequest / MinerResponse / Score
  tests/bench/    Parity tests: keep Python ↔ Go scorer + anti-gaming in lockstep
go/
  bittensor/      Canonical Go types, scorer, anti-gaming (production validator)
harness/
  go-template/    Self-contained reference miner harness (Dockerfile + stubs)
```

The Go validator binary and the Python contributor runner agree byte-for-byte
through the parity tests in `ditto/tests/bench/`.

## Quickstart

### Miner

```sh
git clone https://github.com/heyditto/ditto-subnet
cd ditto-subnet

# Copy the reference harness and implement the two stubs.
cp -r harness/go-template my-harness
$EDITOR my-harness/internal/core/handler.go
$EDITOR my-harness/internal/retrieval/handler.go

# Build the image. The Dockerfile build context is the repo root so the
# in-tree replace directive in harness/go-template/go.mod resolves.
docker build -t my-harness:dev -f my-harness/Dockerfile .

# Score against the public fixture set.
uv sync
uv run python -m ditto.bench.runner \
  --image my-harness:dev \
  --mechanism ditto_core \
  --visibility public \
  --sample 10 \
  --report out/report.json
```

See [`ditto/bench/docs/harness_interface.md`](ditto/bench/docs/harness_interface.md)
for the Go interfaces, [`ditto/bench/docs/protocol.md`](ditto/bench/docs/protocol.md)
for the stdio framing, and [`harness/go-template/README.md`](harness/go-template/README.md)
for the full template walkthrough.

### Validator

```sh
# Run the full local suite (Python + Go parity tests).
make test

# Drive a candidate image end-to-end against the public split. Real
# validators add their own private/canary splits via the secret-driven
# partition helpers in ditto/bench/runner/antigaming.py.
uv run python -m ditto.bench.runner \
  --image <miner-image> \
  --mechanism all \
  --visibility all \
  --seed <validator-secret> \
  --report out/report.json
```

See [`ditto/bench/docs/scoring.md`](ditto/bench/docs/scoring.md) for the
per-component weight breakdown and the winner-takes-all policy, and
[`ditto/bench/docs/anti_gaming.md`](ditto/bench/docs/anti_gaming.md) for the
hidden-split, canary, memorisation-discount, and distractor controls.

## Make targets

- `make lint` — `ruff format --check` + `ruff check`
- `make format` — `ruff format` + `ruff check --fix`
- `make typecheck` — `mypy ditto/`
- `make test` — `pytest` (Python) + `go test` (canonical scorer/antigaming)
- `make go-test` — Go tests only
- `make go-lint` — `gofmt -d` + `go vet` over `go/...`
- `make harness-build` — `go build ./...` inside `harness/go-template/`
