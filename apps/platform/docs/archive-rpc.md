# Finney archive RPC failover

Historical payment verification reads `System.Events`,
`SubtensorModule.Owner`, and `Timestamp.Now` at the payment block hash. The
live Finney RPC prunes that state, so the platform uses this ordered try list:

1. One optional operator-configured archive provider (authenticated when a key exists)
2. OTF public archive (`archive.chain.opentensor.ai`)
3. OnFinality public archive (`bittensor-finney.api.onfinality.io/public-ws`)

The two public endpoints are free and both served a known-pruned Finney payment
block in a read-only production-network probe on 2026-07-20. Public services
are rate-limited and provide no application SLA, so production can prefer a
paid endpoint with `SUBTENSOR_ARCHIVE_RPC_URL`,
`SUBTENSOR_ARCHIVE_RPC_API_KEY`, and `SUBTENSOR_ARCHIVE_RPC_AUTH_MODE`.
Each attempt has a configurable 10-second connection-plus-query deadline via
`SUBTENSOR_ARCHIVE_RPC_TIMEOUT_SECONDS`.

## Provider candidates

| Try-list role | Provider | Free archive result | Paid/archive option | Auth mode |
| --- | --- | --- | --- | --- |
| Public fallback 1 | [OTF](https://www.bittensor.com/docs/guides/running-a-node) | Verified; documented for occasional historical reads | Self-hosted node | `none` |
| Public fallback 2 / configured | [OnFinality](https://onfinality.io/en/networks/bittensor-finney) | Verified public WSS; rate-limited | Authenticated archive endpoint | provider-issued URL |
| Configured | [Taostats](https://docs.taostats.io/reference/hosted-rpc-connectivity) | API key required | Hosted Finney archive RPC | `query` |
| Configured | [Dwellir](https://www.dwellir.com/networks/bittensor) | Published test endpoint was rate-limited during the probe | Authenticated Bittensor endpoint | `path` |
| Configured | [Blockmachine](https://blockmachine.io/bittensor-rpc) | Public endpoint returned pruned state for the probe block | Standard and higher advertise archive guarantees | provider-issued URL |

FlameWire and Nodies candidate public endpoints failed the same live probe and
are not included in the automatic list. Re-test providers against a known-old
block before changing the defaults.

Credentials are attached only at connection time, redacted from errors, and
never logged as part of the endpoint URL. Production deploys read the optional
API key directly from Secret Manager secret
`platform-subtensor-archive-rpc-api-key` (overridable with
`SUBTENSOR_ARCHIVE_RPC_SECRET_ID` / `SUBTENSOR_ARCHIVE_RPC_SECRET_PROJECT`). If
the secret is absent or the configured provider fails, the same request falls
through to the credential-free archives.

## Recovering stalled champion source releases

The source-emission collector reads the same try list. It needs an archive
that replays historical blocks faster than Finney finalizes them (roughly five
per minute); the free public archives throttle long before that. See #2850 for
the backlog and #2851 for provider selection.

Production uses Taostats. Its configuration needs no separate archive secret:

1. In the `prod` GitHub Environment, set `SUBTENSOR_ARCHIVE_RPC_URL` to
   `wss://api.taostats.io/api/v1/rpc/ws/finney_archive` and leave
   `SUBTENSOR_ARCHIVE_RPC_AUTH_MODE` unset or `query`. For that exact URL,
   Platform reuses `DITTO_TAOSTATS_API_KEY`, which deploys already load from
   `platform-taostats-api-key`. Do not create
   `platform-subtensor-archive-rpc-api-key` for Taostats; an explicit archive
   key takes precedence over the reused one.
2. Deploy Platform, then read `get_source_release_policy` through Backroom.
   `release_gate.archive_provider` should report host `api.taostats.io`, auth
   mode `query`, and `credential_configured: true`. It never returns the URL
   or key, and it describes the served configuration rather than provider
   health; absence means a Platform release that predates the field.
3. Compare successive `collector_cursor_block` readings and
   `collector_blocked_reason`. Catch-up requires sustained advancement faster
   than the chain, without rate-limit blocking reasons. The reason is cleared
   by each successful sweep, so read it several times. `server rejected
   WebSocket connection: HTTP 401` from the first provider, while Taostats
   validator-name enrichment still refreshes, means the key is valid but its
   plan does not include archive RPC (a paid tier); the collector then falls
   through to the throttled public archives.
4. Once the cursor passes a champion's completed payout, verify the exact
   agent ID and artifact SHA in the payout receipt rows, then the agent's
   public artifact status. Release time remains the verified payout timestamp
   plus the configured embargo; collection delay does not restart it, and
   crown age or positive weights never substitute for the proof.

Configuring a provider does not reset the durable cursor, synthesize a payout,
or change source-disclosure policy.

## Sparse catch-up replay

When the collector is more than 1,000 blocks behind, it plans each catch-up
range of up to 720 blocks before reading it (#2939):

1. One batched `chain_getBlockHash` fetches every hash in the range.
2. The runtime code hash, `MechanismCountCurrent`, `CommitRevealWeightsEnabled`
   and the `Keys` map are compared at the two ends of the range. Any
   difference, or an invalid value at either end, runs the unchanged
   per-block scan for that range.
3. Otherwise each block costs one `state_queryStorageAt` for `System.Events`
   and `LastMechansimStepBlock`. Only payout blocks, blocks with a
   netuid-scoped `WeightsSet`, `TimelockedWeightsRevealed` or
   `IncentiveAlphaEmittedToMiners`, `System.CodeUpdated`, or a structural
   SubtensorModule event (swap, registration, dissolution) get the full
   per-block reader. Every other block commits as part of one cursor skip.
4. Two sampled skipped blocks per range are re-read in full. Any disagreement
   fails the sweep closed without moving the cursor.

A provider that rejects any of these calls falls back to the per-block scan.
`LastUpdate` cannot replace the event filter: under commit-reveal, a reveal
write does not update it.
