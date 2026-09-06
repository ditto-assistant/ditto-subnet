# Router & Compression competition v1 (shadow contract)

Status: proposed shadow-only competition dimension. This document does not add
a project to the active upload protocol, issue a lease, run a miner, alter the
Tool + Memory composite, or change validator weights. Router contract v1 is
permanently `weight_eligible=false`.

Board: https://github.com/orgs/ditto-assistant/projects/10
Epic: https://github.com/ditto-assistant/ditto-subnet/issues/1664

## Why

The Ditto inference gateway ("dittorouter", `backend/pkg/services/inference`,
served at `api.heyditto.ai/v1/messages`, `/v1/chat/completions`,
`/v1/responses`) sits between coding harnesses (Claude Code, Codex) and model
providers. Every dollar it saves is measured with real headless Claude Code
runs on the inference cost bench (`heyditto-stack` skill
`inference-cost-bench`). What one team shipped in a week:

| Lever | Measured effect (Sonnet 5, 2026-09-05/06) |
|---|---|
| Native Messages route that preserves `cache_control` | 0 → ~95% cached input; README task $0.45 → $0.18 |
| Memory recall once per user turn, re-attached byte-identically (ledger) | 5-task suite $4.31 → $0.71 |
| Shape-based request archetypes + per-archetype routes (asides/probes → GLM 5.3 Flash) | title prompt ~10x cheaper |
| Tool-description compression (one condensed rewrite per unique tool hash) | prefix −16%, 8/8 tasks still pass |
| Deterministic context compaction of completed-turn tool results | large-read 3-turn task $0.95 → $0.67 |
| Compaction archetype + proactive compaction snapshots (experimental) | compaction served from a snapshot in seconds; cheap models lose detail |
| Deterministic tool-result compression at ingestion | 97% removal on a 110 KB log read, 22% on Bash output |

Every lever lives in one service with one job: accept a harness request on a
provider-compatible wire and decide what to send upstream. SN118 already knows
how to run a competition on an immutable artifact behind a public HTTP contract
with validator-owned execution, deterministic grading, hidden material, and
shadow before weights. Miners should compete on the whole gateway, not on one
of its knobs.

## The submission: a third project

The unified submission today carries the Tool and Memory projects (one screened
image serving `GET /health`, optionally `/coding/health`). Router contract v1
adds a **third project: a complete dittorouter implementation**. It is a
multi-codebase competition; a submission may be router-only, agent-only, or
both.

The router project is a service, in any language, that exposes the same
provider surface Ditto exposes at `api.heyditto.ai/v1`:

```text
GET  /router/health                          advertisement (contract versions, wires served)
POST /v1/messages                            Anthropic Messages (streaming and non-streaming)
POST /v1/messages/count_tokens               Anthropic token counting
POST /v1/chat/completions                    OpenAI Chat Completions
POST /v1/responses                           OpenAI Responses
POST /router/seed                            validator-supplied memory records for the task (may be empty)
```

Validators run the **real, latest pinned harnesses** — Claude Code with
`ANTHROPIC_BASE_URL`, Codex with `OPENAI_BASE_URL` — pointed at the miner's
router. Whatever the router does between the harness and the provider
(rerouting, coding-agent-specific optimisations, deterministic harness-specific
compression, compaction, memory placement, side calls to cheaper models) is the
miner's business, under the gates below.

The router may reach exactly one upstream: the **validator's locked,
ticket-scoped relay**. The relay is injected as `DITTOBENCH_RELAY_BASE_URL`
plus a single-use bearer `DITTOBENCH_RELAY_TICKET`; it speaks the same four
provider routes, enforces the frozen catalog and budget, and logs every
upstream body. No provider credential exists in the miner image or the
sandbox. The relay is the only place the miner's behaviour is observed, which
is what makes the observation trustworthy.

```json
{
  "status": "ok",
  "supported_router_contract_versions": [1],
  "wires": ["anthropic_messages", "openai_chat", "openai_responses"],
  "count_tokens": true
}
```

`404 /router/health` means no router project; no penalty, no router score. A
malformed advertisement yields no router attestation and cannot touch a normal
score. Unknown fields are ignored (`extra="ignore"`); known fields are
validated, normalized, and bound to the exact screened image digest.

