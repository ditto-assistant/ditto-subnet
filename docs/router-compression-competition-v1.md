# Router & Compression competition v1 (shadow contract)

Status: proposed shadow-only competition dimension. This document does not add
a route to the active upload protocol, issue a lease, run a miner, alter the
Tool + Memory composite, or change validator weights. Router contract v1 is
permanently `weight_eligible=false`.

Board: https://github.com/orgs/ditto-assistant/projects/10
Epic: https://github.com/ditto-assistant/ditto-subnet/issues/1664

## Why

The Ditto inference gateway ("dittorouter", `backend/pkg/services/inference`,
served at `api.heyditto.ai/v1/messages` and `/v1/chat/completions`) sits between
coding harnesses (Claude Code, Codex) and model providers. Every dollar it saves
is measured with real headless Claude Code runs on the inference cost bench
(`heyditto-stack` skill `inference-cost-bench`). What one team shipped in a
week:

| Lever | Measured effect (Sonnet 5, 2026-09-05/06) |
|---|---|
| Native Messages route that preserves `cache_control` | 0 → ~95% cached input; README task $0.45 → $0.18 |
| Memory recall once per user turn, re-attached byte-identically (ledger) | 5-task suite $4.31 → $0.71 |
| Shape-based request archetypes + per-archetype routes (asides/probes → GLM 5.3 Flash) | title prompt ~10x cheaper |
| Tool-description compression (one condensed rewrite per unique tool hash) | prefix −16%, 8/8 tasks still pass |
| Deterministic context compaction of completed-turn tool results | large-read 3-turn task $0.95 → $0.67 |
| Compaction archetype + proactive compaction snapshots (experimental) | compaction served from a snapshot in seconds; cheap models lose detail |
| Deterministic tool-result compression at ingestion | 97% removal on a 110 KB log read, 22% on Bash output |

Each lever is a policy over the same inputs (a harness request plus session
state) with the same hard constraints. That is a competition shape SN118
already knows how to run: an immutable artifact, a public HTTP contract,
validator-owned execution, deterministic grading, hidden material, shadow
before weights. Miner talent should be pointed at this surface.

## Hard constraints (learned in production)

These are gates, not scoring dimensions. A plan that violates one scores zero
for that request.

1. **Harness compatibility.** Claude Code decides when to compact from the
   `usage` the gateway reports, expects every `tool_use` id and block it sent
   to still exist in the transcript it gets back, and marks its cache
   breakpoint on the last block of the last user message. Codex expects
   Responses-shaped items intact. The plan may not rename, drop, reorder, or
   merge tool ids, tool blocks, or message roles.
2. **Determinism.** The same `(request, session_state)` must produce a
   byte-identical plan on every call. Every transform applied to a completed
   turn must be re-applied identically on every later re-send, or provider
   prefix caches stop hitting and the "savings" invert.
3. **Fidelity is task pass rate.** Token counts are a cost, not a quality
   signal. The only fidelity measure is whether the harness still solves the
   task under the plan, at parity with a fixed baseline.
4. **No prompt or memory contamination.** Bytes a plan adds must come from an
   allowlisted derivation of bytes already in the request or from the
   validator-supplied memory bundle. Compaction instructions recorded as
   memories were later recalled as "prompt injection" and the model refused
   the real compaction; a miner must not be able to reproduce that failure.
5. **Provider quirks are real.** GM misses the cached prefix when a
   `cache_control` block is followed by another block; Anthropic
   `clear_tool_uses` context edits return `applied_edits: []` through
   OpenRouter and GM; Haiku's cache minimum is 4096 tokens. The catalog the
   validator hands the miner encodes these; the plan is scored against what
   the provider actually billed.

## One artifact, one additive lane

The submission reuses the unified miner capability contract
([`coding-unified-miner-capability-shadow.md`](coding-unified-miner-capability-shadow.md)).
A router-capable artifact is the same gzip build context (≤ 20 MiB, root
`Dockerfile`, port `8080`) that today serves `GET /health`. It additionally
serves:

```text
GET  /router/health
POST /router/plan
POST /router/artifacts
```

`404 /router/health` means normal-only; no penalty, no router score. A
malformed advertisement yields no router attestation and cannot touch a normal
score. Router routes are additive, not a replacement for `/health`, `/seed`,
`/run`, or `/coding/*`.

