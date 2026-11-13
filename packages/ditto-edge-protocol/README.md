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
| `placement` | Which objects a node holds next: least-replicated first, never twice behind one operator. |
| `challenges` | Secret per-window challenge slots, challenge drawing, and judging answers against the verifier's own bytes. |
| `ledger` | One closed window, one node, one row: availability, held GiB-hours, eligibility, verified units, hypothetical share. |
| `paths` | Platform endpoint paths, which nodes sign exactly. |
| `vectors` | Conformance vectors (`testdata/conformance_v1.json`) that the Desktop helper must reproduce byte for byte. |

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