### Open question: packaging and admission

Whether the router project ships as a second `Dockerfile` in the same ≤ 20 MiB
build context (`router/Dockerfile`), as a second screened image from one
upload, or as a separate upload bound to the same hotkey is an owner decision;
the contract only requires that the router runs as its own container with its
own screened digest. Likewise whether router admission requires Tool + Memory
core qualification: this document proposes source-integrity screening plus the
public router canary only, so a router-only submission is admissible.

## Hard constraints (learned in production)

These are gates, not scoring dimensions. A session that violates one ends as
`candidate_integrity` for that task.

1. **Harness compatibility.** Claude Code decides when to compact from the
   `usage` the router reports, expects every `tool_use` id and block it sent
   to still be honoured, marks its cache breakpoint on the last block of the
   last user message, and calls `count_tokens` and the title/aside prompts
   with distinct shapes. Codex expects Responses items intact. The harness is
   the conformance suite: it either finishes the task or it does not.
2. **Determinism at the relay.** Replaying the same harness step must produce
   byte-identical upstream bodies. Every transform applied to a completed turn
   must be re-applied identically on every later re-send, or provider prefix
   caches stop hitting and the "savings" invert.
3. **Fidelity is task pass rate.** Token counts are a cost, not a quality
   signal. The only fidelity measure is whether the harness still solves the
   task through this router, at parity with the reference router.
4. **No prompt or memory contamination.** Text the router adds upstream must
   be derivable from what the harness sent or from the seeded memory records.
   Compaction instructions recorded as memories were later recalled as
   "prompt injection" and the model refused the real compaction; a router
   must not be able to reproduce that failure and be rewarded for it.
5. **Provider quirks are real.** GM misses the cached prefix when a
   `cache_control` block is followed by another block; Anthropic
   `clear_tool_uses` context edits return `applied_edits: []` through
   OpenRouter and GM; Haiku's cache minimum is 4096 tokens. The catalog
   encodes these; cost is what the provider actually billed.

## The relay contract

The relay is validator-owned and reuses the `dittobench-api` broker
(`DITTOBENCH_REQUIRE_TICKET_INFERENCE=true`, Luna relay, native hosted
inference authority). Router contract v1 asks it for:

- **Four provider routes** mirroring the router's surface, streaming and
  non-streaming, with provider `usage` passed through unchanged.
- **Catalog enforcement.** The request's `model` must name a catalog
  `route_id`; the relay resolves it to `{wire, provider_api, provider_route,
  provider_route_profile, allow_fallbacks=false, zdr}` and rejects anything
  else as `route_not_in_catalog`. The catalog is frozen per task revision and
  shaped like `coding_inference_policy_locked_v1` (integer micro prices per
  input / cached-input / output token, `cache_min_tokens`, quirk flags,
  `cost_source: provider_receipt_v1`, `max_cost_usd_micros`).
- **Budget.** One ticket, one budget: every upstream call the router makes for
  a session — main-model turns, `count_tokens`, title and compaction side
  calls to cheap models — draws on the same `max_cost_usd_micros` and request
  cap. Exceeding either ends the arm as `budget_exceeded`; the harness sees a
  clean provider error, not a hang.
- **Full logging.** For every upstream call: ticket, monotonic sequence,
  route, canonical request bytes and digest, response digest, provider
  receipt (tokens in / cached / out, micros, latency), and the
  `X-Dittobench-Step` the ingress tap stamped (below). This log is the
  evidence for every offline check.

The relay is not a harness: it never rewrites bodies, never retries
(`no-automatic-retries.md`), never falls back.

## What the validator observes

```text
harness container ──► ingress tap ──► miner router container ──► relay ──► provider
   (Claude Code /        (validator,       (miner, no other           (validator,
    Codex, pinned)        records every     egress)                    logs every
                          harness request)                             upstream body)
```

`ANTHROPIC_BASE_URL` / `OPENAI_BASE_URL` in the harness container point at the
**ingress tap**, a validator-owned transparent proxy in the sandbox network
namespace that records each harness request and response byte-for-byte,
stamps a `X-Dittobench-Step` (session, turn, request index) that the router
must echo on its upstream calls, and forwards to the router unchanged. The
miner's `ANTHROPIC_BASE_URL` is therefore the tap; the tap is invisible to
the router except for the step header.

