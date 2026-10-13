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
| Public fallback 2 / configured | [OnFinality](https://onfinality.io/en/networks/bittensor-finney) | Verified public WSS; rate-limited | Authenticated archive endpoint | `apikey` |
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

`query` attaches the key as `authorization=...` (the existing Taostats
convention); `apikey` attaches it as `apikey=...`, as documented by
[OnFinality's Enhanced API Service](https://onfinality.atlassian.net/wiki/spaces/SUPPORT/pages/1648525360/).
Both modes encode the key and replace existing instances of their selected
parameter while preserving unrelated query parameters. `path` appends an
encoded segment; `none` does not attach a key. Keep the configured URL keyless.

## Recovering stalled champion source releases

The source-emission collector uses the same archive provider configuration.
Historical payout proofs need sufficient archive capacity to replay faster
than finalized blocks arrive. Merely configuring a credential does not prove
that the provider supports the needed history or throughput. See #2850 for the
backlog and #2851 for provisioning; #2852 supplies catch-up batching and shared
reads.

1. Provision an archive plan with adequate historical-read capacity. Store
   its key in `platform-subtensor-archive-rpc-api-key`; do not put it in a
   GitHub variable or the provider URL.
2. Set the existing GitHub repository variables `SUBTENSOR_ARCHIVE_RPC_URL`
   to the provider-issued keyless WSS endpoint and
   `SUBTENSOR_ARCHIVE_RPC_AUTH_MODE` to the provider's mode (`apikey` for
   OnFinality). The Platform deployment forwards both settings and loads the
   key from Secret Manager on the VM.
3. Deploy the Platform and Backroom changes together. Read
   `get_source_release_policy` through the authenticated Backroom MCP. Its
   `release_gate.archive_provider` reports the running configuration's host,
   auth mode, `configured`, and `credential_configured` without returning a
   URL or key. Absence on an older release means unknown. These fields describe
   configuration, not provider health or successful authentication.
4. Compare successive durable `collector_cursor_block` values and blocking
   reasons. During catch-up it must sustain advancement faster than the chain
   (roughly five blocks per minute), then reach the relevant completed payout.
   Verify the exact agent ID and artifact SHA in the payout receipt rows;
   same-hotkey submissions do not share eligibility.
5. Check the exact agent's public `source_artifact` status and artifact
   endpoint after proof resolution. Eligibility remains the verified completed
   payout timestamp plus the configured embargo. Crown age and positive
   weights alone never substitute for that proof; delayed collection does not
   restart the embargo at the collection time.

This configuration does not reset the durable cursor, synthesize a payout, or
change source-disclosure policy.
