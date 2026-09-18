# Conversational Continuity v1

## Product question

Can this memory harness sustain a useful relationship with a new user? A high
needle-retrieval score is insufficient if the assistant cannot understand a
correction, carry a decision into another discussion, or admit that it does not
know something. This assessment is a black-box product evaluation, separate from
source screening and cheating allegations.

## Instrument

GPT-6 Astra conducts exactly 30 exchanges, in ten three-turn sessions, starting
with an empty, randomly named memory graph. Ten onboarding exchanges establish
a synthetic user's projects, preferences, people and decisions. Twenty scored
probes test five dimensions. The schedule, rubric, budgets and model are pinned;
private seeded story details vary. No code, benchmark answers, leaderboard rank,
miner name or prior scores enter the judge context. Submission output is
untrusted evidence, never an instruction to the judge.

The judge's only interaction tool is `converse(turn_id)`. The host checks the
next ID, expands the story into ordinary prose, calls the submitted `/run`, and
ingests the actual exchange through `/seed`. It supplies only the current
session's transcript as conversational context. Earlier sessions are available
only through the submitted memory implementation. The driver does not summarize,
retrieve, repair or manufacture the agent's memories. This uses the existing
incremental-ingestion protocol; it measures conversational recall after ingestion,
not autonomous decisions about when to call a save-memory tool.

Story expansion saves Astra from generating long setup passages. It is a
versioned, seed-dependent fixture bank, not compressed text sent to the harness.
Identical schedule does not imply identical stochastic answers or grades.

## Rubric

Each probe receives an integer 0–4 with an exact quote from its response and a
specific rationale. Host code verifies coverage and quotes and computes all
scores; the judge cannot choose weights or report its own aggregate.

| Dimension | Weight | What earns a 4 |
| --- | ---: | --- |
| Grounded recall | 30% | Correct, relevant details from earlier sessions, with entities and time kept distinct |
| Updating beliefs | 25% | Applies corrections and superseded decisions; distinguishes current truth from history |
| Applied memory | 20% | Uses multiple remembered constraints to make a useful new recommendation |
| Conversational coherence | 15% | Answers the actual request naturally, follows local context and format, and avoids memory dumps |
| Calibrated boundaries | 10% | Admits missing facts, resists false premises, and respects a request not to repeat private details |

Common anchors: **0** unusable, fabricated or nonresponsive; **1** major errors
that require the user to repair the answer; **2** partly useful with material
omissions; **3** correct and usable with minor shortcomings; **4** fully grounded,
useful and appropriately concise. Verbosity, flattery and quoting many memories
earn no extra credit. Failure to retrieve should affect recall; polished prose
must not conceal it. A low product score is not a cheating verdict.

## Cost and failure semantics

The prompt describes limits; runtime code enforces them: exactly 30 calls to the
agent, bounded HTTP response bytes, per-operation and whole-run deadlines,
bounded judge requests/output, and a conservative prepaid judge budget. Unknown
usage, malformed results, refusal, truncation, timeout or partial coverage produce
`incomplete` with no score. There is no free retry and no zero-score fallback for
provider or infrastructure failure. Reserve the maximum per-run cost before a
claim; an expired claim remains spent until an operator reconciles it.