With tap and relay logs the validator can compute, without cooperation from
the miner, what the router did to every request. That replaces the typed
`transforms[]` array an earlier draft required the miner to declare.

### Observable transform ledger

For each harness step the validator diffs the tap body against the upstream
body (or bodies) carrying the same step and classifies the difference with a
deterministic, published classifier:

| Observation | Ledger kind |
|---|---|
| `model` differs from the harness's model | `route` |
| A tool `description` differs, `name` and `input_schema` equal | `tool_digest` |
| A completed-turn `tool_result` content differs, `tool_use_id` equal | `result_digest` / `context_compaction` |
| A block appears that is verbatim seeded-memory content | `memory_placement` |
| `cache_control` moved or added | `cache_marks` |
| An upstream call with no tap counterpart on that step | `side_call` (title, compaction, probe) |
| Anything else | `unclassified` (reported; gated by the contamination rule, not forbidden per se) |

The ledger is reported to the miner and to Backroom per arm; it is how a
miner learns which of their levers paid. Ditto's own savings ledger uses the
same kinds (`kind_route`, `tool_compression`, `context_compaction`,
`memory_ledger`, `result_compression`, `precompaction`).

## Task material

A router task is a **coding task plus a pinned harness**, not a recorded
trace. Replaying a recorded trace cannot score a router, because a changed
request changes the model's reply and every later turn. Task records reuse
the coding catalog shape (content-addressed
`router-catalog/v1/<commitment>/records/<index>.json`, position-bound Merkle
membership proofs, commit-then-future-block selection, append-only exposure
ledger) with a `harness` section added:

- repository epoch, task text, hidden trusted tests, runtime policy
  (editable paths, argv-only test/build commands, `network=none` for the
  task's own tool sandbox);
- `harness`: family (`claude_code` | `codex`), exact pinned version and image
  digest, launch flags, system-prompt hash, tool set hash, multi-turn script
  (`---turn---` boundaries), autocompact window if forced;
- memory profile: seeded records (`coding_memory_v1` shape, conditions
  `required_constraint | relevant_nonrequired | irrelevant | stale_conflicting
  | current_override`) delivered to the router via `/router/seed`, plus one
  canary record;
- catalog revision digest and budget.

Public practice packs are fully inspectable (`task_entropy_bits=0`); the
hidden validator partition is disjoint, authored under
`coding-private-authoring-v2.md` gates, withheld from miners.

## How validators evaluate

### Arms

```text
T tasks x {reference router, candidate router} x R replicas
```

The **reference router** is Ditto's own gateway behaviour (the current
production strategy set, pinned per contract version and published as the
starter kit's default configuration). It runs in the same sandbox, behind the
same tap, against the same relay, in the same wave, so provider price and
latency drift cancel. v1 shadow uses `T = 8`, `R = 2` per validator (32
harness sessions), with tasks drawn so at least two need memory, two are
three-turn, one forces an autocompact, one is a large read, and both harness
families appear.

Each arm gets a fresh harness container, fresh router container, fresh
workspace, fresh ticket. No state crosses an arm boundary. The coding shadow
machinery is reused unchanged: lease, workspace freeze, pristine grade in a
separate grader container, terminal evidence, failure classifier, outbox,
fail-once.

### Sandbox topology and network policy

- **Three containers per arm** plus the validator-owned tap and grader:
  harness (task workspace mounted, tools sandboxed by the existing runtime
  policy: argv-only commands, `network=none` for tool execution, pids and
  memory limits), router (miner image, read-only rootfs, no writable mounts
  except tmpfs), relay (validator, existing broker).
- **Egress rules**: harness → tap only; tap → router only; router → relay
  only; relay → provider. DNS disabled inside the miner router; the relay is
  reached by injected address. Any other connection attempt is recorded and
  ends the arm as `candidate_integrity` (`egress_violation`).
- **Harness pinning per wave**: the wave manifest fixes the harness image
  digests; a Claude Code or Codex release is adopted by publishing a new task
  revision (same contract) unless the request shape changes, in which case
  the conformance vectors change and a new contract version is required
  (open question 2).
- **Side calls** are ordinary upstream calls on the same ticket. A router that
  answers titles with a flash model or runs its own compaction pays for it
  from the arm's budget and it appears as `side_call` in the ledger. A router
  that answers a harness request locally without any upstream call (a
  snapshot) is allowed; the ledger records `local_answer` and fidelity judges
  it.
- **Time**: wall clock per arm is bounded by the task's `wall_time_seconds`;
  router cold start counts against the candidate, so the router must be up
  within the health grace period before the harness launches.

### Gates (offline, over tap + relay logs)

All gates are computed from recorded bytes after the arm ends; none require
miner cooperation. A failed gate ends the arm as `candidate_integrity` with a
bounded `failure_code`:

- **Determinism** (`nondeterministic_upstream`): for a sampled 25% of steps the
  validator re-sends the recorded tap request to a fresh instance of the same
  router image with the same seed and expects byte-identical upstream bodies
  (excluding the step header and ticket).
- **Prefix stability** (`prefix_unstable`): for upstream request *n* on a
  route, the bytes representing every turn completed before request *n−1*
  must equal the bytes the router sent for those turns at request *n−1*. This
  is the cache-stability gate, measured on bytes, not on provider cache
  reports.
- **Harness compatibility**: the real harness end to end. Additionally, the
  router's responses to the harness (tap) must carry provider `usage`
  faithfully and must not drop or reorder `tool_use` blocks relative to the
  upstream response (`usage_misreport`, `response_mutation`).
- **Provider-surface conformance** (`surface_nonconformant`): a fixed vector
  set exercising `/v1/messages` (streaming SSE event order, `stop_reason`,
  `cache_control`, tool use), `/v1/messages/count_tokens`,
  `/v1/chat/completions` (streaming, tool calls, `usage` in the final chunk)
  and `/v1/responses` (items, `previous_response_id` absent, streaming
  events) exactly as the pinned harness versions use them, replayed against
  the router with the relay in record mode before any live arm.
- **Contamination** (`contamination`): text present upstream but absent at the
  tap must be either verbatim seeded-memory content or contain no token that
  is absent from the source request, the seeded records, and a small published
  connective vocabulary (`…`, `[N lines omitted]`, digits). This permits
  compression and digests; it blocks novel instructions. The canary memory
  record (`irrelevant`, unique nonce) must never appear upstream, in a memory
  block or anywhere else.
- **Catalog and budget** (`route_not_in_catalog`, `budget_exceeded`): enforced
  live by the relay, recorded for evidence.
- **Egress** (`egress_violation`): sandbox network log.

### Terminal evidence

Each arm ends in exactly one coding terminal domain: `resolved`,
`repair_failure`, `validator_infrastructure`, `task_invalid`,
`candidate_integrity`, `control_plane_integrity`. Provider, relay, tap, or
grader failures are `validator_infrastructure` and never improve or penalise
the miner; missing candidate evidence fails closed. The signed record binds
router image digest, harness image digest, task and condition commitments,
ticket, tap log digest, relay log digest, transform-ledger digest, frozen
workspace digest, pristine grade, and classification.

## Scoring

Public router tasks are qualification material only. The competitive score
uses the hidden validator partition. All quantities are integers (micros or
basis points), never floats, as in `V9BaseDetails`.

```text
RouterEligible =
  source-integrity pass
  AND public router canary pass
  AND provider-surface conformance pass for every wire the router advertises

solved(a)            = 1 if all trusted tests pass for arm a, else 0
cost_micros(a)       = sum of provider-receipt USD micros over every upstream call on the arm's
                       ticket, side calls included (cost_source = provider_receipt_v1; an unknown
                       receipt fails the arm as validator_infrastructure, never a fabricated zero)
tail_bps(t)          = 10_000 × (wall(candidate_t) − wall(reference_t)) / wall(reference_t)

FidelityGate         = 1 if solved_rate_bps(candidate) >= solved_rate_bps(reference) − 500  else 0
LatencyGate          = 1 if median_t tail_bps(t) <= 2_500                                    else 0

For each task t (paired, both arms resolved):
  saving_bps_t = 10_000 − 10_000 × cost_micros(candidate_t) / cost_micros(reference_t)
                 clipped to [0, 9_999]
For each task t where candidate is repair_failure or candidate_integrity and reference resolved:
  saving_bps_t = 0
Tasks where the reference is not resolved are excluded from the denominator.

ValidatorScore_bps   = mean_t(saving_bps_t)
FinalRouterScore_bps = IntegrityGate × FidelityGate × LatencyGate
                       × median over the validator quorum of ValidatorScore_bps
```

Notes:

- The saving is a ratio against a same-wave reference router, so it is
  stable across provider price changes and cannot be gamed by choosing tasks.
  The clip below `10_000` keeps the composite strictly under a perfect score,
  in the spirit of the asymptotic efficiency shape adopted in #884.
- Fidelity is gated, not blended. A router that solves fewer tasks has no
  score, whatever it saved. The 500 bps floor is a starting value;
  calibration sets it from the paired variance of reference-vs-reference
  waves.
- Latency counts because a route to a slow cheap model is a real regression
  for a developer; the 2 500 bps tail is likewise a calibration input.
- Ties use the existing protocol 20 evidence-tied pooling and shared-seed
  paired confirmation; nothing new is invented for rank.
- `IntegrityGate` is zero only for confirmed sandbox escape, hidden-test
  access, credential exfiltration, relay bypass, or evidence tampering, as in
  [`coding-screening-private-scoring-policy.md`](coding-screening-private-scoring-policy.md).

## Anti-gaming

- **The router never touches a provider.** One upstream, one ticket, one
  budget; the relay refuses routes outside the catalog and never falls back.
  A provider failure ends the arm as infrastructure, not as an opening for a
  second dispatch.
- **Hidden tasks, hidden harness prompts.** The scored partition is withheld;
  system prompt and tool set hashes in the hidden partition may differ from
  the public pack, so a router keyed on harness wording fails while
  shape-based detection keeps working.
- **Observation without cooperation.** Tap and relay logs are validator-owned
  bytes; determinism and prefix stability are replayed on a fresh router
  instance, so a router cannot present a cache-friendly face once and a
  cheaper one later.
- **Reference in the same wave** removes the incentive to time submissions to
  provider price windows.
- **Source integrity** reuses the screening court and anti-copy fingerprints
  with router deny rules: embedded provider clients or keys, any egress
  target other than the injected relay address, DNS, time/random/environment
  reads that affect upstream bytes, hidden-test or workspace probing.
- **Canary memory and the no-novel-token rule** close the contamination
  channel that bit production.

## How Ditto consumes winners

Router contract v1 is shadow-only; consumption is what the shadow evidence is
for. The gateway grows an `endpoint.router_policy` field:

```text
router_policy = ditto                      (today's gateway; default)
              | shadow:<router-digest>     (mirror the endpoint's traffic to the miner router
                                            behind Ditto's relay, log its ledger and cost delta,
                                            serve Ditto's response)
              | miner:<router-digest>      (serve the miner router's response; opt-in per endpoint)
```

- Ditto runs certified top-ranked router images as sidecars behind
  `api.heyditto.ai` with the same posture as the validator sandbox: no
  egress except Ditto's own relay, which is where billing, provider
  credentials, budgets and failover already live. The gateway keeps its own
  gates and falls back to `ditto` on any failure, recording the fallback in
  the savings ledger under a new `miner_policy` strategy kind.
- The transform ledger from shadow traffic tells Ditto which levers a winner
  found; the cheapest ones (a tool-digest table, a compaction plan shape) can
  also be ported into the reference router, which raises the bar for the
  next wave. The reference router is versioned so miners always know what
  they are beating.
- The savings ledger attributes every dollar to `ditto` or to a router digest
  per strategy kind, so the production delta of a champion is visible on the
  endpoint card and can feed a future reward policy.

## Phases

| Phase | Name | What is scored | Weight |
|---|---|---|---|
| v1 | Offline conformance and replay | Provider-surface conformance vectors; determinism and prefix-stability replay over the public pack's recorded tap logs; catalog and budget conformance at a record-mode relay. No live harness, no hidden tasks; diagnostic leaderboard. | none, `weight_eligible=false` |
| v2 | Shadow live evaluation | Real harnesses on hidden tasks in the sandbox topology above; paired reference/candidate arms; `FinalRouterScore_bps` published to Backroom only. Ditto runs the top routers in `shadow:` mode on opted-in endpoints. | none, `weight_eligible=false` |
| v3 | Production routing | `miner:` policy on opted-in endpoints; production savings ledger becomes calibration evidence for a separately reviewed weight-eligible contract version. | requires new contract version, calibration evidence, owner approval |

Project completion does not activate any phase beyond v1. Each promotion is an
owner decision recorded in Backroom.

## Metrics and telemetry

Validator-side (signed terminal evidence): per arm solved, billed micros,
tokens in / cached / out, wall time, upstream call count, side-call count,
gate outcomes, route histogram, transform-ledger histogram, router cold-start
time.

Platform/Backroom: router leaderboard with `FinalRouterScore_bps`,
per-validator `ValidatorScore_bps`, fidelity and latency gate states,
failure-code taxonomy, per-wave harness pins and reference-router version; a
`backroom:read` tool for every knob this document names (task count,
replicas, fidelity floor, latency tail, budget per arm, catalog revision,
harness pins, reference router version) so no state has to be inferred from
logs.

Gateway-side (production, v2+): savings ledger rows keyed by strategy kind
and policy source; fallback counts; shadow delta per endpoint per day.

## Reuse map

| Need | Existing machinery |
|---|---|
| Artifact upload, build, `/health` screen, anti-copy | normal submission pipeline, screening court |
| Additive capability advertisement | `/coding/health` pattern |
| Certification lease bound to exact screened image | `coding-qualified-certification-lease-shadow.md` |
| Public canary | `coding-public-v2-canary.md` sequence, with a public-pack harness session through the router in place of `/coding/run` |
| Hidden task material, leases, artifact delivery | coding private catalog, ticket sets, task leases, Hippius transport |
| Locked inference, receipts, budgets | `dittobench-api` broker, Luna relay, native hosted inference authority (extended with logging and four provider routes) |
| Tool sandbox for the coding task | coding runtime policy (argv-only, `network=none`, resource profile) |
| Arms, freeze, pristine grade, terminal evidence, classifier | `coding-private-shadow-execution-v2.md`, `coding-shadow-failure-classification.md` |
| Quorum median, ties, retries | validator quorum of three, protocol 20 pooling, `no-automatic-retries.md` |
| Contract vectors | `packages/dittobench-coding-contract` layout (`generate_*_vectors.py --check`, `testdata/*.json`, `.invalid` hosts) cloned as `packages/dittobench-router-contract` |
| Version authority | `research/dittobench-datagen/protocol/epoch.go` pattern: `RouterContractVersion` constants, known-vector CI test |
| Screening wire | `packages/ditto-screening-protocol` (`SourceReview*`, `coding_source_screen.py` pattern for a `router_source_screen`) |

## Open questions

Tracked on the epic; not decided here.

1. Router admission: source integrity plus router canary only (proposed), or
   Tool + Memory core qualification as well.
2. Harness pin policy: per-wave pins are fixed; when a Claude Code or Codex
   release changes request shape, is that a task revision or a new contract
   version?
3. Packaging of the third project: second `Dockerfile` in one build context,
   second image from one upload, or separate upload bound to the same hotkey.
4. Budget per arm and per wave. The bench measured $0.18–$1.02 per task on
   Sonnet 5 through Ditto's gateway; a naive router can cost several times
   that, so the per-arm cap decides how much a bad router can burn.
5. Shared reference arms per wave (cheaper) or paired per candidate (cleaner
   statistics).
6. Fidelity floor, latency tail, clip, and the no-novel-token vocabulary after
   calibration.
7. Whether the reference router is exactly production Ditto (then Ditto's
   own improvements move the bar mid-competition) or a pinned snapshot per
   contract version (proposed).
8. Whether the ingress tap should also be offered to miners locally
   (`uv run ditto router practice`) so their local ledger matches the
   validator's byte for byte.
9. Not in the repo: whether the gateway's shape-based archetype detector and
   savings-ledger strategy kinds are stable enough to publish as the ledger
   vocabulary; needs backend confirmation.