```json
{
  "status": "ok",
  "supported_router_contract_versions": [1],
  "capabilities": ["plan_v1", "tool_digest_v1", "result_digest_v1", "context_compaction_v1"]
}
```

Unknown fields are ignored (`extra="ignore"` per repository policy). Known
fields are validated, normalized, and bound to the exact screened image digest.

### Open question: must a router artifact be core-qualified?

Coding admission requires a durable Tool + Memory core-qualification decision
for the same artifact. Router talent (gateway, caching, tokenizer, provider
economics) is not memory-harness talent. This document proposes that router
certification requires source-integrity screening plus the public router canary
but **not** Tool + Memory core qualification, so a router-only image is
admissible. Owner decision; tracked on the epic.

## What a miner submits: the plan function

`POST /router/plan` is a pure function. The validator calls it once per
harness request during evaluation; the miner never sees a provider, a
credential, the task, the hidden tests, or the harness process.

### Request

```text
router_contract_version      1
plan_ticket                  validator-signed, ticket-scoped, single use
session                      opaque session id, turn index, request index
harness                      {family: claude_code|codex, wire: anthropic_messages|openai_chat|openai_responses}
request                      the canonical harness request body (unmodified bytes)
prior_plans                  digests of every plan already served in this session, in order
memory_bundle                validator-supplied recall result for this user turn (may be empty)
catalog                      frozen provider routes for this task, each shaped like the locked
                             coding inference policy: {route_id, wire, model, provider_api,
                             provider_route, allow_fallbacks=false, zdr, price micros per input /
                             cached-input / output token, cache_min_tokens, quirks[]}
budget                       remaining max_cost_usd_micros and wall-clock seconds for the task
```

Bodies are canonical JSON (`canonical_bytes`: sorted keys, compact separators,
UTF-8, escaped U+2028/U+2029, one trailing newline; 4 MiB / 32-level bound,
duplicate keys rejected) so every digest in this document is reproducible in
Python, Go, and Rust. `router_contract_version` is independent of
`bench_version` and `coding_contract_version`; ledger rows carry
`bench_family: "router"` so no numeric floor accidentally matches.

The memory bundle is validator-owned: recall runs once per user turn in the
validator's gateway shim exactly as production does today. The miner decides
where and whether to attach it; it never writes memories in v1.

### Response: `RouterPlan`

```text
plan_digest        sha256 over the canonical plan
route              one catalog route_id
request            the transformed body to send on that route
transforms[]       typed, ordered records of what changed (see classes below)
cache_breakpoints  positions the plan expects the provider to cache
usage_policy       "passthrough" (v1 only: report provider usage unmodified)
```

The validator executes `request` on `route` through the existing locked,
ticket-scoped inference relay (`dittobench-api` broker; no provider credential
reaches the miner or the harness), returns the provider response to the harness
unchanged, and records the provider receipt (input, cached, output tokens,
billed USD, latency).

### Allowed transform classes (v1)

| Class | Allowed | Forbidden |
|---|---|---|
| `route` | Any catalog route whose wire matches the request's tool/feature needs | Routes outside the catalog; per-request provider fallback |
| `tool_digest` | Replace a tool `description` with a condensed rewrite keyed by `sha256(name + description + input_schema)` | Changing `name` or `input_schema`; dropping tools |
| `result_digest` | Rewrite the content of a `tool_result` from a **completed** turn with a deterministic digest derived only from that result's bytes (head/tail, exact-count line grouping) | Touching the current turn's results; touching code, diff, or JSON payloads (a digest that groups them is a hard failure); changing `tool_use_id` |
| `context_compaction` | Apply a per-session plan of `result_digest`s, re-applied identically on every later request | Any transform whose output differs between re-sends of the same completed turn |
| `memory_placement` | Attach the validator-supplied memory bundle as one block on the user turn, or omit it | Attaching text not in the bundle; attaching to the system prompt tail |
| `cache_marks` | Move or add `cache_control` markers | Leaving a marked block followed by another block on a GM route (catalog quirk) |

Not in v1: response rewriting, proactive compaction snapshots that answer a
compaction request without a model call, model-generated digests at plan time,
and memory writes. Each is a candidate v2 capability with its own fidelity
evidence requirement. Anything not listed is forbidden; the verifier
reconstructs the transformed body from the original plus the typed transforms
and rejects any byte the reconstruction does not explain.

### `POST /router/artifacts`

