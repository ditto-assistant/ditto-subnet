# No-inference competition mode

Status: **design, phase 1 in progress.** Two decisions are open for Peyton (see
*Decisions*). Nothing here changes a live score, weight or setting.

## Goal

A v15 canonical run costs about **$1.43** (49 accepted v15 canonical runs over
2026-11-02..04, Backroom `get_inference_runtime_metrics`): 16.8M prompt and
1.36M completion tokens of locked `openai/gpt-oss-20b` per 350-case run, plus
$0.001 of embeddings. Almost all of it is chat. The 200-connector catalog
(~16k tokens) resent on every turn is the largest single line.

The no-inference mode removes the chat model. An agent gets two hosted lanes:

- **Decisions**: the TypeSafe Jev decision lane (`typesafe/jev-1.14`, $0.042/M
  input tokens, output free). It answers typed questions about a state:
  `choice` (pick one labelled option, with probabilities), `noul` (yes/no
  probability) and `score` (an ordinal scale). `box` is a visual bounding box,
  not a text span (probed 2026-11-04), so it does not help with text.
- **Embeddings**: `perplexity/pplx-embed-v1-0.6b`, $0.004/M tokens.

Everything else is host code: retrieval, candidate extraction, arithmetic,
formatting and tool execution. Measured cost of the baseline prototype below is
about **$0.07 to $0.11 per 350-case run, 13x to 20x cheaper than v15 chat**.

Peyton's framing: "we dont even have to delete our code it could be a mode. but
i dont care that its a breaking change its time 2 evolve. the usecase is nice".

## What exists today

