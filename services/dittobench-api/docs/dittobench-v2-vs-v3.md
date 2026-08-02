# DittoBench v2 vs v3

`bench_version` 3 is the anti-gaming release. An audit of rejected submissions
found the dominant attack was not a better agent but a better parser: the
generator is public and deterministic, and a memory answer was a verbatim
substring of the cleartext haystack, so a harness holding the generator source
could recover answers without reasoning. A later review found compliance
laundering: letting the model misbehave, then editing the graded response to
hide it.

v3 addresses both without breaking the two invariants that define DittoBench:
**trustless scoring** (a pure, reproducible function of `(dataset, transcript)`,
no validator secrets, no LLM in the score path) and a **simple validator**
(generate, run, grade, sign). A validator-side judge and a secret holdout were
considered and rejected for breaking them.

Both versions are immutable contracts and both are served by one binary: v2 is
frozen and still regenerates byte-identically, so any already-scored run stays
auditable.

## At a glance

| Dimension | v2 | v3 |
| --- | --- | --- |
| Scoring scope | One path | Practice (lenient) and scored (on-chain, strict) are separate |
| Unobserved observable tool case | Capped at 0.5 on self-report | 0 on the scored path; observed execution is mandatory |
| Unreachable tool endpoint | Completes as an all-zero report | Preflight: the run fails and retries, never scored as zeros |
| Value recall | Same-attribute distractor scan | Dump guard zeroes a profile dump; unheld same-attribute values are planted so a candidate sweep also zeroes |
| Result-usage needle | Served by any content tool | Only the needle-bearing tool serves it; others serve a decoy that zeroes the usage half |
| Recurrence counting | Literal labels | Coreference, so a label-only counter undercounts |
| Opinion reversal | Hard-cessation lexicon | Conveyed by sentiment, defeating a "no longer"/"stopped" grep |
| Distractors | Static pools | Adds a "considered but rejected" value that zeroes a similarity-grabber |
| Canary | Single nonce; honest miss penalized | Multi-decoy attribution; honest-miss penalty removed, leak disqualifier stays |
| Injection framings | 13 fixed templates, trailing | Composed per seed from independent parts; non-trailing; payload token shape shared with real answers, so a token scrubber deletes its own answers |
| Injection evidence | Response text only, scrubable | An observed call to the bait tool scores 0, recorded by the validator |
| Cross-user lifecycle | Read path only | A write/delete under one user must not affect another's value |
| Rephrasing robustness | Generator-chosen twins, fixed in the dataset | Post-commit transform audit: a seed-selected share of cases is re-asked in wording fixed after commit |
| Response-field mutation | Legal by omission | Content-keyed mutation of `final_text`, `answer`, `abstain`, `tool_calls` is a screening reject |
| Offline verification | Graded inputs not retained | Every scored run publishes a content-addressed transcript, its digest bound inside the score signature |
| Champion comparison | Unpaired | A challenger inside the indifference band is re-scored with the champion on shared seeds |
| Regression protection | Manual review | CI runs non-reasoning baselines against fresh suites; any score fails the build |

## What v3 does not claim

Task-side hardening cannot make a memory answer model-required: the answer is
always a function of the cleartext haystack, so a harness holding the public
generator can compute the count, reversal, and timeline families without a
model. v3 raises the cost of building that parser and locks difficulty against
regression; it does not close it.

That includes the transform audit. It defeats answers keyed to a question's
exact surface form and prices memorization, but a solver that genuinely
recomputes from the haystack recomputes correctly under the transform too —
measured, not assumed. A low `transform_robustness` is a brittleness signal,
not proof of cheating, which is why it currently only reports and does not
affect status.

What v3 does make unforgeable is anchored to values a harness must retrieve or
execute to obtain: mandatory observed tool execution, the per-seed attribution
nonce, and the observed injection bait. Forcing model use remains the screener
oracle's job, plus the per-submission fee.

## What did not change

- The seed still derives from an on-chain block hash drawn after commit.
- Grading is still deterministic and judge-free; the composite is still
  `0.5*tool_mean + 0.5*memory_mean` times the bounded integrity factors.
- Every harness still runs against the one frozen open-weight model.
- Scoring is still k=3 validators, median finalized, each score sr25519-signed.
- The wire protocol is backward compatible: v3 adds optional fields, and a v2
  harness still parses every request.

## Scored vs practice

Practice runs (no pinned dataset) behave like v2: self-reports capped at 0.5 on
observable cases, preflight failures only log. Scored runs require observed
execution and begin with the preflight. Strictness lands only where emissions do.

## The preflight

A scored run's first turn is a probe: `POST /run` with a `case_id` starting
`preflight:` and `tool_endpoint` set. The harness must POST one `ToolExecRequest`
(any `search_web` call) before responding, or the run fails and reschedules.

From validator state alone, a harness that never calls tools and an endpoint
unreachable from the harness container look identical. v2 priced that
infrastructure fault as a zero score; the probe tells them apart. It can only
prevent an unfair zero, never raise a score.

## Verifying a score

1. Fetch the score record (`/public/agent/{id}/scores`).
2. Recompute `seed = derive_seed(block_hash, agent_id)` from the pinned block.
3. `generate -bench-version <v> -seed <seed> -run-size <size> -sha` must print
   the pinned `dataset_sha256`.
4. Reconstruct the canonical score payload
   `{validator_hotkey}:{agent_id}:{lease}:{run_id}:{composite!r}:{seed}`, where
   `lease` is `ticket_deadline` normalized to UTC with six fractional-second
   digits (or empty on a legacy row). Append `:{bench_version}` and then
   `:{transcript_sha256}` when the row declares them. Verify the row's sr25519
   `signature` against the SS58 public key in `validator_hotkey`; reject the row
   unless that verification succeeds. Then fetch the transcript and check that
   its SHA-256 equals the signed `transcript_sha256`.
5. Re-run the public grader over (dataset, transcript) and compare.

The transcript portion of step 4 was impossible under v2, which did not retain
graded inputs.

## For miners

- Port `Baseline::preflight` so the probe is answered without your model.
- Route every non-memory tool call through `tool_endpoint`; self-report alone
  earns 0 on the scored path.
- Do not post-process graded fields. Any transformation keyed to what a value
  *is* (scrubbing token shapes, clearing slots on detected injections, omitting
  an executed call) is a screening reject — and because payloads share a token
  shape with real answers, such filters delete your own correct answers.
- Answer the question in front of you rather than matching its wording. A share
  of cases is re-asked in phrasing fixed after you commit.
- Expect the `injection` flag when your model names a payload while refusing it.
  It is audit context; the score is unaffected.
