# DittoBench coding starter kit

For the ten-task public local dataset, follow the
[repository practice guide](../../research/dittobench-coding-datagen/practice/v2/README.md).
Datagen unpacks and grades public workspaces without a hosted dataset account.
This starter still implements contract v1; the v2 workspace-grading commands
do not yet provide an automatic v2 `/coding/run` adapter for this harness.
The scripted examples below are v1 protocol regressions, not the public pack.

This is the shadow-only reference miner harness for DittoBench Coding contract
v1. It demonstrates miner-owned, task-scoped embedded memory and a bounded SWE
agent loop while keeping the mutable repository behind the validator-owned
workspace capability.

It does **not** alter the active DittoBench score or weights. The public coding
practice pack is permanently ineligible for emissions.

The shared contract vectors are owned by
`packages/dittobench-coding-contract`. Rust seed/run parsing and canonical
memory digests are tested against the same vectors as Python and Go.

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
repo_create_file -> repo.create_file (manifest-authorized private tasks)
repo_delete_file -> repo.delete_file (manifest-authorized private tasks)
tests_run       -> tests.run(command_id)
build_run       -> build.run(command_id)
git_status      -> git.status
git_diff        -> git.diff
```

There is no general shell tool. The scorer freezes the validator-owned
workspace after `/coding/run` returns.

Workspace calls are serialized. A lost response is retried once with the exact
same `call_id`, which the runner serves idempotently; an unresolved ambiguous
transport failure poisons the client so later calls fail immediately instead of
burning the remaining budget against a sequence gap. Create/delete names are
reserved in contract v1 but disabled by the public practice policy.

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

Coding contract v1 also pins the first public system prompt and the complete
ordered model-facing workspace-tool schemas. The miner-safe
`coding_inference_miner_v1.json` vector proves this reference harness emits the
same two-turn Chat Completions shape. Miners may improve memory retrieval,
planning, context management, and loop behavior, but a changed first prompt or
tool contract requires a separately versioned benchmark policy rather than a
silent per-miner provider surface.

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

The coding-only binary deliberately does not load dotenv files. Keep the
credential in an ignored external file and export it as uppercase
`OPENROUTER_API_KEY` only for the local process. The unified image `serve`
command still loads a working-directory `.env` for normal DittoBench
configuration; do not package that file in a submission archive.

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

If a provider nevertheless returns multiple tool calls, the adapter fails the
response closed. It never rewrites one batched provider decision into several
apparent model turns, so every workspace mutation follows a fresh, observed
model decision and workspace operations remain serial.

Live workspace observations may use the runner's full 32 KiB read bound.
Context compaction separately evicts older complete tool-call/result pairs.
Token, tool, turn, context, or wall-time exhaustion returns a bounded degraded
final report rather than HTTP 5xx, so the validator can still freeze and grade
the authoritative workspace.

## Validation

```bash
cargo fmt --check
cargo clippy --all-targets --all-features -- -D warnings
cargo test --all-targets --all-features
# from the repository root:
scripts/test-coding-starter-practice-e2e.sh
```

`cargo test` covers protocol, compaction, and the scripted agent loop.
The PRACTICE-LEDGER-001 HTTP+grader path is the repository-root e2e script.