| Piece | State | Gap for this mode |
| --- | --- | --- |
| Decision lane | Live since v15. Board revision 41: enabled, `typesafe/jev-1.14`, budget 4,096 requests and 25M tokens per run, concurrency 8 per ticket, 32 per validator and 64 global, 600/2,400/6,000 RPM. **Zero calls in the last 7 days across 314 grants with the lane.** | No harness uses it, so its rails have never run under real load. |
| Broker `/decisions` | Signs with the run grant, 16 in flight per session. Since v16 it books decision usage for the priced-cost factor. | Decision calls carry no case attribution (`traceContextLocked(..., "", "", "")`). The route is 404 on confirmation and ablation sessions. |
| Relay chat gate | Model allow-list only. | **Phase 1 (#3375):** `inference_grants.inference_mode = 'no_chat'` refuses chat with 4107 on the Go relay and the Python path. Embeddings and decisions stay open. |
| `model_use` gate | Enforced. It counts **chat** only (`v9AggregateModelTelemetry`). | A no-chat run reads `zero_inference`, so its composite is exactly 0.00 today. |
| Route challenge | A zero-chat interval is re-probed with a chat route challenge. | Needs a decision-lane probe for no-chat runs. |
| Claim provenance and `answer_in_prompt` (v13+) | Shadow. They check that served claim tokens came from model **completions** and were not authored into the prompt. | The "model completion" becomes the Jev-chosen option. See *Grading changes*. |
| v16 priced cost | Prices decision input at 42 nano-USD/token. Reference is $1.00/run, floored at 0.6. | A no-chat run already sits at factor 1.0. A track could lower the reference. |
| Tracks | `memory`, `generator` (paid), `coding`, `router` (reserved). Live split memory 7000 / generator 3000 bps of the track pool, which is memory 56% / generator 24% / Gamma gm 20% of emission. | No mode dimension on the memory ledger. |
| Upload | Tarball + Dockerfile. No manifest. The screener enforces the root Rust package contract. | No way to declare a mode. |

## 1. Mode shape

### Options

**A. A new track, `memory_noinfer`, on the same bench contract.** Same
datasets, grader and seeds as the chat track. The agent declares its mode at
upload, and its grants are minted `no_chat`. Its ledger rows form a separate
board and crown with their own emission share.

**B. A bench variant (bench 17 = decision-only).** Every agent runs no-chat
from activation. One board. The breaking change Peyton is willing to make.

**C. One board, mixed modes.** Rejected. The top chat agents score 0.97
composite and 0.95 memory (live board 2026-11-04). The measured no-chat
ceiling is far lower (section 2), so a mixed board pays only chat agents
unless the cost factor is made punitive enough to invert it. That would
re-weight the board on cost instead of capability.

### Recommendation: build the mode (A), keep B as a share decision

Build `inference_mode` as a **run dimension** that every layer carries:

- the agent row;
- the ticket and grant (`no_chat`, phase 1);
- the scorer request and signed evidence;
- the ledger entry;
- the validator track fold.

Then:

- **A** is a track with a nonzero share.
- **B** is the same machinery with the chat track's share at 0 and no new chat
  leases. It needs no second contract fork, and it can be reached gradually.

Starting with A costs little extra. Starting with B commits every miner,
LongMemEval confirmation and the generator panel to a contract whose ceiling
has not been measured on real agents.

**Contract versioning: mode, not a new bench version.** No-chat scoring adds
a gate set (decision use, decision provenance, the decision route challenge)
under its own contract id, `no-chat-v1`, carried in the signed evidence like
`priced-cost-v1`.

- Chat-mode evidence omits the field, so every v15/v16 chat score stays
  byte-identical.
- `no-chat-v1` is valid from bench 15 up, because the decision lane exists
  from v15. The mode can launch on whatever bench is active when it ships
  (v15 now, v16 after activation) without a version bump.
- It does not touch v16 files: the v16 dueling work continues independently.

If Peyton prefers B, the same gate set ships as bench 17's base contract
instead.

### Leaderboard and crown

- A separate board `memory_noinfer`: same `n`, quorum (3), composite and
  stderr, and the same crown rules (3 shared-seed confirmations to dethrone,
  champion standing rank 3) and rank shares (0.5/0.2/0.13/0.1/0.07).
- Backroom `get_leaderboard` gains `track` (default `memory`). The public board
  gets a tab.
- Validators fold `memory` entries filtered by mode as two tracks
  (`TrackFoldInputs.memory_noinfer_entries`) under a new validator protocol.
  The Platform track-share gate already refuses a share until every live
  weight setter reports the paying protocol, the same as generator at 33.
- The crown, champion pool and ATH review run per track. A no-chat champion
  never dethrones a chat champion, or the reverse.

### Emission share (Peyton decides)

The track pool is 80% of emission today (memory 7000 / generator 3000 bps of
it). Options, in bps of the track pool:

| Option | Split | Effect |
| --- | --- | --- |
| 0. Shadow | memory 7000, noinfer 0 (folded and logged only), generator 3000 | Observe a real board first. Recommended for the first 2 weeks. |
| 1. Carve 10% | memory 6000, noinfer 1000, generator 3000 | 8% of emission. Enough to attract builders without moving the chat board much. |
| 2. Carve 20% | memory 5000, noinfer 2000, generator 3000 | 16% of emission. Signals the direction. |
| 3. Migration schedule | memory steps down 1000 bps every 2 weeks into noinfer once the noinfer board has 5 ranked agents | Ends at option B (chat at 0) without a flag-day break. |

Recommendation: **0 → 1 → 3**, with option 3's steps gated on the noinfer
board's depth (at least 5 ranked agents) and on cost data, not on a calendar.

### Opt-in

Use a file in the tarball root, `ditto-agent.toml`:

```toml
inference_mode = "no_chat"   # absent or "chat" = today's mode
```

- The tarball SHA-256 binds it, so it is immutable per artifact and is visible
  to the screener, copy court and reviewers.
- It needs no change to the CLI signature payload (`ditto-upload-v2:...`).
- Upload parses it during the existing tar-member scan (cheap, before
  payment). `/upload/check` reports it, and the agent row stores it.
- The screener verifies it against the build. A `no_chat` image that ships a
  generative model is held under N2 (section 3).
- A no-chat upload counts against the same hotkey cooldown. It is a separate
  submission, not a sibling.
- `GET /api/v1/public/submission-terms/memory` documents the field.

### Enforcement

- **Mint** (phase 2): an agent with `inference_mode = no_chat` gets grants with
  `inference_mode = 'no_chat'`. The decision pin is required, enforced by the
  phase-1 CHECK. If the operator's decision board is off, Platform does not
  lease no-chat agents (the v15 rollout blocker pattern).
- **Relay** (phase 1, done in #3375): chat on a `no_chat` grant answers 4107
  `model_not_permitted` before provider, ledger or route work. Embeddings and
  decisions keep their own gates.
- **Broker**: `inference_mode` rides on the exchange response. The broker
  answers chat locally with the same 4107 shape, saving a round trip, and
  exposes `DITTOBENCH_INFERENCE_MODE=no_chat` to the sandbox.
- **Confirmation (LongMemEval)**: decisions are 404 on confirmation sessions
  today. Either confirmation grants get the decision lane for no-chat agents,
  or the noinfer track runs without the LongMemEval dimension until it does.
  Recommend the first in phase 3, since confirmation is a permanent dimension
  of every bench.

## 2. Answers without chat

### How a harness produces graded answers

The grader already reads the structured `answer` slot first and falls back to
`final_text`, by normalized bounded containment. It also honors `abstain`. No
grading change is needed for a value that **is** a span. The harness's job:

1. **Retrieve**: BM25 and/or embeddings over seeded records and earlier
   `/run` turns.
2. **Propose candidates**: split records into clauses, then spans within the
   chosen clause. Dates, money, emails, identifiers, quantities. For computed
   families, host arithmetic over extracted operands under each plausible
   reading.
3. **Decide with Jev**:
   - `noul` "do the records answer this?" for abstention;
   - `choice` over clauses, then over spans;
   - `choice` over computed candidates for arithmetic.
4. **Emit**: `answer` = the chosen span, verbatim. `final_text` = a template
   around it. `abstain: true` when the abstain decision is below threshold.

### Which v15/v16 families are solvable

The extractive ceiling is the share of cases whose graded value(s) appear
verbatim (normalized) in their own evidence records. Over 1,000 v15 memory
cases (seeds 42, 7, 1234, 99001, full profile):

| Bucket | Share | Families | No-chat path |
| --- | --- | --- | --- |
| Extractive | **64.5%** | owner/contact/status current, x-owner-email, order, point-in-time contact, parser-divergence (retraction, hypothetical, reported speech), twins, declarative behavior, injection data-inside-envelope, most personal and open programs | Jev choice over clauses and spans |
| Abstain | 10.0% | absence (pure, near-miss, stale-removed, false premise, cross-user, insufficient composition) | Jev `noul` + `abstain: true` |
| Conversational | 2.4% | chitchat (credit capped at 0.5 by policy), declarative acknowledgements | template + echo the Jev-chosen value |
| Composed value/list | 8.7% | next-action triples, multi-hop (owner → email), lesson claims | several Jev choices, one per slot or hop |
| Computed number | 7.6% | quantity, record-quantity cf, trip durations, enterprise sums | host arithmetic, Jev picks the reading |
| Computed money | 6.8% | point-in-time invoice, remaining balance, family compiler money | host arithmetic, Jev picks the convention |

The v16 datagen half (seed 42, dueling profile, 125 memory cases) is 62.4%
extractive, the same picture. The v16 generator slice is generator-authored
and was not measured.

Tool cases (100 per v15 run) are mostly selection out of ~235 tools plus
arguments that are spans of the prompt, of stored state or of a tool result.
For example, `calendar_create_event {title: "dexia path rollout"}`,
`run_workflow {name: <from list_workflows>}`, `set_reasoning_effort
{effort: <stored default>}`, `delete_memory {pair_id: <a stored record>}`. A
no-chat tool loop:

1. Shortlist the catalog by BM25 or embeddings.
2. Jev `choice` over the shortlist plus `no_tool`.
3. Fill arguments with Jev `choice`: over enum values, over prompt and result
   spans, and over stored record ids for `pair_id`.
4. Execute through `tool_endpoint`.
5. Jev `noul` "is the request done?", up to 3 hops.

Free-text arguments (an email body) and result-usage replies are templates
around chosen spans.

### Expected ceiling

| Axis | Ceiling for a well-engineered no-chat agent | Why |
| --- | --- | --- |
| memory_mean | **0.70 to 0.80** | 64.5% extractive at about 0.85 selection, 10% abstain at about 0.7, part of the computed 23% |
| tool_mean | **0.75 to 0.90** | Selection and span arguments suit `choice`. Multi-hop result usage is the hard part. |
| composite | **about 0.65 to 0.80** before factors | |

The chat board tops out at 0.97, so the two boards are not comparable. That is
why the shape is a separate track.

### Baseline prototype (measured)

[`no-inference/prototype_harness.py`](no-inference/prototype_harness.py)
(Python stdlib, a measurement prototype, not a submittable agent) works like
this:

- BM25 retrieval, no embeddings.
- A two-stage Jev choice: which record clause answers, then which span of it.
- Jev routing of each `/run` into recall, act, remember or chat.
- A shortlist and Jev tool loop with Jev argument filling.
- `/run` turns persisted to memory, so declarative turns can be recalled.

It ran against the real `typesafe/jev-1.14` through OpenRouter System One and
the Linux scorer in **practice scope** (`SCORED=0`, so v9-base gates are not
administered). No chat call was made.

| Run | n | composite | tool_mean | memory_mean | Decision calls | Input tokens | $/case | $/350-case run |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| v15 small, seed 42 | 33 | 0.170 | 0.333 | 0.312 | 93 | 244k | $0.00031 | $0.11 |
| v15 medium, seed 42 | 143 | 0.109 | 0.267 | 0.304 | 477 | 660k | $0.00019 | $0.07 |
| v15 medium, seed 7 | 143 | 0.140 | 0.299 | 0.276 | 493 | 714k | $0.00021 | $0.07 |

Reproduce:

```bash
# extractive ceiling (section 2 table)
cd research/dittobench-datagen && for s in 42 7 1234 99001; do
  go run ./cmd/generate -bench-version 15 -seed $s -run-size full -out /tmp/v15-$s.json; done
python3 docs/no-inference/extractive_ceiling.py /tmp/v15-{42,7,1234,99001}.json

# prototype: the scorer needs Linux (tree-sitter cgo), so build it in golang:1.27.3
# and run it with the stub relay (no chat is ever called); the harness needs
# DECISIONS_URL pointing at a System One endpoint (OpenRouter + a dev key).
# Then POST /v1/submit {harness_url, bench_version: 15, run_size, seed} (practice).
```

Read these as a **floor**: about one hour of prototype work, lexical retrieval
only, and no arithmetic. The composite is also held down by the
`conversational_sanity` factor (0.61) and `memory_over_call` (0.75), both
fixable product behavior. Jev latency was about 200 ms per call, with zero
provider errors over about 1,060 calls. Routing was right on 30 of 33 small
cases. On medium it sent 93 cases to recall and 41 to act, against a true split
of 95 memory and 48 tool cases (aggregate counts only).

## 3. Integrity policy (screener policy delta)

Policy v13 assumes a model writes the answer. I1 (model-authority bypass), I3
(dissent suppression) and I4 (authority transfer) all protect a *chat model's*
authority over the finalized answer. In no-chat mode, host computation is the
point, so the deciding authority is redefined. The source-review question
moves from "did the model write it?" to "did general product logic plus a
genuine decision produce it, without benchmark knowledge?"

Draft **policy `noinfer-1`**, an overlay on v13 that applies only to
`inference_mode = no_chat` artifacts:

**Unchanged and fully enforced**

- I2 (trusted instruction or authorization loss)
- I6 (execution misrepresentation)
- I8 (prohibited evaluation dependence: no case ids, seeds, `bench_version`
  branching or audit detection)
- S1 (cross-user access) and S2 (exfiltration)
- Q1, W10 (catalog fidelity), W11, W12, W14 (build and runtime integrity),
  W15 (user isolation), W16 (evaluation independence)

**Redefined**

- **Deciding authority**: the TypeSafe Jev decision lane, *or* deterministic
  host logic that is general product behavior. A harness may answer without a
  Jev call when its general extraction finds exactly one candidate.
- **I1 → D1, decision bypass**: reject when a scored answer, abstention or
  tool choice is supplied by a benchmark answer lookup or precomputed table.
  Also reject when Jev is called ornamentally and its answer is ignored or
  overridden. The decision-dependence counterfactual (below) is the
  behavioral lead.
- **I3 → D3, decision suppression**:
  - The host may not re-ask Jev until it agrees with a host-preferred value.
  - It may not override a valid Jev choice toward a preferred answer.
  - It may not pick a lower-probability option when the product rule does not
    say so.

  Asking several independently-framed questions and combining them by a
  published rule (for example majority, or max probability) is permitted.
- **I4 → D4, post-decision rewrite**: once Jev chooses a candidate, the host
  may format it but not change its value: a currency symbol and thousands
  separators are allowed; a unit conversion requested by the user is a new
  candidate, not a rewrite. W8 serialization still applies.
- **I5, the central rule**: host computation is permitted only when it is
  general. That includes:
  - general extractors (dates, money, names, ids, clause splitting);
  - general calculators;
  - general recency and supersession ordering by timestamp;
  - general parsers for CSV, Markdown and JSON.

  Rejected when benchmark-shaped:
  - phrase tables keyed on generator wording or coined glossary terms;
  - family classifiers that switch answer recipes;
  - candidate filters derived from the generator's distractor construction;
  - renderer-template parsers (for example a regex for the generator's
    `Case file ...: Before the ... lands` sentence shape);
  - fixed answer templates per question family.

  Rigged candidate sets are I5. That is a choice whose options were filtered
  by benchmark knowledge so that only one is plausible.

**New**

- **N1, no generative stand-in**: the image ships no generative text model
  (decoder LM) and calls no external inference endpoint. Encoders, rerankers
  and classifiers are permitted (the stock kit's TinyBERT reranker is the
  precedent), with a size cap Peyton sets (suggest 300 MB). This keeps "no
  inference" from becoming "a local chat model on validator CPU".
- **N2, mode honesty**: the declared mode matches the build. A `no_chat`
  artifact that attempts chat (4107 in traces) is not a violation by itself,
  since the relay refuses it, but repeated attempts are a held lead.
- **N3, no learned benchmark heads**: a classifier or reranker trained on
  DittoBench outputs (public practice included) that selects answers or
  trajectories is the I7 "benchmark-fitted learned head". General-purpose
  pretrained encoders are fine.

**Scorer gates for `no-chat-v1`** (shadow first, then penalize, then enforce,
per the calibration rule):

| Gate | Replaces | Rule | Initial posture |
| --- | --- | --- | --- |
| `decision_use` | `model_use` | Successful decision calls attributed to at least 80% of eligible cases. A choice with one option does not count. | enforce (zero), same as `model_use` today |
| `decision_route_challenge` | chat route challenge | A discarded decision probe proves the harness reaches the broker, so zero use is never an adapter mismatch. | enforce |
| `decision_provenance` | claim provenance | The graded claim tokens lie inside the label of a Jev-chosen option attributed to the case, or inside the user prompt for echo cases. | shadow |
| `decision_dependence` | `model_dependence` | The broker perturbs a slice of decision answers (for example to the runner-up option) and checks that the served answer moves. | shadow |
| `answer_in_prompt` | | Retired for no-chat. Candidates are supposed to be in the question, and the record-token exemption already covers record values. | not applicable |

`decision_use` and provenance need a case attribution header on decision calls
(`X-Ditto-Case-Id`, as chat uses), verified by the broker against the cases in
flight.

## 4. Cost and capacity

### Per-run profile

| | v15 chat (measured) | No-chat prototype (measured) | No-chat, strong agent (estimate) |
| --- | --- | --- | --- |
| Chat prompt / completion tokens | 16.8M / 1.36M | 0 | 0 |
| Decision input tokens | 0 | 1.6M to 2.6M | 2 to 5M |
| Decision calls | 0 | about 1,200 | 1,000 to 2,500 |
| Embedding tokens | 0.31M | 0 (BM25) | 0.5 to 2M |
| **$/350-case run** | **$1.43** | **$0.07 to $0.11** | **$0.09 to $0.22** |

The upper estimate covers a harness that sends whole retrieved records as
state on every decision, and multi-call self-consistency. Jev adds about 400
template tokens to a small request. Decision cost scales with **state size**,
not answer length, so the harness pays for context exactly as a chat harness
does, just about 40x cheaper per token.

### Lane limits and changes

| Rail | Today | Load from one no-chat run | Change needed |
| --- | --- | --- | --- |
| Request budget per run | 4,096 | 1,000 to 2,500 | none (raise to 8,192 if multi-hop agents hit it) |
| Token budget per run | 25M | 2 to 5M | none |
| Per-ticket concurrency | 8 | case_concurrency × 1 (decisions are sequential within a case) | **raise to 32** for no-chat grants, matching chat; the broker's 16-per-session cap moves with it |
| Per-validator concurrency | 32 | | **raise to 128** |
| Global concurrency | 64 | about 8 to 16 per run in flight | **raise to 256**; 64 saturates at 4 to 8 simultaneous no-chat runs |
| Per-ticket RPM | 600 | 1,000 calls in about 10 minutes is about 100/min average, with bursts of about 1,500/min at 32 in flight × 200 ms | **raise to 1,920** (chat's value) |
| Global RPM | 6,000 | | **raise to 20,000** once the provider confirms headroom |

These are operator settings (`set_inference_concurrency_settings`). This plan
changes none of them.

Provider risk: Jev has one provider (TypeSafe via OpenRouter). An outage
voids no-chat runs fleet-wide; the provider-outage-never-miner-fault rule
applies. Before any nonzero share, confirm TypeSafe's RPM and TPM headroom for
about 20k RPM. The per-run walls stamped at mint keep a runaway harness
bounded at $1.05 (25M × $0.042).

## 5. Product fit

Ditto's live product needs cheap, fast memory retrieval and decisions on every
turn: which memories matter, whether the answer is on record, which tool to
offer, which value to fill. A chat model is the expensive, slow way to make
those calls. This mode trains miners to build exactly that layer:

- retrieval and candidate generation over a user's real memory graph;
- typed decisions with calibrated probabilities (Jev returns them), which
  gate whether a chat model is needed at all.

A strong no-chat agent is directly a **pre-chat router and memory resolver**
for production. Run it first. Answer outright when it is confident and the
question is extractive. Escalate to chat only when composition or prose is
required. At $0.0003 per case against $0.004 for chat, that is the cost
structure Ditto wants on every user turn. The noinfer board's per-case
confidence data also tells us which families genuinely need a generative
model.

## 6. Rollout

| Phase | Work | Needs a decision? |
| --- | --- | --- |
| **1. Primitives** | `inference_grants.inference_mode` plus relay and Python chat refusal (#3375). This doc. A baseline prototype, measured (above). | no |
| **2. Mode plumbing** | Parse `ditto-agent.toml` at upload, store it on the agent row, show it in `/upload/check`. Mint `no_chat` grants and expose the mode on the exchange. Broker local 4107, `DITTOBENCH_INFERENCE_MODE`, decision case attribution. Backroom: mode on agent, grant and decision-lane reads (`live_no_chat_grants`). | mode vs bench (the opt-in exists only under A) |
| **3. Scoring** | `no-chat-v1` evidence: `decision_use`, the decision route challenge, provenance and dependence in shadow. A decision lane on confirmation grants. Python mirror in `ditto-screening-protocol`. | no (contract shape is in this doc) |
| **4. Track** | Ledger `track` on memory entries. Validator protocol bump with a `memory_noinfer` fold. `KNOWN_TRACKS`/`PAYABLE_TRACKS`. Leaderboard `track` filter. Crown per track. Share served at 0 (shadow). | emission share |
| **5. Starter kit** | `miners/dittobench-noinfer-starter-kit` (Rust, the screener's root package contract): a port of the prototype with embeddings, plus `ditto practice --inference-mode no_chat` rehearsal. Starter provenance manifest. `mine` skill, `docs/MINER.md`, PROTOCOL section "No-chat agents". | no |
| **6. Policy** | `noinfer-1` overlay published, with 14 days' notice before enforcement. Screener manifest check (N1, N2). Backroom review bar addendum. | the N1 size cap |
| **7. Share** | Option 0, then option 1 when the board has 5 or more ranked agents. Option 3 steps after that. | yes, each step |

Miner announcement (draft for phase 5):

> A new SN118 lane, **no-chat agents**. Your harness gets the TypeSafe Jev
> decision model and embeddings, and no chat model. Answers come from your
> retrieval plus Jev's choices. Declare `inference_mode = "no_chat"` in
> `ditto-agent.toml`. Separate leaderboard, separate crown. Runs cost about a
> tenth of a chat run. Starter kit and rules: `<link>`.

## Decisions for Peyton

1. **Mode vs bench.** A: a `memory_noinfer` track beside chat, with a
   migration path to B by share. B: bench 17 is decision-only for everyone.
   **Recommend A**: it reaches B through option 3 without a flag day.
2. **Emission share.** Options 0 to 3 above. **Recommend 0 (shadow) at
   launch, then 1 (memory 6000 / noinfer 1000 / generator 3000) at 5 ranked
   agents.**
3. Smaller ones:
   - the N1 local-model size cap (suggest 300 MB);
   - the opt-in spelling (`inference_mode = "no_chat"` in `ditto-agent.toml`);
   - whether no-chat agents join the v16 generator-slice panel (recommend no
     until the noinfer board is ranked: generator columns should keep being
     measured by chat agents).
