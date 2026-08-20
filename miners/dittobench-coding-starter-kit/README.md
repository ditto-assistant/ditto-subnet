# DittoBench coding starter kit

This is the shadow-only reference miner harness for DittoBench Coding contract
v1. It demonstrates miner-owned, task-scoped embedded memory and a bounded SWE
agent loop while keeping the mutable repository behind the validator-owned
workspace capability.

It does **not** alter the active DittoBench score or weights. The public coding
practice pack is permanently ineligible for emissions.

## Trust boundary

The harness receives:

- one opaque case and profile capability;
- only that case's visible memory bundle;
- an expiring coding-runner URL;
- a ticket-scoped inference relay URL.

It never receives a repository path, `.git`, hidden tests, grader commands,
provider credentials, the authoritative patch, or score evidence. All code and
test operations use typed tools. Provider-facing function names use underscores
and the client maps them to the canonical dotted runner wire names:

```text
repo_list_tree  -> repo.list_tree
repo_search     -> repo.search
repo_read_file  -> repo.read_file
repo_read_range -> repo.read_range
repo_apply_patch -> repo.apply_patch
tests_run       -> tests.run(command_id)
build_run       -> build.run(command_id)
git_status      -> git.status
git_diff        -> git.diff
```

There is no general shell tool. The scorer freezes the validator-owned
workspace after `/coding/run` returns.

## Endpoints

```text
GET  /coding/health
POST /coding/seed
POST /coding/run
```

`/coding/seed` verifies a canonical memory-bundle digest and creates a bounded,
in-memory Turso store for `(ticket_id, case_id, profile_capability_id)`. The
reference embedder is a deterministic 768-dimensional lexical hash suitable
for offline practice. It is intentionally a baseline that miners can improve.

`/coding/run` retrieves only from that store, injects bounded memory IDs,
content, validity metadata, and similarity into the coding context, and sets no
persistent task memory. Case state is deleted when the run ends.

## Model modes

The default is the production-shaped ticket broker:

```bash
cargo run --locked -- --port 8080
```

The request's `inference_base_url` is used with a placeholder compatibility
bearer. The Platform relay remains responsible for the real provider key and
route enforcement.

Offline scripted practice requires an explicit gate:

```bash
cargo run --locked -- \
  --model-mode scripted \
  --allow-practice-model \
  --script fixtures/mock/ledger-001.json
```

Direct OpenRouter is also local-practice-only and requires two independent
inputs:

```bash
export OPENROUTER_API_KEY='...'
cargo run --locked -- \
  --model-mode openrouter \
  --allow-direct-openrouter
```

The binary deliberately does not load dotenv files. Keep the credential in an
ignored external file and export it as uppercase `OPENROUTER_API_KEY` only for
the local process.

That mode fixes `openai/gpt-5.6-luna`, Chat Completions, medium reasoning,
serial tool-call execution, the current `azure/eu` ZDR route, no
fallback, supported-parameter enforcement, and denied data collection. It
rejects a nonempty mismatched model, a mismatched provider, or a missing
generation ID.
If OpenRouter omits the optional response-model field, the exact request model
and no-fallback route are used as a labeled local-practice identity. Coherent
nonnegative token counts enforce the in-process run budgets, and direct mode
requires a finite nonnegative reported USD cost. The current service does not
persist generation/provider/cost metadata as authoritative benchmark evidence;
that remains future validator/relay work. The key is never serialized or
logged, and no custom base URL is accepted with it. Scripted and
direct-OpenRouter servers bind only to loopback; only the credential-free
ticket-broker mode binds the container interface. Inference and workspace HTTP
clients refuse redirects.

If a provider nevertheless returns multiple tool calls, the adapter preserves
their order in a bounded 16-call queue and releases exactly one call per agent
turn. It does not make another provider request until the queue is empty, and
usage/cost metadata is attached only to the first returned call. Workspace
operations therefore remain serial and validator-observed.

## Validation

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-targets --all-features
```

The practice integration test consumes the committed
`PRACTICE-LEDGER-001` capsule, confirms its base fails, runs the scripted agent
through an HTTP workspace capability, and confirms the real public grader tests
pass afterward.
