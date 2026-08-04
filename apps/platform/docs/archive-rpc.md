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
