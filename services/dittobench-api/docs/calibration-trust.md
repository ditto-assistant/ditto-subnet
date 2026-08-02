# Calibration trust assumption

DittoBench's difficulty and variance calibration (`cmd/benchcal`, `cmd/calibrate`)
runs against `internal/refharness` — a keyword-overlap routing table with **no
model** — plus, for the memory suite, a **synthetic** per-question-type
competence table (`refMemoryCompetence` in `cmd/benchcal/main.go`).

This is deliberate and useful for one thing only: **isolating dataset
difficulty**. Because the reference harness is deterministic, the only thing that
changes between seeds is the dataset, so the spread of `tool_mean` across seeds
is a clean measure of how much run-to-run difficulty the generator injects. The
repeat-seed noise floor is ~0 by construction, which confirms the measurement
sees dataset difficulty and nothing else.

## Why the gate cannot be trusted as-is

A deterministic parser and a real locked-model reasoning harness can share the
same mean score while having **completely different variance**. The reference
harness has zero run-to-run variance; a real reasoning model does not. So:

- A sigma / variance gate tuned against the reference harness measures **only**
  dataset-difficulty spread, not the champion-region variance a real leaderboard
  score carries.
- Tuning the KOTH `margin` / `score_tol` (the dethrone band) against that number
  makes the band too tight, which **certifies a deterministic parser as a stable
  champion** — exactly the anti-gaming failure mode we are trying to close.

Put bluntly: the calibrator can tell you the *test* is stable. It cannot tell you
a *harness* is stable, because the harness it runs is a parser with no variance.

## How to recalibrate honestly

1. Stand up a **real locked-model harness** (the frozen model from
   `internal/llm`, `llm.HarnessModel()`), not the reference parser.
2. Run `cmd/calibrate --run-size full` against it over **many fresh seeds** and
   record the **measured** champion-region composite σ (mean and spread).
3. Set the gates from that measured number:
   - `cmd/calibrate --max-stddev <measured tool_mean σ>`
   - `cmd/benchcal --target-sigma <measured composite σ>` (and, if you want a
     hard gate, `--max-composite-stddev`; note this only bounds the hermetic,
     tool-routing-dominated σ — it is necessary, not sufficient).
4. Feed a **recorded** per-question-type competence profile into benchcal via
   `--mem-profile <path>` so the hermetic memory_mean reflects the real model's
   per-type competence instead of the synthetic placeholder. The file is JSON:

   ```json
   {
     "single-session-recall": 0.82,
     "multi-session": 0.51,
     "temporal-reasoning": 0.40,
     "knowledge-update": 0.63,
     "preference": 0.80,
     "preference-application": 0.55,
     "contradiction": 0.42,
     "abstention": 0.60
   }
   ```

   Values are per-question-type mean score in `[0,1]`. The report records its
   provenance in `mem_profile_source` so a committed artifact says whether it used
   the synthetic placeholder or a recorded profile.

## What stays trustless

None of this touches the **score path**. Scoring remains a pure, deterministic,
reproducible function of `(dataset, transcript, scope)` — no model, no
validator-held secret, no nondeterminism. Calibration only sets thresholds and
targets; it never enters a miner's score. The recorded competence profile and the
sigma targets are configuration/provenance, not secrets: anyone can re-run the
calibrator with the same inputs and reproduce the numbers.