Returns the miner's content-addressed artifacts referenced by plans:
`tool_digest` rewrites keyed by tool hash and any static tables. Artifacts are
bounded (1 MiB total), fetched once per evaluation, digest-verified, and
shipped to Ditto if the artifact wins (see consumption below). A plan that
references an artifact the endpoint did not return is invalid.

## How validators evaluate

Evaluation reuses the coding shadow machinery: leases, arms, workspace freeze,
pristine grading, terminal evidence, the failure classifier, and the
fail-once retry policy ([`no-automatic-retries.md`](no-automatic-retries.md)).

### Task material

A router task is a **coding task plus a fixed harness**, not a recorded trace.
Task records reuse the coding catalog shape (content-addressed
`router-catalog/v1/<commitment>/records/<index>.json`, position-bound Merkle
membership proofs, commit-then-future-block selection, append-only exposure
ledger) with a `harness` section added to the task payload.
Replaying a recorded trace cannot score a transform, because a changed request
changes the model's reply and every later turn. The validator therefore runs
the real harness against a validator-owned gateway shim that consults the
miner's plan for every request.

- Tasks come from the same catalogs as DittoBench Coding: public practice
  packs for rehearsal, a hidden validator partition for scoring, content-
  addressed and assignment-scoped, withheld from miners.
- Each task fixes: repository epoch, task text, hidden trusted tests, the
  harness family and pinned version, the harness system prompt and tool set,
  the memory profile (some tasks need a seeded memory to pass, as the bench's
  `pricing-policy` and `cli-conventions` do), the multi-turn script
  (`---turn---` boundaries), and a frozen provider catalog with prices.
- Every task ships with a **baseline arm**: the identity plan (passthrough on
  the catalog's reference route with the memory bundle attached on the user
  turn). Baseline receipts are collected in the same wave as the candidate so
  provider price and latency drift cancel.

### Arms

```text
T tasks x {baseline, candidate} x R replicas
```

v1 shadow uses `T = 8`, `R = 2` per validator (32 harness sessions), with
tasks drawn so at least two need memory, two are three-turn, and one forces an
autocompact (`CLAUDE_CODE_AUTO_COMPACT_WINDOW` in the harness env). Each arm
gets a fresh harness process, workspace, and session; no plan digest, cache,
or artifact state crosses an arm boundary except the miner's own static
artifacts.

### Per-request checks (deterministic, offline-replayable)

Every plan the validator receives is checked before it is executed. A failed
check ends the arm as `candidate_integrity` (repair score zero for that task,
no retry):

- **Schema and compatibility:** reconstruction from typed transforms equals the
  submitted body byte for byte; every original `tool_use`/`tool_result` id and
  block is present in order; roles unchanged; route in catalog.
- **Determinism:** the validator calls `/router/plan` twice with identical
  inputs for a sampled 25% of requests and requires identical `plan_digest`.
- **Prefix stability:** for request *n*, the transformed bytes of every
  completed turn must equal the bytes the plan produced for that turn at
  request *n−1*. This is the cache-stability gate; it is measured on bytes,
  not on provider cache reports.
- **Contamination:** no added byte outside the allowlisted derivations;
  digests of code/diff/JSON are rejected; the validator seeds one canary
  memory per task and fails the arm if canary text appears anywhere the
  bundle was not placed.
- **Budget:** the plan may not select a route whose worst-case price for this
  request exceeds remaining task budget.

These checks form the **v1 offline scorer**. They run without any provider
call against recorded shim traffic, so they are also what public practice and
the canary execute.

### Terminal evidence

Each arm ends in exactly one of the coding terminal domains: `resolved`,
`repair_failure`, `validator_infrastructure`, `task_invalid`,
`candidate_integrity`, `control_plane_integrity`, with router-specific bounded
`failure_code`s (`plan_schema`, `plan_nondeterministic`, `prefix_unstable`,
`transform_unexplained`, `contamination`, `route_not_in_catalog`,
`budget_exceeded`, `artifact_missing`). Only `resolved` and `repair_failure`
enter the mean; `candidate_integrity` is an attributable zero.

Each arm produces a signed record binding artifact digest, task and condition
commitments, harness identity, every `plan_digest` in order, every provider
receipt, the frozen workspace digest, the pristine grade, and the terminal
classification. Provider, relay, or grader failures are `validator_infrastructure`
and never improve or penalize the miner; missing candidate evidence fails
closed.

