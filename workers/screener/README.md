# Ditto Screener

Platform-operated screening worker for Ditto SN118 submissions.

The stable core leases one submission at a time from `ditto-platform`,
downloads and verifies its tarball, enforces safe language-neutral Docker
rules, builds the Docker image, starts it with resource caps and an internal fake
gateway, waits for `/health`, then performs a bounded, read-only Luna source
review before submitting a lease-bound sr25519 result. An optional, default-off
GPT-5.6 Terra level-2 reviewer can resolve only medium/high L1 leads with an
isolated, no-network coding-analysis harness. Terra is primary; OpenRouter may
fall back to the configured GLM/SOL models only after an upstream model error.
A complete, very-high-confidence medium-risk certificate from primary Terra may
clear directly; every static/high-risk,
incomplete or lower-confidence safe result requires the
independent GPT-5.6 SOL level-3 critic. A SOL challenge is decided by a second,
low-reasoning SOL adjudicator rather than becoming an automatic violation.
Obvious single-mechanism violations stop at L2; mixed benchmark/scorer/tool
violations use a bounded SOL cause adjudicator before retaining quarantine.
Reachable generator-shaped or review-adaptive model routing cannot be released
by model agreement alone; a location-bound deterministic hold preserves
quarantine without becoming a terminal rejection.
For `benchmark_emulation` and `scorer_contract_manipulation`, L2 evidence v2
also requires artifact-bound `served_trigger`, `authority_bypass`,
`scorer_visible_effect`, and `reachability_link` roles plus the exact changed
field or outcome (`final_text`, `answer`, `abstain`, `tool_calls`, the
validator-observed trajectory, or the graded outcome). `off` retains the L1
finding without running L2, `shadow` records the v2 candidate while returning
the exact L1 finding, and `enforce` accepts only a role-complete,
transition-compatible v2 finding. Missing, opaque, or budget-limited causal
proof is inconclusive and never becomes an unsupported quarantine finding.
Medium- and high-risk results are
quarantined for operator review, never automatically rejected. The default
manifest never calls `POST /run`. It never reads or writes the platform database.

The health smoke mirrors the validator runtime contract: UID/GID 65532, a
read-only root filesystem, a bounded noexec `/tmp` tmpfs, dropped capabilities,
private IPC, bounded local logs, and a locked
`DITTOBENCH_DB=/tmp/dittobench.db`. Images declaring implicit writable volumes
are rejected. An image is never exported if
it only boots as root or depends on writing elsewhere in its root filesystem.

Production builds use a dedicated rootless Docker daemon. The worker verifies
Docker's advertised `rootless` security option before accepting work when
`SCREENER_REQUIRE_ROOTLESS_DOCKER=1`; a missing or rootful endpoint becomes a
retryable infrastructure result, never a miner failure. The build receives no
credentials, cannot request host networking or insecure BuildKit entitlements,
and non-root host traffic is denied access to cloud metadata except DNS.

On a pass, the worker exports the exact verified image with `docker image save`,
hashes the archive, and uploads it sequentially in bounded multipart chunks.
Each storage request has a finite timeout and bounded retry policy; failures
trigger a best-effort multipart abort and the local archive is always removed.
The platform streams the completed object to verify the full archive SHA-256
before acknowledging it. The worker then binds that verified upload ID, archive
digest, byte size, immutable Docker image ID, and image reference into the
canonical signed verdict. Validators can therefore load the screened image
instead of repeating the untrusted build.

Rust is the reference starter implementation, not a competition requirement.
Python, TypeScript/JavaScript, Go, Rust, or any other implementation is accepted
when its root `Dockerfile` builds an image that serves the same `/health`,
`/seed`, and `/run` HTTP contract on port 8080. The screener never infers the
contract from a language manifest such as `Cargo.toml`, `package.json`,
`pyproject.toml`, or `go.mod`.

The only shared application boundary is the dependency-light
`packages/ditto-screening-protocol` package. It owns request/response models,
`AgentStatus`, `SCREENING_POLICY_VERSION`, artifact metadata, and the canonical
verdict-signing message. The worker does not import platform or subnet
application packages.

Private modules can rotate timing and relay tripwires, randomized controls,
source/fingerprint triage, and behavioral challenge packs without changing the
v9 protocol or signing bytes. No private signal proves causal model use.
Modules can pass or route to `retryable_infra`, `quarantine`, `inconclusive`,
or `pass_inconclusive`;
only the objective stable core can return `deterministic_reject`.

The worker also sends the optional signed, privacy-bounded fleet heartbeat
defined by the open platform fleet-health work. It reports only five-point
CPU/memory/disk buckets, aggregate Docker health/counts, worker state, and the
active agent ID. Heartbeat protocol v2 may also include one allowlisted stage
(`preparing`, `downloading`, `validating`, `building`, `starting`,
`health_check`, or `submitting`) and the current job's signed start time.

Heartbeat protocol v6 additionally announces the worker's own hardware, so an
operator can tell a node that is *slow* from a node that is *small*: logical
CPU count, physical cores when the kernel reports them, total RAM, the total
size of `/`, and the machine architecture. These are whole-unit sizes of the
machine, not an inventory of it — no hostname, serial, provider metadata,
image, mount layout, or free-space trace. They are sampled once at startup and
signed with the rest of the heartbeat, so a worker cannot advertise hardware
its hotkey did not sign for. A worker that cannot read its own hardware
reports at v5 rather than going dark.

The heartbeat never includes artifact contents, build output, dependency or
image metadata, policy internals, paths, prompts, evidence, or secrets. An
older platform can reject the optional endpoint without blocking or changing
screening.

## Local development

