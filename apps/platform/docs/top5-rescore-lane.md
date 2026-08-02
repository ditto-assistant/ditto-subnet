# Top-five continual shared-seed re-benchmark lane

**Status:** implementation-ready RFC.
**Canonical PRs:** ditto-platform #280, ditto-subnet #202.
**Supersedes:** ditto-platform #195 and ditto-subnet #161.

## Goal

Continually re-benchmark the current KOTH emission set—the champion plus four
participation-tail agents—on champion-anchored shared seeds. The extra evidence
reduces dataset-luck in crown and tail ordering without changing the authoritative
three-validator score quorum.

Membership follows the live emission set. A new entrant automatically joins and
catches up at a bounded rate; an agent that leaves the set stops consuming this
lane. Seed families are scoped to the active benchmark contract, so a benchmark
version change starts a fresh comparable history without any deployed-version
hard-code.

## Safety invariants

1. **The canonical score is immutable here.** Top-five results never use the
   ordinary `/validator/agent/{id}/score` endpoint and never replace a k=3 score.
2. **Every benchmark is platform-leased.** The validator must obtain a dedicated
   top-five ticket before running. The platform owns membership, freshness,
   validator authorization, replay protection, and tempo.
3. **Evidence is append-only.** One immutable row is stored per validator,
   benchmark version, and seed. Duplicate submissions are idempotent.
4. **All signed pairs are bound.** One member claim may evaluate multiple missing
   seeds; the dedicated receipt signature binds the complete ordered
   `(seed, composite)` list.
5. **Failure is isolated.** A declined claim, expired lease, or failed member run
   does not block normal scoring or chain weights.
6. **No extra quorum voter.** The lane adds measurement evidence only; it does not
   consume or expand the three-score finalization budget.

## Contract

### Claim

`POST /api/v1/validator/top5-confirmation-job`

The signed request identifies both the projected champion and the requested
emission-set member. The platform recomputes current membership and rejects stale
or ineligible claims. A successful response is a normal artifact-bound ticket with
an exact deadline and current benchmark version.

### Append

`POST /api/v1/validator/agent/{agent_id}/top5-confirmation-score`

The request carries a representative `ScoreReport` plus aligned
`confirmation_seeds` and `confirmation_composites`. Its separate signature domain,
`validator-top5-confirmation-score:v1`, binds every pair. The endpoint validates
the live ticket, current emission membership, benchmark version, and the exact
champion-derived seed family before appending rows and spending the ticket.

This is deliberately separate from ordinary score submission. Reusing `/score`
would either collide with current re-test guards or overwrite the canonical score,
both of which violate the design.

## Seed and history model

Seeds are a deterministic function of `(champion_agent_id, benchmark_version,
replicate_index)`. Champion anchoring keeps the baseline stable when tail members
change. When the champion changes, the anchor changes and a new family begins.

The validator ledger exposes append-only confirmation history. The fold groups
records by seed and takes the median across validators for that seed, then uses
shared seeds for paired dethrone comparisons. Legacy in-row confirmation arrays
remain a read-only fallback during transition.

The champion establishes a three-seed baseline, then extends one seed per round up
to a fixed cap. A behind tail entrant scores up to two missing seeds per claimed
round until it catches up; it never exceeds the champion's depth.

### Catch-up

Issuance distinguishes two kinds of pending work for a member.

*Growth* is the next seed the wave has not finished, paced one per round. The wave
is a synchronisation point: nobody may run ahead of the seed the rest of the
emission set is still working, so this pacing is what keeps the crown moving at a
fixed cadence.

*Catch-up* is the member's backlog — seeds every **other** emission member already
holds and this one does not. That is settled evidence, so there is no wave left to
tear and no ordering to preserve, and the whole backlog is offered at once. The
platform hands one backlog seed per validator per claim (the ticket is pinned to a
single seed), so K idle validators converge K seeds in the time one used to take
one. Convergence after a membership change is therefore ~1 round rather than one
round per anchored seed.

This matters because a completed wave is not stored state: it is recomputed at read
time by intersecting the *current* emission set's seed sets. A member promoted into
the top five arrives at depth zero and gates that intersection for everyone.
ditto-platform#489 stopped the promotion from discarding accumulated evidence on the
read side (`wave_membership = participants`); catch-up is the write side, shortening
the window in which anything is degraded at all.

Two bounds hold it in place. A behind member is privileged over *extended-cohort*
top-up work only — never over another emission member — so a member that cannot be
leased can never stall the lane. And "behind" is a fact about append-only stored
evidence, not a promotion event: the privilege exists exactly while a backlog does,
and an agent oscillating in and out of the top five keeps what it earned, so it
cannot re-acquire a backlog by re-entering.

Catch-up is scoped to emission-set members. The extended retest cohort is
spare-capacity work whose depth gates nothing, and it keeps one-seed-per-round
pacing.

## UI

The public leaderboard shows confirmation depth for top-five agents. This is
evidence depth, not a new canonical score count, and must not be presented as a
fourth quorum score.

## Rollout

1. Merge and deploy ditto-platform #280.
2. Merge and release ditto-subnet #202.
3. Verify successful dedicated claims and appends, unchanged ordinary k=3 scores,
   healthy normal queue throughput, and healthy weight cycles.

The platform goes first because a new subnet against an old platform must only see
a rejected optional claim; normal scoring and weights continue. No data backfill is
required. Historical confirmation arrays remain readable, while new evidence is
written only to the append-only ledger.