## Scoring

Public router tasks are qualification material only. The competitive score
uses the hidden validator partition.

All quantities are integers (micros or basis points), never floats, as in
`V9BaseDetails`.

```text
RouterEligible =
  source-integrity pass
  AND public router canary pass
  AND every executed plan passed the per-request checks

solved(a)            = 1 if all trusted tests pass for arm a, else 0
cost_micros(a)       = sum of provider-receipt USD micros across the arm's requests
                       (cost_source = provider_receipt_v1; unknown receipts fail the arm
                       as validator_infrastructure, never as a fabricated zero)
tail_bps(t)          = 10_000 × (wall(candidate_t) − wall(baseline_t)) / wall(baseline_t)

FidelityGate         = 1 if solved_rate_bps(candidate) >= solved_rate_bps(baseline) − 500  else 0
LatencyGate          = 1 if median_t tail_bps(t) <= 2_500                                   else 0

For each task t (paired, both arms resolved):
  saving_bps_t = 10_000 − 10_000 × cost_micros(candidate_t) / cost_micros(baseline_t)
                 clipped to [0, 9_999]
For each task t where candidate is repair_failure and baseline resolved:
  saving_bps_t = 0
Tasks where the baseline is not resolved are excluded from the denominator.

ValidatorScore_bps   = mean_t(saving_bps_t)
FinalRouterScore_bps = IntegrityGate × FidelityGate × LatencyGate
                       × median over the validator quorum of ValidatorScore_bps
```

Notes:

- The saving is a ratio against a same-wave baseline, so it is stable across
  provider price changes and cannot be gamed by choosing tasks. The clip below
  `10_000` keeps the composite strictly under a perfect score, in the spirit of
  the asymptotic efficiency shape adopted in #884.
- Fidelity is gated, not blended. A router that solves fewer tasks has no
  score, whatever it saved. The 500 bps floor is a starting value; calibration
  (child issue) sets it from the paired variance of baseline arms.
- Latency counts because a route to a slow cheap model is a real regression
  for a developer; the 2 500 bps tail is likewise a calibration input.
- Ties use the existing protocol 20 evidence-tied pooling and shared-seed
  paired confirmation; nothing new is invented for rank.
- `IntegrityGate` is zero only for confirmed sandbox escape, hidden-test
  access, credential exfiltration, relay bypass, or evidence tampering, as in
  [`coding-screening-private-scoring-policy.md`](coding-screening-private-scoring-policy.md).

## Anti-gaming

- **The miner never touches a provider.** Plans are executed through the
  locked relay; the plan cannot name a provider outside the catalog and cannot
  trigger a fallback. `no-automatic-retries` applies: a provider failure ends
  the arm as infrastructure, it is not an opening for a second dispatch.
- **Hidden tasks and hidden harness versions.** The scored partition is
  withheld; the harness pin and system prompt in the hidden partition may
  differ from the public pack, so archetype detection keyed on harness wording
  fails while shape-based detection keeps working.
- **Determinism and prefix equality are byte checks**, sampled twice per
  request, so a plan cannot present a cache-friendly face on the first send
  and a cheaper one later.
- **Typed transforms with reconstruction** mean the verifier never has to
  reason about intent; any unexplained byte is a hard failure.
- **Baseline in the same wave** removes the incentive to time submissions to
  provider price windows.
- **Source integrity** reuses the screening court and anti-copy fingerprints.
  Additional router-specific deny rules: embedded provider clients or API
  keys, any outbound network call from the plan process, non-deterministic
  sources (time, random, environment) reachable from `/router/plan`.
- **Canary memory** and allowlisted derivations close the contamination
  channel that bit production.

## How Ditto consumes winners

Router contract v1 is shadow-only; consumption is what the shadow evidence is
for. The gateway grows an `endpoint.router_policy` field:

```text
router_policy = ditto                      (today's strategies; default)
              | shadow:<artifact-digest>   (compute the miner plan, log the savings delta, apply ditto)
              | miner:<artifact-digest>    (apply the miner plan; opt-in per endpoint)
```

- Ditto runs certified top-ranked router images as capability-only sidecars
  (no egress, same posture as the validator shim) and calls `/router/plan`
  in-process of a request. The gateway keeps its own per-request checks and
  falls back to `ditto` on any failure, recording the fallback in the savings
  ledger under a new `miner_policy` strategy kind.