Astra pricing defaults are $10/M input and $50/M output, verified against the
[official model page](https://developers.openai.com/api/docs/models/gpt-6-astra)
on 2026-09-18. The meter reserves UTF-8 bytes plus framing as a conservative token
ceiling, including maximum reasoning/output tokens, before each request and
reconciles reported usage afterward. The harness must separately use an isolated,
trusted, metered relay; harness-reported token counts are never billing evidence.
The worker must not pass the judge key or Platform credential to the submission.

## Score and selection

Select the current finalized top five from Platform's canonical public board,
before conversation scoring, once per artifact, benchmark epoch and instrument.
Freeze the candidate's identity and base quality at claim time. Persist the
transcript, story commitment, model identity, rubric, probe grades, usage and
failure reason. A completed assessment remains attached when the candidate
subsequently leaves the top five; never rejudge merely to fish for a better score.

Proposed quality is `(2 * base_quality + conversation_score) / 3`. Where the
existing base is the equal memory/tool mean, this gives memory, tool and
conversation equal weight. Verified base penalties stay in base quality.
Efficiency remains a separate, existing versioned operation applied afterward;
judge tokens do not enter miner token efficiency. Missing conversation evidence
is pending, never silently a perfect score or a zero.

Ship the instrument and candidate composite as **shadow**, without changing
historical benchmark results or reward ordering. Promotion requires a new scoring
contract and coordinated validator/Platform/public-board activation. Top-five-only
evaluation creates a selection problem: scores outside the evaluated cohort are
not directly comparable with conversation-adjusted scores. Before enforcement,
evaluate every contender whose optimistic conversation score could enter the top
five, retaining a separate base-score admission queue. Do not sort evaluated and
unevaluated candidates together as though both had the same composite.

## Implementation and rollout plan

1. Shared versioned evidence model, seeded 30-turn instrument, black-box harness
   adapter, Astra conversation tool loop, and enforced budgets.
2. Durable Platform top-five reservations/results and a read-only Backroom tool
   exposing pending/completed/incomplete state, cost and proposed composite.
3. Idle enrolled screener workers claim one assessment globally, verify and
   normalize the screened image, start a fresh rootless container on an internal
   network, attach a separately capped trusted relay and persist the report
   before one result delivery. No Platform or judge credential enters the miner.
4. Preserve the live submission fee during shadow activation. The approved
   2026-09-18 rollout retains **100,000,000 RAO (0.1 TAO)**. Backroom's
   `fee_change_request` is a separate proposal, not an activation requirement
   or authorization to change the fee. Any future fee change requires its own
   explicit approval and audited submission-settings revision.
5. Calibrate on cleared leaders, ordinary starter-kit agents and intentionally
   memoryless/incoherent controls, with repeated blinded runs. Measure agreement
   with human judgments, ranking variance, false failures, cost and latency.
   Keep the hypothesis of benchmark overfitting unproven until those runs exist.
6. Only then version the reward contract, expand admission as described above,
   and activate. Deployment, paid calibration and live fee activation are separate
   operational steps from this source implementation.

## Running the shadow instrument

After migrating Platform, use `get_conversation_assessments` and
`set_conversation_settings` in Backroom. Apply `mode: shadow` with the current
`settings_revision`, an operator reason and confirmation
`APPLY CONVERSATION SHADOW SETTINGS`. The append-only settings revision is the
live authority. `off` stops new claims; an already admitted job may finish.
The default is off. There is no enforce mode.

Idle enrolled workers call the `/api/v1/screener/conversation-assessments` claim
and result endpoints using their rotating node principal. Claim admission is
serialized globally: at most one live assessment, $25 reserved for Astra and $5
for the harness, with a $150 rolling daily cap. Failed/expired identities are
never automatically reissued. Image download capabilities and story seeds stay
private. Reports bind artifact, screened archive, instrument and claim owner.

The launcher uses a rootless Docker daemon, verifies archive size and digest,
reuses the screener's config/layer normalization and checks the loaded image ID.
The fresh miner has a read-only root, bounded tmpfs, CPU/memory/PID caps and one
internal network. Only its trusted inference sidecar also joins an egress
network. The miner receives placeholder keys and a public TLS CA, never the
provider key, Docker socket, private certificate key or report state.
Host requests reach only `/health`, `/run`, and `/seed` through a bounded Docker
exec helper in the trusted sidecar. No container port is published. This works
with internal-only rootless networks and does not depend on host port forwarding.

Production Astra calls use OpenRouter's stateless Responses API with
`openai/gpt-6-astra`, OpenAI-only routing, no provider fallback and the same
$10/$50 price ceiling. The harness profile is separately versioned as
`conversation-openrouter-oss20b-pplx768-v1`: GPT-OSS-20B chat/Responses and
Perplexity 768-dimensional embeddings. It does not claim to be the benchmark's
exact serving-provider profile. Route overrides and paid hosted tools are
removed or refused; every dispatch reserves against the $5 cap before sending.
Provider failure poisons the relay. Missing dollar receipts for metered embedding
usage produce a labelled price-ceiling bound, not invented actual spend.
Chat accepts both plain text and arrays of text parts, including the system
message shape emitted by Rig's OpenAI client. Multimodal parts remain refused.
Larger positive client output allowances are capped at the existing 8,192-token
dispatch ceiling before reservation; they do not enlarge the instrument budget.
Private reports retain the first relay failure's fixed code, request/provider
stage and optional HTTP status. They never include exception text, request URLs,
headers or provider bodies. Subsequent client retries cannot overwrite that
diagnostic or dispatch more paid inference.
BYOK Router fees exclude the separate provider invoice. For those responses the
meter retains the provider tariff plus Router fee as a labelled upper bound;
pre-dispatch reservations include the possible 5% BYOK fee. A zero Router charge
therefore never makes BYOK inference appear free.

Deploy the released code first. The focused
`infra/ansible/playbooks/conversation-shadow.yml` playbook installs the worker
capability using the existing exact-subject X.509 federation and existing
`validator-openrouter-key` access. It preserves current fleet channel limits.
Its systemd override sets `SCREENER_CONVERSATION_OPENROUTER_KEY_FILE` and a private
per-worker `SCREENER_CONVERSATION_SPOOL_DIR`; absent settings leave the consumer
inert. Drain/restart workers after convergence. Enable claims through Backroom
only after runtime isolation and provider preflight checks pass.

Run the no-inference launcher smoke against the same rootless daemon before
enabling claims. It builds a disposable fixture, exercises 30 exchanges and TLS,
and verifies cleanup. The image download transport uses the local fixture archive;
it does not test object storage or grade a real submission.

```sh
cd workers/screener
DOCKER_HOST=unix:///run/ditto-screener-docker/docker.sock \
SCREENER_GATEWAY_STATE_ROOT=/var/lib/ditto-screener-gateway-state \
uv run python scripts/smoke_conversation_runtime.py
```

The standalone CLI remains useful for an explicitly provisioned local sandbox:

From `workers/screener`, with the judge key available only to the trusted process:

```sh
uv run ditto-conversation-assess \
  --claim /private/run/claim.json \
  --harness-url http://127.0.0.1:18080 \
  --output /private/run/report.json
```

Add `--platform-url https://<platform-origin>` and set
`DITTO_PLATFORM_ADMIN_TOKEN` in the trusted process to submit the saved report.
The host persists evidence before submission and prevents rerunning a claim file
after any billed attempt. A failed upload should resend the same report and lease
token, not rerun the assessment. The result endpoint is idempotent for byte-equivalent
canonical reports and rejects conflicting replacements.

`get_conversation_assessments` in Backroom lists state, reserved cost, observed
combined actual spend when both receipts are known, proposed composite and the
fee-change payload. Supply `assessment_id` for the private transcript, grades,
judge usage and harness usage. Upper bounds remain labelled in the private
report; a $30 reservation is not a claim of $30 actual spend.

## Local validation (2026-09-18)

- 15 focused examiner tests: complete HTTP conversation flow, fresh-session
  boundaries, ingestion acknowledgement, quote/coverage checks, stateless judge
  continuation, budget and malformed-input handling. The judge is mocked here.
- Five real-Postgres Platform tests: concurrent top-five admission and budget
  fencing, disabled/authentication behavior, immutable bound results, expired
  claims, shadow composite arithmetic and audited fee revision. Existing upload
  and admission tests also passed.
- Full screener suite: 1,205 passed. Backroom: 747 passed; TypeScript checking
  and production build passed. Root contracts, release routing and skills: 185
  passed. The new migration is a single successor of the fetched main head.
- Platform lint/copy checks passed; Linux-target mypy passed for 816 files.
  The broad macOS Platform run had 6,116 passes, six failures and 79 setup errors
  in unchanged coding-hosted paths (Linux-only Go APIs, socket restrictions,
  and macOS child-process environment behavior). It is not a clean full-suite
  result. Native macOS mypy also rejects the existing Linux `SO_PEERCRED` use.
- No live Astra assessment, real-submission calibration, deployment, reward
  activation or production fee mutation was performed. Runtime/schema tests do
  not establish the grader's quality, repeatability or measured provider cost.

### Production-path validation in progress

The enrolled-worker endpoints, serial admission, audited mode switch, rootless
launcher and fixed upstream relay now have local regression coverage. A bounded
live provider preflight passed Astra function calling, GPT-OSS-20B chat and
768-dimensional Perplexity embeddings with usage receipts. Full live assessment,
release adoption and corpus calibration remain separate proof steps; record their
actual outcomes before declaring shadow activation complete.

### Initial shadow containment (2026-09-18)

All five initially admitted top-five assessments ended `incomplete` with
`harness_inference_incomplete`, no completed exchanges and no score. Backroom
revision 2 paused new admissions at 18:28:05Z; no attempt was running. The live
fee remained 0.1 TAO and the rolling reservation remained $150. The old reports
did not retain the underlying relay exception. Source inspection and an offline
request reproduction identify a rejected Rig system-text array and a 16,000-token
client allowance rejected by the 8,192-token relay ceiling; they do not
establish that this explains every failed submission.

Terminal identities remain terminal after a relay release or mode change.
Do not change an artifact, benchmark epoch or instrument identity to bypass
that rule, or clear an old reservation to create budget headroom. A paid rerun
requires explicit audited manual retry authorization and admission within the
unchanged rolling cap. The current lane has no manual retry endpoint; ordinary
shadow reactivation alone cannot rerun these five identities. A successful
synthetic smoke is not a completed top-five assessment.
