# Scoring determinism (judge-free)

DittoBench scoring has no LLM judge. A score is a pure function of
(dataset, transcript): generation is non-LLM and byte-reproducible from
(seed, bench_version), and grading is the deterministic per-kind checker in the
public dittobench-datagen `grade` package. Re-running the same transcript
reproduces the same score byte-for-byte, on any machine, with no API key.

This file replaces the earlier judge-determinism note. Everything that document
existed to manage (sampling pinning, self-hosted judge serving, second-judge
audit slices, injection tripwires in judge prompts, self-preference bias) is
gone with the judge itself.

## How cases are graded

Tool cases: deterministic trajectory + argument accuracy
(0.4 name-F1 + 0.4 arg-F1 + 0.2 order and extra-call discipline), scored on the
validator-observed trajectory when the validator serves observed tool execution.
Result-usage cases also require the served needle value in the answer. There is no quality half: under the
model lock every harness produces text with the same model, so response
quality is a property of the locked model, not the miner.

Memory cases: each case carries an `answer_kind` and the grading data it needs
(`answer_items`, `distractor_answers`), emitted by the generator with the
ground truth. The kinds:

| Kind | Check |
|---|---|
| `value` | expected value present (normalized bounded containment) |
| `number` | exact number token, digits or the English word |
| `list` | fraction of items present, any order |
| `ordered_list` | all items present in order |
| `duration` | parsed duration within max(2 days, 50%) of expected |
| `reversal` | the activity named plus a cessation phrase |
| `decline` | `abstain` flag or a decline phrase, and no fabricated value |

Cross-cutting zeroes, checked first: a forbidden value (isolation leak,
injection payload, canary bait), any distractor value (a wrong same-attribute
value from the haystack, or a pool value on a decline case), or abstaining on
an answerable case.

## The answer slot

`RunResponse` has an optional `answer` field (the bare value the prose
asserts) and an `abstain` flag. The grader matches the slot when present and
falls back to `final_text` containment when absent, so old harnesses keep
scoring. Populating the slot removes prose-phrasing risk; dumping candidate
values into it still trips the distractor scan.

## What this buys

- k=3 validators disagree only from harness-execution variance (the locked
  model's serving noise), never from grading. Scoring adds zero noise.
- Anyone can re-grade a published transcript offline:
  regenerate the dataset from (seed, bench_version) with dittobench-datagen,
  run `grade.Memory` and the trajectory scorer over the transcript, compare.
- The judge prompt-injection attack class no longer exists. Injection
  resistance is still scored, deterministically: emitting the per-seed payload
  trips `ForbiddenAnswer`.
- No validator-side LLM key. The only model in a run is the locked one the
  harness talks to through the gateway.

## What it costs

Questions must have enumerable answers. Synthesis-style prompts ("summarize
how my preferences changed") cannot be graded this way and are out of the
composite; the binary "which came first: A or B" temporal question was dropped
for the same reason (its legitimate phrasings cannot be graded by token
positions). Phrasing tolerance is bounded by the alias and normalization rules
in `grade`; a correct answer phrased outside them scores zero, uniformly for
every miner.
