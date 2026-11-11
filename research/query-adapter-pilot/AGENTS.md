# Query adapter pilot guidance

This component is a synthetic-only, opt-in pilot for miner-hosted Personal
Query Adapter inference. It must not change DittoBench scores, `bench_version`,
validator weights, emissions, or competition policy, and it pays nothing.

- Every tenant, weight, and query is synthetic and derived from seeds. Tenant
  aliases match `syn-...`. Never add real user data, real embeddings, trained
  adapter weights, or production routing.
- The reference miner binds loopback only. The client refuses non-loopback
  miners. Do not relax either without a separate private-hosting design
  (attestation and Ditto-held keys).
- `contract.py` is the `query-adapter/v1` wire contract. A wire change needs a
  new contract version, not an in-place edit.
- `reference.py` must stay pinned to `testdata/golden_reference_v1.json`, which
  was produced by the source `QueryAdapter.apply`.

Validate with:

```bash
uv sync --locked --group dev --python 3.12
uv run ruff format --check . && uv run ruff check . && uv run mypy src && uv run pytest -q
```
