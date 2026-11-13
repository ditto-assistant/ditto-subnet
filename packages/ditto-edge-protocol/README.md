# ditto-edge-protocol

The `edge/v1` contract for **Ditto Edge**, the shadow-only public-bytes lane
from [SN-481](../../docs/design/sn481-miner-hostable-workloads.md). Opt-in Mac
nodes hold content-addressed copies of public Ditto objects that Platform
assigns to them, and answer Platform's challenges. Ditto keeps every trust
decision: the page wrapper, publication state, leases, revocations, and the
verifier.

This package holds the parts every implementation must agree on:

| Module | What it fixes |
|---|---|
| `wire` | Request and response models. Nodes learn a digest, size, media type and lease, never a URL, owner, title or account. |
| `signing` | Ed25519 device keys, `node_id`, the canonical bytes every request signature covers, and the hotkey binding message. |
| `proofs` | SHA-256 content addressing and nonce-keyed possession proofs. |
| `placement` | Salted rendezvous hashing: each object's holders are the top `replicas` live nodes, one per operator. Load spreads evenly, joining nodes take their share, and the salt stops key grinding. |
| `challenges` | Secret per-window challenge slots, challenge drawing, and judging answers against the verifier's own bytes. |
| `ledger` | One closed window, one node, one row: availability, held GiB-hours, eligibility, verified units, hypothetical share. |
| `paths` | Platform endpoint paths, which nodes sign exactly. |
| `vectors` | Conformance vectors (`testdata/conformance_v1.json`) that the Desktop helper must reproduce byte for byte. |

It also ships the runtime the contract is proven with:

| Module | What it does |
|---|---|
| `node` | The reference node: poll, revoke, answer from disk only, fill and verify, expire, acknowledge. The Desktop helper ports this order. |
| `store` | Digest-verified object stores: in-memory, and an atomic 0600 directory store. |
| `settings` | Lane tunables with shadow defaults (Platform mirrors these). |
| `coordinator` | Platform's lane rules in memory: enrollment, signed-request checks, rendezvous placement, secret challenge slots, revocation, insert-once ledger. |
| `transport` | Signed HTTPS to Platform (stdlib, no proxies or redirects), or in-process to a coordinator with the same signatures and JSON. |
| `sources` | Synthetic objects shaped like production's mix, and anonymous reads of real public shares from `share.heyditto.ai`. |
| `dryrun` | The whole lane in one process: honest, part-time and adversarial nodes over several windows, with a pass/fail verdict. |
| `cli` | `ditto-edge keygen`, `ditto-edge node run/status/leave`, and `ditto-edge dry-run`. |

## Run a node on a Mac mini (headless)

```sh
uv run ditto-edge keygen                      # creates ~/.ditto-edge/device.key (0600)
uv run ditto-edge node run --quota-gb 2       # outbound HTTPS only; Ctrl-C to stop
uv run ditto-edge node leave                  # releases everything and deletes the cache
```

The node needs no wallet and no TAO. Platform refuses enrollment while the lane
is `off`. In `shadow` it measures the node, and nothing pays.

## Dry run against prod-shaped traffic

```sh
# Synthetic objects shaped like production's public-content mix:
uv run ditto-edge dry-run --windows 4 --synthetic 40 --out report.json

# Real public shares, read anonymously from share.heyditto.ai and rechecked
# every window (an unpublished share is revoked and measured):
uv run ditto-edge dry-run --share-id <id> --share-id <id> --recheck-public
```

It runs honest nodes, a part-time node, and three adversaries:
- `lazy` holds nothing and re-downloads to answer;
- `keeps_revoked` ignores revocations;
- `tamper` corrupts answers.

The run exits 0 when all of these hold:
- no honest window fails;
- every adversary is caught;
- every online honest node releases revoked objects within one lease plus one
  poll.

It needs no deployment, no secret and no admin write.

## Shadow boundary

- `LaneMode` is `off` or `shadow`. There is no `enforce`. Leaving shadow needs
  a new contract revision and Peyton's approval.
- Ledger rows are hypothetical. Platform stores them with
  `weight_eligible = false`, and nothing here touches weights, emissions,
  track shares or users.
- Nothing a node reports about itself is scored. Every scored outcome is
  checked against bytes the verifier holds.

## Develop

```sh
uv sync
uv run ruff format --check . && uv run ruff check .
uv run mypy ditto_edge_protocol
uv run python -m ditto_edge_protocol.vectors --check testdata/conformance_v1.json
uv run pytest -q
```

If a change alters derived bytes, regenerate the vectors with
`uv run python -m ditto_edge_protocol.vectors --write testdata/conformance_v1.json`.
Every other implementation must then match the new file.
