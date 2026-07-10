# Central Scoring Scheduler: Implementation Scope (#35)

Status: implemented (role flags), pending only the thin-validator distribution decision
Context: docs/scoring-decentralization-brief.md (dittobench-api). Decision =
independent weight-setting over centrally-computed scores.

## Implemented: role flags, not a separate module

Rather than physically extract a new `ditto/scorer/` module, the split is done
with two role flags on the existing worker. Same deployment topology, lower risk,
no code duplication, backward compatible.

- `ditto/validator/config.py`: `VALIDATOR_ENABLE_SCORING` / `VALIDATOR_ENABLE_WEIGHTS`
  (both default true). Env gating relaxed: the scoring half requires dittobench /
  OpenRouter only when `enable_scoring`; the weight half requires Pylon identity
  only when `enable_weights`. At least one role must be on.
- `ditto/validator/worker.py`: `run_once` gates the queue-scoring loop on
  `enable_scoring` and the weight fold on `enable_weights`. Re-scoring stale
  champions moved out of the weight fold into the scoring sweep
  (`_rescore_stale_champions`), so the split keeps re-scoring: the weight-only
  validators just read the ledger the scorer refreshes.
- `ditto/validator/__main__.py`: a scoring-only instance builds no chain client.
- Infra (`infra` repo, `validator_worker` role): `validator_enable_scoring` /
  `validator_enable_weights` role vars render into `validator.env`. The central
  scorer host sets `validator_enable_weights=false`; independent validator hosts
  set `validator_enable_scoring=false`.
- Tests: `TestRoleSplit` (worker) + `TestRoleConfig` (config); full validator
  suite green (135 tests).

Deploy topology: one host runs the scorer (`enable_weights=false`); any number of
hosts run weights-only (`enable_scoring=false`). The combined default remains
valid for the current single-process setup, so the cutover is staged, not a
flag day.

The one remaining decision is code distribution for external validators (publish
ditto-subnet vs extract a minimal public validator) — see the brief. It does not
change the code above; datagen stays sealed in the private dittobench-api either
way.

---

## Original scope (for reference)

## Goal

Split the validator's single hourly loop ("queue -> dittobench -> weights") into:

1. A **central scorer** (one instance, ours) that triggers scoring per epoch and
   persists canonical signed scores.
2. A **thin validator** (anyone runs it) that reads the canonical ledger,
   computes weights, and sets them on chain.

## What already supports this (no change needed)

The seam is half-built already:

- **Weight-setting already reads the canonical ledger, not local state.**
  `validator/worker.py:_update_weights` (181-223) calls `platform.get_ledger`
  (`platform.py:60-77`) = `GET /api/v1/scoring/scores`. It deliberately does not
  use the scores it just wrote (fixes the one-epoch-weight bug). The thin
  validator's read path exists today.
- **Score persistence is caller-agnostic and signed.**
  `POST /api/v1/validator/agent/{id}/score` (ditto-platform
  `endpoints/validator.py:289-416`) -> `upsert_score` (`db/queries/scores.py:212-274`)
  -> `Score` model (`db/models.py:277-372`, PK `(agent_id, validator_hotkey)`).
  A scheduler reuses this endpoint verbatim.
- **`compute_weights` is a pure deterministic fold over the ledger**
  (`validator/weights.py:92-148`). Keep it validator-side and identical across
  validators so they converge under Yuma.

## The extraction (the scoring trigger)

Move out of `ditto-subnet/ditto/validator/worker.py` into a new scorer module:

- the queue loop in `run_once` (152-165)
- `_score_agent` (472-475) + shared `_evaluate_and_submit` (477-535)
- the CRN re-score path `_rescore_stale_champion_and_tail` (225-287) + `crn.py`
- the `DittobenchClient` (`dittobench.py`, whole file)
- `platform.py` client calls `get_queue` (46-58), `get_artifact` (79-90),
  `submit_score` (92-112)
- `signing.sign_score` (508-515) moves with whoever now submits scores

After extraction, `run_once`/`run_forever` (140-179, 537-571) collapse to just
the `_update_weights` path on `epoch_seconds`; `sweep_seconds` and the whole
queue loop leave the validator.

## New component: the scorer worker

There is no platform-side background loop today (the `ditto-platform/ditto/validator/`
package is empty; the API server is purely request-driven). The right structural
precedent is the existing **screener** process, which already runs a poll loop in
ditto-subnet and drives the platform over HTTP.

- New ditto-subnet module `ditto/scorer/worker.py` with `run_forever`/`_sweep`,
  entrypoint `python -m ditto.scorer`. Copy the shape of `ditto/screener/worker.py`
  (49-79) and the validator `__main__.py` wiring (42-113).
- Loop each epoch: pull `evaluating` agents (`GET /validator/queue`) -> for each,
  `get_artifact` -> score via dittobench-api -> `submit_score` (signed). Reuses
  `dittobench.py` + `platform.py` as-is.
- Runs as ONE instance on our infra, not per-validator.
- Deploy: new Ansible role `scorer_worker` mirroring `validator_worker` (systemd
  `python -m ditto.scorer`, EnvironmentFile, epoch cadence). The
  `validator_worker` role is the template.

## The thin validator

`ditto/validator/worker.py` slims to: `get_ledger` -> `compute_weights` ->
`put_weights` on the `epoch_seconds` cadence. It drops `get_queue`,
`get_artifact`, `submit_score`, and the `DittobenchClient` import. It keeps the
permit/stake/commit-reveal self-checks (289-423) and the chain sink. Anyone can
run it because it holds no scoring secrets.

## Identity / auth (decision needed)

The score-write endpoint verifies an sr25519 signature and an on-chain validator
permit (`endpoints/validator.py:318-328`). The central scorer needs an accepted
identity:

- **Simplest:** run the scorer under our already-registered validator hotkey. It
  signs scores exactly as the validator does today, just without setting weights.
  Thin validators run under their own hotkeys and only set weights (they submit no
  scores, so they need only weight-setting permits).
- **Reconciliation with #37:** scores are already sr25519-signed by the scorer
  hotkey, giving provenance from a known on-chain identity. That may make the
  separate KMS Ed25519 scheme (#37) redundant for per-score provenance. Revisit
  #37: keep sr25519 hotkey-signing for individual scores, and layer KMS only if we
  serve a separate aggregated canonical-payload endpoint.

## Consensus note

With a single central scorer, all validators read one canonical ledger and
compute identical weights, so convergence is trivial. That is exactly
"independent weight-setting over centrally-computed scores." The multi-validator
k=3 / median-of-3 plan hinted at in `scores.py:359-360` and `validator.py:14-22`
is the alternative for multiple independent scorers; it is not needed for this
model.

## Phasing

1. **Extract the scorer.** New `ditto/scorer/` module + `scorer_worker` Ansible
   role. Run it as the sole scorer under our hotkey. Validator still sets weights,
   so nothing breaks if the split is staged.
2. **Slim the validator.** Reduce `validator/worker.py` to read-ledger -> weights;
   remove its scoring path. Now anyone can run a thin validator.
3. **Signed canonical scores + liveness** (ties to #37 and #40): finalize score
   provenance and add staleness handling on the ledger read.

## Open decisions

- Scorer identity: our validator hotkey vs a dedicated scorer identity plus an
  endpoint auth path.
- #37 reconciliation: existing sr25519 vs KMS Ed25519 for score provenance.
- Cadence: scorer epoch vs the validator's current 120s sweep / 3600s weight
  cadence.
