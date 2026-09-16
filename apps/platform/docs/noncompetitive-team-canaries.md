# Noncompetitive team canaries

A team canary is an ordinary SN118 upload that the team runs through normal
screening, copy detection and validator scoring. It exists to prove a path end
to end, for example the core-qualification observation that the hosted coding
certification requires. It must never compete for weights or emissions.

The `noncompetitive_agent_exclusions` table starts empty. An exclusion is an
audited, append-only record. It can only **remove** an exact identity from
competition: it never changes agent status, never skips screening or copy
detection, and cannot be used to make a banned, rejected, held or unscoreable
agent count. There is no endpoint that lifts an exclusion; a database trigger
refuses deletes and any change after binding.

## Identity and matching

An exclusion is created in two audited steps:

1. **Reserve before upload**
   - Call `POST /api/v1/admin/noncompetitive-canaries` with the exact testing
     `miner_hotkey`, the tarball `artifact_sha256`, a reason, and
     `RESERVE TEAM CANARY {miner_hotkey} {artifact_sha256}`.
   - The request is refused if an agent with that hotkey and artifact already
     has a score. Write-once kingship, frozen efficiency epochs and frozen
     rollout cohorts may already include it, so that case needs a separate
     review.
2. **Bind after screening**
   - Call `POST /api/v1/admin/noncompetitive-canaries/{exclusion_id}/bind`
     with the resulting `agent_id`, the same hotkey and artifact, the verified
     `screened_image_sha256`, a reason, and
     `BIND TEAM CANARY {exclusion_id} {agent_id}`.
   - All four values must equal the agent row exactly. A reservation binds
     only once.

An agent is excluded when its hotkey matches a reservation **and** either its
artifact digest or its bound `agent_id` matches:

| Case | Result |
|---|---|
| Reserved hotkey and artifact, before or after binding | Excluded; labelled `reserved` or `bound` |
| Bound agent whose screened image is later rebuilt | Still excluded; labelled `binding_drift` |
| Same hotkey, different artifact | **Not** covered. A hotkey alone never excludes; reserve that artifact separately before uploading it |
| Same artifact, different hotkey | **Not** covered. Copy detection judges it as usual |
| Every other agent | Unchanged |

## Where it applies

The validator folds weights from `GET /scoring/scores`, which serves scored
rows whether or not they are flagged eligible, so a flag alone is not enough.
The exclusion is applied as an additional AND-NOT at every weight, emission and
winner projection:

- **Validator ledger (`/scoring/scores`).**
  - Excluded rows are dropped before owner dedupe, so a legitimate sibling can
    still represent the owner.
  - The exclusion set is part of the snapshot-reuse context, and a
    database-failure replay filters stale entries against the last known set.
  - Epoch pins are built by the same materializer, so a pin never includes
    a canary. A pin is deliberately not re-filtered while it is served, because
    every validator in an epoch must fold identical bytes. Reserving before the
    first score is what guarantees no earlier pin contains the canary.
- **`list_eligible_ledger`.**
  - An excluded row is forced ineligible and flagged `competition_excluded`.
  - That feeds crown lineage, KOTH emissions, first-crowned records, the
    retest cohort, queue floors, the efficiency cohort, v9 confirmation
    candidates and deferred source review.
  - The row intentionally stays in the ledger so copy detection, screened-image
    retention and rescreening still see it.
- **Rollout authority.** `ranked_quorum_agent_ids` and the counts built on it.
- **Rollout rankings.** `rolling_top_five` and `historical_rescore_cohort`.
- **Public records.**
  - The memory leader timeline never records a team canary.
  - The provisional overlay marks it ineligible.
- **Efficiency and confirmation.**
  - The efficiency watermark includes the exclusion, so a new exclusion
    re-materializes the cohort.
  - Persisted v9 confirmation subjects do not re-admit a canary.

## Visibility

- **Public leaderboard.** Entries carry `team_canary: true`, `rank: null` and
  `eligible: false`. `emission_eligible` is `false`, or `null` when chain
  registration is unknown (as for every agent); it is never `true`. The
  dashboard shows a "team canary" chip.
- **Operators.** `GET /api/v1/admin/noncompetitive-canaries` and the read-only
  Backroom tool `list_team_canaries` list every exclusion. Each entry shows its
  audit fields and the exact agents it currently removes, with their state.
  The Backroom write tool `set_team_canary` (`action` reserve or bind, see
  `apps/backroom/docs/mcp.md`) calls the two POST endpoints with the same
  confirmations and the signed-in operator as the audit actor.

## Operating a canary

Nothing here funds, uploads or activates anything. For each canary:

1. Reserve the exact hotkey and artifact.
2. Confirm it through `list_team_canaries`.
3. Upload.
4. Bind once screening has produced the verified image.
5. Check that the public entry shows `team_canary`, with no rank and no
   emission eligibility.

Uploading a different artifact from the testing hotkey requires a new
reservation first.
