# Unified DittoBench miner starter

This reference kit builds one Docker image that serves the active normal
DittoBench harness and the additive, shadow-only coding harness on port `8080`:

```text
GET  /health          POST /seed          POST /run
GET  /coding/health   POST /coding/seed   POST /coding/run
```

Normal endpoints are the active submission contract. The coding endpoints are
an optional capability advertisement only: coding contract v1 is
`weight_eligible=false`, and this image does not alter normal Tool + Memory
scores, validator weights, or emissions.

The normal baseline and the coding agent stay in their existing starter kits.
This thin composition crate owns only one listener, endpoint routing, and
packaging. The coding agent still receives no repository path, hidden tests,
grader material, or provider credential.

## Run

Use the Platform-owned inference routes in normal validator operation:

```bash
cargo run --locked -- serve --port 8080
```

The normal harness reads its existing `DITTOBENCH_*` configuration. Do not add
an API key or `.env` file to the submitted archive. The coding route uses the
ticket-scoped `inference_base_url` sent on each `/coding/run` request.

## Package one submission archive

From this directory:

```bash
cargo run --locked -- submit
cd ../..
uv run ditto verify \
  --path miners/dittobench-unified-starter-kit/dittobench-submission.tgz
```

The command creates one build-context archive with a root `Dockerfile` and the
three source crates required by its path dependencies. It excludes targets,
databases, `.env` files, Git state, and prior archives, then enforces the
normal 20 MiB compressed-upload limit.

For a source-checkout Docker build, use `miners/` as the context so Docker can
see the two sibling crates:

```bash
docker build \
  --file miners/dittobench-unified-starter-kit/Dockerfile \
  --tag dittobench-unified-miner:local \
  miners
```

The public coding practice pack remains a separate developer download. Private
catalog records, repositories, hidden tests, and grader plans are never
packaged in this image.
