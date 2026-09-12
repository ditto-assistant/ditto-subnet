# Ditto full-memory retest protocol

This is an offline research protocol for the private Ditto backend harness, not
a miner submission, production confirmation score, payout input, or a new Mem0
leaderboard comparison. The public repository carries the audit code and
methodology; raw answers, internal tool transcripts, fixture dumps, credentials,
and provider generation IDs stay in the private evidence workspace. No result
is implied by this protocol. Publish a completed audit before quoting a score.

The backend preparation and baseline implementation is tracked in
[backend PR #2679](https://github.com/ditto-assistant/backend/pull/2679).
PR publication does not mean the retest is complete, merged, or deployed.

## Why retest

The historical isolated-user measurement reported 448/500 (89.6%) with Gemini
3.1 Pro, without running the subject-generation/dreaming stages. Its restored
fixture was also incomplete and had incorrect per-occurrence timestamps (see
the fixture audit below). The score describes that flawed fixture, not faithful
complete LongMemEval-S inputs. The condition did not exercise the full
subject-backed memory system. Gemini is an
experimental reader choice, not a LongMemEval requirement. The new reader
condition is `openai/gpt-5.6-luna` with `medium` reasoning, verified against the
answer-provider response identities, not merely the requested model name.

Two graph concepts must not be conflated:

- Subject summaries and subject-to-memory links support subject search and
  subject-scoped memory reading after dreaming.
- Subject-to-subject edges connect related subjects. Their existence does not
  prove they contributed retrieval candidates or were available to the agent.
  The opt-in experiment adds that retrieval/tool capability separately.

## Conditions and claim boundaries

| Condition | Reader | Preparation | Subject-edge retrieval |
| --- | --- | --- | --- |
| Historical isolated reference | Gemini 3.1 Pro | Incomplete pre-embedded fixture with date errors; no subject dreaming | Off |
| Full-memory Luna baseline | GPT-5.6 Luna, medium | Complete seed plus production dreaming barrier | Off |
| Opt-in graph variant | Same Luna settings | Same completed fixture snapshot | On, bounded opt-in |

Keep the baseline running against its frozen source revision while implementing
the graph variant in a different checkout. The variant must not change the
baseline database or run in-memory code. Freeze each source revision, prompt,
tool catalog, learned-weight binary, dataset, fixture, knobs, and judge before
that condition starts. Record graph bounds and candidate counts in the private
backend evidence. A baseline/variant paired comparison can isolate the intended
graph change only if all other relevant conditions match. A historical Gemini
comparison changes the reader, preparation, source revision, and potentially
other adapter behavior; it is descriptive, not a causal graph-improvement test.

### Restored fixture audit discovered additional input defects

The 2026-09-12 read-only full-history audit supersedes any earlier claim that
the saved fixture was a complete, faithful LongMemEval-S seed. After restoring
71 missing whole-session occurrences, the inspected intermediate fixture had
122,495 pairs representing 244,867 of 246,738 original non-empty turns. It was
still missing 1,871 assistant-first turns (turn index zero) across 1,871 session
occurrences and 484 questions. Five of those occurrences are answer-bearing
sessions; an answer-session flag is diagnostic only, never permission to omit
other histories.

The strict exact-date audit found 23,363 pairs with the wrong per-occurrence
session date, across 4,653 session occurrences and 482 questions, among 23,867 total session
occurrences. Eight affected answer-session occurrences span five questions.
The defect reused a date associated with a shared session ID rather than each
question's specific session occurrence.
This supersedes the earlier 23,350-pair/4,651-occurrence count, which permitted
13 small positive timestamp offsets instead of requiring exact dataset dates.

The new fixture must restore those assistant-first turns and each occurrence's
actual dataset date, then run dreaming afresh. Summaries already generated from
the incomplete/wrong-date fixture cannot serve as the corrected baseline. Keep
the historical and partially dreamed fixtures intact for audit; repair a new
isolated database. Commit the repair/audit procedures and final count/hash
evidence. These additional changes further prevent attributing any historical
score difference to Luna, dreaming, or graph retrieval alone.

## Dataset, isolation, and the preparation barrier

Use all 500 cleaned LongMemEval-S questions, dataset revision
`98d7416c24c778c2fee6e6f3006e7a073259d48f`, SHA-256
`d6f21ea9d60a0d56f34a05b609c79c88a451d2ae03597821ea3d5a9678c3a442`.
Each question owns a different fixture user/knowledge graph. Shared source
sessions do not authorize cross-question memory. Preserve every timestamped,
non-empty user and assistant turn, including assistant-first, user-only, and
same-role-adjacent histories. The public adapter's `entry_to_pairs` documents
and tests lossless pair conversion. Do not silently discard missing distractor
sessions simply because the answer-bearing sessions are present.

Only history content, roles, IDs, and dates reach seeding/dreaming. Never pass
question text, answers, answer-session IDs, question type, or `has_answer`
labels into memory preparation. Pin answer-time prompt clock to the question's
dataset date. Do not seed current wall-clock dates into historical memories.

Run the backend's production dreaming completion path against a dedicated
loopback-only database. Record a per-user durable preparation audit: memory
count, generation/storage backlog, subject count, subject-memory links, edge
count, graph build state, timestamps, source SHA, extraction model and any
fallbacks. Require complete generation/storage and valid subject/link/build
state before answering. Inspect refinement/orphan recovery errors too: a graph
build marker alone is not evidence every production stage succeeded. An edge
count of zero can be legitimate for a small disconnected graph; report it,
rather than inventing edges or dropping that question.

## Known learned-retriever overlap

The backend repository's retrieval training dataset contains 474 unique
LongMemEval question IDs; all 474 overlap the exact cleaned 500-question set.
The checked training script's `val_frac=0.1`, `seed=1234` split yields 427
training and 47 validation rows. The inspected embedded `model.bin` SHA-256 is
`46d34091333706c841b6e0f6b35b2bf9f5f89ac0fca692720ce9c22cc795138c`.
Its metadata references `model.pt`; dataset overlap alone does not establish a
complete, independently verified training-to-export lineage for that binary.

This is substantial training/evaluation overlap and precludes an unqualified
held-out, unseen-test, or apples-to-apples Mem0 superiority claim. Preserve and
report the production weights for the requested best-system measurement. A
future uncontaminated assessment needs independently held-out histories or a
separately labeled retriever trained without these evaluation questions. Do
not tune on failures, change weights, and then label a rerun the same condition.

## Answering, judging, and interruption policy

Record requested and observed answer model/provider for every successful
provider turn, reasoning effort, prompt clock/time, graph preparation flag,
graph retrieval flag, tool names, fixture user, hypothesis, and explicit judge
status. A provider/query/judge failure is not a wrong answer and must not be
silently counted as a valid negative judgment. Preserve empty final answers in
raw attempt evidence; this strict condition treats them as incomplete and
does not certify a headline aggregate containing them. Do not choose a more
favorable answer from private reasoning.
Checkpoint/resume must reject duplicate IDs and condition changes. Retry
operational failures only, retaining attempt evidence; do not resample judged
incorrect or empty native answers to improve a score.

Pin and name the built-in judge separately from the reader. Its boolean QA
accuracy is the headline for that condition, not the backend's 60/40
QA/session-recall composite. Session recall is a separate diagnostic. The
official post-hoc comparison is an additional judgment of the identical frozen
hypotheses using LongMemEval source revision
`9e0b455f4ef0e2ab8f2e582289761153549043fc`, unmodified evaluator/prompt and
`gpt-4o-2024-08-06` through the existing narrow judge proxy. Do not relabel a
built-in judge score as official. No evaluator inference is launched by the
audit script.

## Reproduce the offline audit

From `services/dittobench-api/integrations/longmemeval`:

```bash
python3 audit_backend_run.py \
  --dataset /private/longmemeval_s_cleaned.json \
  --run /private/luna-full-memory.json \
  --condition ditto-lme-s-full-memory-luna-medium-v1 \
  --judge-model '<exact-built-in-judge-model>' \
  --output /private/luna-full-memory.audit.json \
  --export-hypotheses /private/luna-full-memory.hypotheses.jsonl
```

The CLI requires a final JSON report with a full source commit and actual
SHA-256 prompt/tool/learned-weight digests. Its loading utility understands
JSONL checkpoints for diagnostics, but a checkpoint alone cannot attest source
provenance and is rejected for a publishable audit. The auditor
rejects incomplete/duplicate/unexpected IDs, unknown/failed judgments, changed
reader/provider/reasoning/clock/graph flags, repeated provider IDs, shared
fixture users (including swapped question fixtures), reasoning-derived or
empty final answers, and inconsistent reported QA accuracy. All 500 IDs must pass;
there is no `--allow-partial` or legacy-evidence relaxation. Historical reports
without this provenance remain historical, not retrospectively certified.

For graph-on evidence add `--graph-retrieval` and use a distinct condition.
To compare opposite graph conditions under otherwise identical checked reader,
judge, reasoning, and date settings, add `--paired-run /private/other.json`.
The requested run is the left side and paired run the right side. Source,
fixture and graph-parameter equivalence still need manual manifest review;
the script does not infer them from matching accuracy or flags.

Only the aggregate audit is safe to publish after review: it includes hashes,
per-type QA counts, empty-answer count, Wilson intervals, aggregate tool usage,
provider turn counts, and optional paired disagreements/exact McNemar test.
Wilson intervals describe finite-case Bernoulli uncertainty, not repeated-run
model variance; overlapping histories further limit independence. A single run
cannot establish stochastic robustness or guarantee a graph gain.

```bash
python3 -m unittest -v
```
