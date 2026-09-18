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
3. Standalone specialized worker command accepts a claim and isolated harness
   endpoint, emits replayable evidence, and submits it through the trusted control
   plane. Provisioning continues to use the existing sandbox and metered relay.
4. Set the fallback submission fee to **200,000,000 RAO (0.2 TAO)**. Migrated
   databases use append-only submission settings, including fresh installations.
   Backroom returns the current fee and an exact `fee_change_request` with the
   current revision, unchanged cooldown and 0.2 TAO amount. Apply that request
   through the existing audited submission-settings control when activating;
   already-issued payment reservations retain their quoted amounts.
5. Calibrate on cleared leaders, ordinary starter-kit agents and intentionally
   memoryless/incoherent controls, with repeated blinded runs. Measure agreement
   with human judgments, ranking variance, false failures, cost and latency.
   Keep the hypothesis of benchmark overfitting unproven until those runs exist.
6. Only then version the reward contract, expand admission as described above,
   and activate. Deployment, paid calibration and live fee activation are separate
   operational steps from this source implementation.

## Running the shadow instrument

After migrating Platform, `DITTO_CONVERSATION_SHADOW_ENABLED=true` enables
admission. It is off by default. A trusted controller calls
`POST /api/v1/admin/conversation-assessments/claim` to reserve the next current
top-five candidate (or receives null). Admission reserves $25 for Astra and $5
for the harness, with a $150 rolling daily cap. The database never automatically
reissues failed or expired claims. The claim contains private story entropy and
a lease token: store it with mode 0600.

The existing isolated sandbox launcher must verify the claim's screened-image
archive digest, start a fresh submitted process, and attach an inference relay
with a **separately enforced $5 cap**. This change does not provision that sandbox
or relay. A bare arbitrary HTTP endpoint is not sufficient deployment evidence;
the CLI deliberately accepts only explicit loopback origins. The examiner cannot
verify the identity of an already running endpoint from its health response.

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
**judge-only** spend, proposed composite and the fee-change payload. Supply
`assessment_id` for the private transcript and grades. Harness relay costs must be
reconciled separately; a $30 reservation is not a claim of $30 actual spend.

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
