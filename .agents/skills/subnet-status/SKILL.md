---
name: subnet-status
description: >
  Look up SN118's live state as a miner, from public endpoints only. Covers:
  which benchmark and harness model score right now; the emission split of the
  miner pool (Gamma, burn, miners) and the KOTH payout curve; the current
  champion and paid slots; the dethrone margin; the next weight epoch; and the
  status, scores, screening and review outcome of your own agents. Use when a
  miner asks "what are the current parameters", "which competitions are
  running", "what does rank N pay", "why is my agent not on the board",
  "when is the next epoch", or "what happened to my submission".
---

# SN118 subnet status (miner view)

This skill is read-only. It uses only the public API at
`https://dittobench.ai/api/v1/public`, with no wallet, Backroom access or
operator token. Use it before deciding what to build or submit, and pair it
with `$mine` for building, rehearsing and uploading.

## One command

```bash
python3 .agents/skills/subnet-status/scripts/subnet_status.py --hotkey <your-ss58-hotkey>
```

It prints:

1. **Benchmark:** the scoring `bench_version`, the rollout state, and the
   locked harness model and reasoning effort. Only the scoring version pays.
2. **Emission split:** the public forecast for the miner pool, split into
   Gamma (service bucket), burn and miners.
   - Subtensor gives miners 41% of each block's emission. The split above is
     applied to that pool.
   - `routing` says whether the Gamma routing shown on this page is activated.
   - The on-chain incentive (below) is ground truth.
3. **Competition (memory KOTH):** the payout curve (`rank_shares`), the
   dethrone margin and z, the champion, and each paid recipient. Each
   recipient's share is shown both as a share of KOTH and as a share of all
   miner emission. Only the champion and the next four distinct miners are
   paid.
4. **Epoch:** when validators next submit weights. A score change pays from
   the next epoch at the earliest.
5. **Your agents** (with `--hotkey`): your newest five submissions, each with:
   - status;
   - scores against the quorum;
   - composite;
   - board rank;
   - any screening or review reason.

Add `--json` for the raw snapshot to script against.

## Reading the results

- **Board order is not composite order.** The crown passes by first-seen KOTH.
  A challenger must beat the champion by the larger of the margin and the
  statistical band. That is why a higher composite can sit below an earlier
  champion.
- **Statuses:**
  - `evaluating`: still collecting validator scores (the quorum is 3). An
    operator can still reject a proven violation at this stage.
  - `scored`: ranked.
  - `banned`: rejected by board review. The reason on the agent is
    miner-visible.
  - `rejected` or `quarantined`: stopped by screening.
- **The payout curve and the split are operator settings.** They can change
  between epochs. Re-run this before relying on a number. Changes to the
  curve take effect only once every validator runs the protocol that reads it.
- **Treat the public pages as a forecast.** For what each UID actually earns,
  read the chain. The `Incentive` and `Emission` vectors for netuid 118 are
  readable with `async-substrate-interface` (see the operator economics
  runbook) or on taostats.

## Endpoints (for your own tools)

| Need | Endpoint |
|---|---|
| Scoring version and rollout | `GET /bench/rollout` |
| Harness and grading | `GET /bench/config` |
| Gamma, burn and miner split | `GET /treasury-allocation` |
| Board, payout curve, champion and recipients | `GET /leaderboard?limit=N` (`emissions` block) |
| Frozen per-epoch recipients | `GET /ledger-epochs?limit=N` |
| Next epoch | `GET /weights` (`epoch` block) |
| Your submissions | `GET /miners/{hotkey}` |
| One agent's status and reasons | `GET /agent/{agent_id}/summary` |
| Recent submissions | `GET /submissions?limit=N` |

Send an explicit `User-Agent` header: the CDN refuses some default client
agents with HTTP 403.

## Out of scope

- This skill does not change anything and does not read private data.
- Anything about building or testing your agent belongs to `$mine`.
- Disputes go to the operator with the exact agent UUID and the `file:line`
  you dispute.