- `tool_digest` artifacts are cheap to ship independently: the gateway already
  keys its rewrites by the same tool hash, so a winning miner's table can be
  loaded as data behind the existing `tool_compression` strategy.
- The savings ledger attributes every dollar to `ditto` or to a miner artifact
  digest per strategy kind, so the production delta of a champion is visible
  on the endpoint card and can feed a future reward policy.

## Phases

| Phase | Name | What is scored | Weight |
|---|---|---|---|
| v1 | Offline replay scoring | Per-request checks over recorded shim traffic from the public pack: schema, determinism, prefix stability, contamination, estimated cost from catalog prices. No provider calls. Public leaderboard is diagnostic. | none, `weight_eligible=false` |
| v2 | Shadow live evaluation | Paired baseline/candidate arms on hidden tasks through the locked relay under budget; `FinalRouterScore` computed and published to Backroom only. Ditto runs the top artifacts in `shadow:` mode on opted-in endpoints. | none, `weight_eligible=false` |
| v3 | Production routing | `miner:` policy on opted-in endpoints; production savings ledger becomes calibration evidence for a separately reviewed weight-eligible contract version. | requires new contract version, calibration evidence, owner approval |

Project completion does not activate any phase beyond v1. Each promotion is an
owner decision recorded in Backroom.

## Metrics and telemetry

Validator-side (signed terminal evidence): per arm solved, billed USD, tokens
in/cached/out, wall time, plan count, per-request check outcomes, route
histogram, transform-class histogram.

Platform/Backroom: router leaderboard with `FinalRouterScore`, per-validator
`ValidatorScore`, fidelity and latency gate states, and the check-failure
taxonomy; a `backroom:read` tool for every knob this document names (task
count, replicas, fidelity floor, latency tail, budget per task, catalog
revision) so no state has to be inferred from logs.

Gateway-side (production, v2+): savings ledger rows keyed by strategy kind and
policy source; fallback counts; shadow delta per endpoint per day.

## Reuse map

| Need | Existing machinery |
|---|---|
| Artifact upload, build, `/health` screen, anti-copy | normal submission pipeline, screening court |
| Additive capability advertisement | `/coding/health` pattern |
| Certification lease bound to exact screened image | `coding-qualified-certification-lease-shadow.md` |
| Public canary | `coding-public-v2-canary.md` sequence, with `/router/plan` replay in place of `/coding/run` |
| Hidden task material, leases, artifact delivery | coding private catalog, ticket sets, task leases, Hippius transport |
| Locked inference, receipts, budgets | `dittobench-api` broker, Luna relay, native hosted inference authority |
| Arms, freeze, pristine grade, terminal evidence, classifier | `coding-private-shadow-execution-v2.md`, `coding-shadow-failure-classification.md` |
| Quorum median, ties, retries | validator quorum of three, protocol 20 pooling, `no-automatic-retries.md` |
| Contract vectors | `packages/dittobench-coding-contract` layout (`generate_*_vectors.py --check`, `testdata/*.json`, `.invalid` hosts) cloned as `packages/dittobench-router-contract` |
| Version authority | `research/dittobench-datagen/protocol/epoch.go` pattern: `RouterContractVersion` constants, known-vector CI test |
| Screening wire | `packages/ditto-screening-protocol` (`SourceReview*`, `coding_source_screen.py` pattern for a `router_source_screen`) |
| Version sweep | `.agents/skills/ditto-subnet-bench-version-bump` (floors, shared constants) |

## Open questions

Tracked on the epic; not decided here.

1. Whether router admission requires Tool + Memory core qualification or only
   source integrity plus the router canary.
2. Which harness pins are frozen per contract version, and how a Claude Code
   or Codex release that changes request shape is handled (new task-set
   revision vs. new contract version).
3. Whether the memory bundle stays validator-owned in v2 or miners may submit
   a recall policy as a further lane.
4. Budget per task and per wave; the bench measured $0.18–$1.02 per task on
   Sonnet 5, so 32 sessions per validator per artifact is roughly $10–$30
   before baseline arms are shared across candidates.
5. Whether baseline arms are shared across candidates in one wave (cheaper)
   or paired per candidate (cleaner statistics).
6. Fidelity floor, latency tail, and clip constants after calibration.
7. Whether `tool_digest` artifacts are scored as a separable lane so a miner
   with a great compression table but no routing wins something.
