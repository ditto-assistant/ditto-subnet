# DittoBench docs

DittoBench is the public benchmark suite for the Ditto Bittensor subnet
(SN118). It scores miners on how well their **memory-grounded agentic
pipeline** performs against the canonical Ditto product surface: tool
routing, memory retrieval, contradiction handling, abstention discipline.

## Reading order

1. [`protocol.md`](protocol.md) — how validators drive a miner harness over
   stdio in a sandboxed Docker container, including the exact JSON request
   and response shapes.
2. [`harness_interface.md`](harness_interface.md) — the Go interfaces
   miners implement (`CoreHarness`, `RetrievalHarness`), the Docker
   contract, environment variables, and the reference template repo.
3. [`scoring.md`](scoring.md) — per-mechanism weight breakdowns and the
   winner-takes-all weight-assignment policy validators apply per
   mechanism.
4. [`anti_gaming.md`](anti_gaming.md) — hidden-split, canary paraphrase,
   memorisation-discount, distractor injection, and Docker image-digest
   pinning controls.
5. [`coverage_matrix.md`](coverage_matrix.md) — every fixture file mapped
   to its mechanism, category, and the product capability it tests.

## Contributor workflow

```
# 1. Clone the subnet (which already ships the reference Go harness).
git clone https://github.com/heyditto/ditto-subnet
cd ditto-subnet

# 2. Copy the reference template and start implementing the two interfaces.
cp -r harness/go-template my-harness
$EDITOR my-harness/internal/core/handler.go
$EDITOR my-harness/internal/retrieval/handler.go

# 3. Build the Docker image (run from the repo root so the in-tree
#    `replace` directive in go.mod resolves the bittensor module).
docker build -t my-harness:dev -f my-harness/Dockerfile .

# 4. Score against the public fixture set.
uv sync
uv run python -m ditto.bench.runner \
  --image my-harness:dev \
  --mechanism ditto_core \
  --visibility public \
  --sample 10 \
  --report out/report.json
```

The on-chain validator uses a sibling pipeline with stricter sandbox
limits, the validator-only private/canary splits, and audited image-digest
pinning. See [`anti_gaming.md`](anti_gaming.md) for the full control set.