```bash
uv sync --group dev
uv run ruff format --check .
uv run ruff check .
uv run mypy ditto_screener packages/ditto-screening-protocol
uv run pytest -m "not integration"
docker build -t ditto-screener:local .
```

The real Docker core smoke test needs a canonical starter-kit checkout:

```bash
DITTO_STARTER_KIT_DIR=../../miners/dittobench-starter-kit \
  uv run pytest -m integration tests/test_gate_docker_integration.py -vv
```

Set `DITTOBENCH_API_DIR=/path/to/dittobench-api` as well to pass that exact
export through the real validator-side image loader during the integration test.

Pull requests retain the fast Python 3.12 unit suite, formatting, lint,
type-check, and production Docker image build gates. The full canonical-starter
build/health and isolated L2 coding-harness integration suite runs in
`Daily anti-cheat core E2E` every day at 08:17 UTC and on manual dispatch. Run
that workflow before merging changes to the real Docker gate or L2 analyzer
boundary when waiting for the next scheduled signal would be too risky.

## Runtime configuration

Required values are supplied through the production host's protected
`screener.env` file:

- `SCREENER_PLATFORM_API_URL`: platform API base URL.
- `SCREENER_API_TOKEN`: dedicated bearer token, at least 32 characters.
- `SCREENER_HOTKEY`: allowlisted public screener SS58 address.
- `SCREENER_WALLET_NAME` and `SCREENER_WALLET_HOTKEY`, or
  `SCREENER_MNEMONIC`: signing-key source. Prefer the host wallet.
- `SCREENER_GH_TOKEN_FILE`: optional path to a read-only token used only as a
  BuildKit secret for a private harness dependency.
- `SCREENER_POLICY_MANIFEST_FILE`: optional protected private manifest. When
  absent, the worker runs the production v8 Luna policy with no `/run` call.
- `SCREENER_REVIEW_JOURNAL_FILE`: optional protected append-only journal path
  for quarantine and inconclusive evidence.
- `SCREENER_AUDIT_SEED`: secret seed read only when a configured random-control
  module names this environment variable.
- `SCREENER_SOURCE_REVIEW_API_KEY_FILE`: required mode-0400 OpenRouter key file
  for the private read-only source reviewer. The default model is
  `openai/gpt-5.6-luna`.
- `SCREENER_L2_REVIEW_MODE`: `off` (default), `shadow`, or `enforce`.
- `SCREENER_L2_REVIEW_MODEL`: defaults to `openai/gpt-5.6-terra`; legacy
  `moonshotai/kimi-k3` remains accepted only for a deliberate rollback.
  `SCREENER_L2_FALLBACK_MODELS` is the ordered OpenRouter model-failover chain
  (`z-ai/glm-5.2`, then `openai/gpt-5.6-sol`). A fallback is used only after a
  model/routing error, never after a valid analyst response.
- `SCREENER_L3_REVIEW_ENABLED`: `true` (default) runs the independent SOL
  critic/adjudicator after L2. `false` keeps L1 routing and the Terra L2 analyst
  active while making the L2 result authoritative. Platform review-settings
  revisions can change this between leases without a worker restart.
- `SCREENER_L3_REVIEW_MODEL`: locked to `openai/gpt-5.6-sol`; both layers use
  OpenRouter ZDR routing and deny data collection.
- `SCREENER_L2_ANALYZER_IMAGE`: locked to the updater-built
  `ditto-screener-l2-analyzer:active` image.
- `SCREENER_L2_CACHE_DIR` and `SCREENER_L2_AUDIT_JOURNAL_FILE`: protected
  sanitized cache/audit locations. Raw source and transcripts are never stored.
- `SCREENER_STATIC_PREFLIGHT_V2_MODE`: `off` (default), `shadow`, or `enforce`.
  `off` and `shadow` preserve the v1 decisive result; `shadow` additionally
  computes the reachability-and-causality v2 candidate for comparison.
- `SCREENER_STATIC_PREFLIGHT_AUDIT_FILE`: protected mode-0600 JSONL journal for
  bounded v1/v2 shadow comparisons. Configure it before selecting `shadow`;
  an append failure is retryable infrastructure and prevents Docker execution.

Static preflight v2 makes a source match decisive only when both its effective
Docker/Cargo/runtime reachability and its category-specific source-to-sink flow
are proven. Excluded or test-only code is inert. Dynamic and unsupported build
or import forms remain advisory source-review leads. Roll out `shadow` first,
inspect only the sanitized private journal, and promote to `enforce` explicitly.

Static source matches remain pre-execution leads. In `shadow`/`enforce`, the
inert reviewer resolves them before any submission Dockerfile runs; only an L3
clearance may permit a high-risk static lead to continue to the build boundary.
An unresolved lead remains retryable, inconclusive, or quarantined and never
becomes a terminal automatic rejection.

Source-review requests follow OpenRouter's app-attribution contract with
`HTTP-Referer: https://heyditto.ai` and `X-OpenRouter-Title: Ditto`.

See [docs/policy-modules.md](docs/policy-modules.md) for the private module
boundary, [docs/source-review-policy.md](docs/source-review-policy.md) for the
allowed-optimization and benchmark-emulation boundary,
[docs/binary-analysis.md](docs/binary-analysis.md) for the bounded opaque-file
inspection contract, and
[docs/l2-source-review.md](docs/l2-source-review.md) for the Terra/GLM/SOL models,
isolated coding harness, evidence, budgets, and canary/rollback contract,
[docs/l4-adjudication.md](docs/l4-adjudication.md) for the automated
clear/reject court that resolves a hold instead of queuing it, and
[docs/deployment.md](docs/deployment.md) for deployment secrets, health checks,
cache maintenance, and the compatible rollout sequence.
