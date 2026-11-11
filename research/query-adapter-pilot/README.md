# Query adapter hosting pilot (synthetic-only)

This is a **synthetic-only, opt-in** SN118 pilot. It tests whether miners can
host per-user Personal Query Adapter inference. It changes no DittoBench score,
`bench_version`, validator weight, emission, or competition policy. It never
touches real users, real embeddings, or production routing.

## What is hosted

Seed Memories v4 adds a small per-user residual MLP on top of the frozen
768-d `text-embedding-005` query embedding:

    f(q) = normalize(q + alpha * MLP(q)),   MLP = Linear(768->128), GELU(tanh), Linear(128->768)

Each adapter is about 197k float32 parameters (~0.79 MB). One miner can serve
many adapters on shared compute.

| Owner | Responsibility |
|---|---|
| Ditto | frozen embedder, every versioned per-user adapter, authorization, HNSW search, context assembly |
| Miner | given an authorized 768-d query and an exact `(tenant, version, digest)`, return the adjusted normalized vector |

The wire contract is `query-adapter/v1`. See
[`src/query_adapter_pilot/contract.py`](src/query_adapter_pilot/contract.py).
The reference math is
[`src/query_adapter_pilot/reference.py`](src/query_adapter_pilot/reference.py),
pinned by golden vectors produced from the source `QueryAdapter.apply`.

## Safety boundary

- Every weight, tenant, and query is synthetic, derived from seeded RNGs.
  Tenant aliases must match `syn-...`.
- The reference miner binds loopback only. The pilot client refuses
  non-loopback miner URLs.
- Neither embeddings nor per-user weights are safe on an untrusted host. A
  production path would need confidential compute with attestation and
  Ditto-held keys. That is future work and out of scope here; see
  `docs/runbook.md`.

## Layout

| Path | Slice |
|---|---|
| `contract.py`, `reference.py`, `workload.py` | shared contract (C0) |
| `miner/` | reference miner runtime: validation, tenant/version cache, stale/missing rejection |
| `client/`, `pilot.py`, `__main__.py` | Ditto-side pilot client: authz, scoped ids, output verification, fallback, usage ledger |
| `fixtures.py`, `bench.py`, `testdata/workloads/`, `docs/runbook.md` | deterministic fixtures, parity and warm/cold/multi-tenant proof, measurements |

## Validate

```bash
uv sync --locked --group dev --python 3.12
uv run ruff format --check . && uv run ruff check . && uv run mypy src && uv run pytest -q
```
