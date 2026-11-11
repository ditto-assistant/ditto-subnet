# Query adapter pilot runbook

## What this pilot is

A synthetic-only, opt-in experiment. It asks whether an SN118 miner can host
per-user Personal Query Adapter inference correctly: many small residual MLP
adapters on shared compute, with every result checked by Ditto. The
`query-adapter/v1` contract fixes the split of work:

- **Ditto** owns the frozen embedder, every per-user adapter version,
  authorization, output verification, fallback, and the usage ledger.
- **The miner** receives an authorized 768-d query and an exact
  `(tenant, adapter_version, adapter_digest)`. It returns the adjusted,
  normalized vector.

## What this pilot is not

- **Not real data.** Every tenant (`syn-...`), weight, and query is derived from
  fixed seeds. No real users, embeddings, or trained adapters are involved.
- **Not networked.** The reference miner binds loopback only, and the client
  refuses non-loopback miners.
- **Not paid.** The ledger counts accepted units for analysis. It always
  reports `payouts: "none"`.
- **No changes to the subnet.** Nothing here touches DittoBench scores,
  `bench_version`, validator weights, emissions, or competition policy.
- **Not private.** Plaintext embeddings and per-user weights are not safe on
  an untrusted host. See [Future work](#future-work-not-implemented).

## Prerequisites

Run everything from `research/query-adapter-pilot/`:

```bash
uv sync --locked --group dev --python 3.12
```

## 1. Validate

```bash
uv run ruff format --check . && uv run ruff check . && uv run mypy src && uv run pytest -q
```

The tests cover:

| Tests | What they prove |
|---|---|
| `test_contract.py`, `test_reference.py` | wire contract round trips; reference math pinned to the source golden vectors |
| `test_fixtures_workloads.py` | builders are deterministic; committed JSON has no drift; every workload parses and has the shape its scenario needs |
| `test_parity_reference.py` | `reference.apply` matches an independent float64 re-implementation on every fixture request; `golden_reference_v1.json` still matches; the fixtures can tell tenants and versions apart |
| `test_fixtures_prove_expectations.py` | every `prove.py` expectation passes on a correct report and fails on targeted corruptions |
| `test_parity_bench_local.py` | the local-baseline half of the bench is at parity, bounded, and makes no claims |
| `test_e2e_pilot.py` | every scenario against the real miner and client (in-process miner, then `scripts/prove.py`), plus the bench miner half. **These skip** until both `query_adapter_pilot.miner` and `query_adapter_pilot.client` are importable. |

## 2. Fixtures

The canonical workloads live in `testdata/workloads/*.json` and are generated
by `src/query_adapter_pilot/fixtures.py`:

```bash
uv run python -m query_adapter_pilot.fixtures --check                       # exit 1 on drift
uv run python -m query_adapter_pilot.fixtures --write testdata/workloads    # regenerate
```

| Workload | Users | Requests | Shape |
|---|---|---|---|
| `smoke` | 1 | 3 | v1 provisioned and authorized |
| `happy` | 3 | 30 | v1 each, round-robin |
| `multi_tenant` | 32 | 80 | versions [1, 2], both provisioned, v2 authorized. Two round-robin passes over all tenants, then 4 hot tenants × 4 back-to-back requests. |
| `stale` | 1 | 4 | provision [1, 2], authorized 1 |
| `missing` | 1 | 4 | provision [1], authorized 2 |
| `unauthorized` | 2 | 8 | u00 authorized. u01 provisioned, then revoked (`authorized_version: null`). Interleaved. |
| `replay` | 2 | 10 | 6 originals plus 4 `{"replay_of": i}` entries |
| `isolation` | 3 | 18 | all share versions [1, 2] with different seeds. Each round sends the **same** query to every tenant, interleaved. |

Seeds never collide across workloads (see the `fixtures.py` docstring). To add
a workload, append it to `WORKLOAD_NAMES`. Do not reorder that list, because
a workload's index feeds its seeds.

## 3. Proof

`scripts/prove.py` goes through the normal entry points. For each scenario it
does the following:

1. Writes a fresh synthetic token to a 0600 temp file.
2. Spawns `python -m query_adapter_pilot.miner --host 127.0.0.1 --port 0 ... --ready-file F` and waits for the ready file.
3. Runs `python -m query_adapter_pilot run --workload ... --miner-url ... --verify-rate 1.0 --fallback local`.
4. Reads the report and the miner's `/v1/health` and checks the expectations.
5. Sends SIGTERM to the miner, which must exit 0.

```bash
uv run python scripts/prove.py                       # all scenarios, reports under a new temp dir
uv run python scripts/prove.py --only happy stale    # a subset
uv run python scripts/prove.py --list                # scenario catalogue
uv run python scripts/prove.py --out-dir "$TMPDIR/qa-prove"
```

It prints one row per scenario:

```
scenario  outcomes  sources  accepted  dupes  min_cos  p50/p95 ms
```

It exits non-zero if any expectation fails and lists each failure. For every
scenario the out dir holds `report.json`, `health.json`, `client.log`, and
`miner.log`. Never write these into the source tree.

### Negative-case matrix

| Scenario | Workload | Miner | Expected outcome | Expected source | Also asserted |
|---|---|---|---|---|---|
| `happy` | happy | `--fault none` | `ok` (all) | `miner` | min `reference_cos` ≥ `PARITY_MIN_COSINE`. `accepted_units == total`. Every vector verified. Miner adapt traffic == total. |
| `incorrect` | happy | `--fault perturb` | `rejected_incorrect` | `fallback_local` | final parity ≥ threshold. `accepted_units == 0`. All verified. |
| `invalid-nan` | smoke | `--fault nan` | `rejected_invalid` | `fallback_local` | final parity, 0 accepted |
| `invalid-bad-norm` | smoke | `--fault bad-norm` | `rejected_invalid` | `fallback_local` | final parity, 0 accepted |
| `invalid-wrong-echo` | smoke | `--fault wrong-echo` | `rejected_invalid` | `fallback_local` | final parity, 0 accepted |
| `unavailable` | smoke | none (closed loopback port) | `unavailable` | `fallback_local` | final parity, 0 accepted |
| `timeout` | smoke | `--fault slow --slow-ms 1000`, client `--timeout-ms 200` | `timeout` | `fallback_local` | final parity, 0 accepted |
| `stale` | stale | `--fault none` | `rejected_stale` | `fallback_local` | final parity, 0 accepted |
| `missing` | missing | `--fault none` | `rejected_missing` | `fallback_local` | final parity, 0 accepted |
| `unauthorized` | unauthorized | `--fault none` | `rejected_unauthorized` for u01, `ok` for u00 | `fallback_identity` / `miner` | No miner fields on refused requests. Miner `adapt_ok + errors` == routed requests only. Accepted == authorized requests. |
| `isolation` | isolation | `--fault none` | `ok` | `miner` | Each user maps to one distinct tenant id. Per-tenant min cos ≥ threshold. |
| `isolation-swap` | isolation | `--fault swap-tenant` | `rejected_incorrect` | `fallback_local` | verification catches cross-tenant output |
| `replay` | replay | `--fault none` | originals `ok`, replays `ok`/`duplicate` | `miner` | `accepted_units == 6`. `duplicates_suppressed == 4`. Replays reuse the original request id. |
| `multi-tenant` | multi_tenant | `--cache-capacity 8` | `ok` | `miner` | Miner `cache.evictions`, `misses`, and `hits` are all > 0. Report has both `hit` and `miss` markers. |

Every scenario also checks the report shape: `contract == "query-adapter/v1"`,
`kind == "pilot-report"`, one entry per workload request in index order,
`summary.total`, known outcomes only, `syn-` user refs, and
`ledger.payouts == "none"`. Fault scenarios also check that the miner's health
reports the requested `fault`.

## 4. Reading a pilot report

`python -m query_adapter_pilot run --out PATH` writes a JSON object with these
top-level keys:

- `contract`, `kind` (`"pilot-report"`), `run_id`, `workload`, `miner_url`.
- `config`: `verify_rate`, `fallback`, `timeout_ms`.
- `provision[]`: what Ditto pushed to the miner and how the miner answered.
- `requests[]`: one entry per workload request, in order. Each has
  `index, request_id, user_ref, tenant_id, adapter_version, outcome, source,
  miner_code, latency_ms, miner_compute_us, cache, verified, reference_cos,
  reference_max_abs`.
  - `outcome` is the Ditto-side verdict (`contract.Outcome`): `ok`,
    `rejected_invalid` (shape, finiteness, norm, or echo failed),
    `rejected_incorrect` (parity failed), `rejected_stale`,
    `rejected_missing`, `rejected_unauthorized`, `miner_error`,
    `unavailable`, `timeout`, or `duplicate`.
  - `source` is where the vector Ditto finally used came from
    (`contract.Source`): `miner`, `fallback_local` (Ditto recomputed with its
    own weights), `fallback_identity` (raw normalized query), or `none`.
  - `reference_cos` / `reference_max_abs` give parity against Ditto's own
    reference computation. The correct-result bar is
    `cos ≥ 1 − 1e-6` and `max_abs ≤ 1e-5`.
- `summary`: `total`, `by_outcome`, `by_source`,
  `latency_ms{miner_ok_p50, miner_ok_p95, end_to_end_p50, end_to_end_p95}`, and
  `parity{min_reference_cos, max_reference_abs}`.
- `ledger`: `accepted_units`, `duplicates_suppressed`, `rejected`,
  `by_outcome`, and `payouts: "none"`. One unit per accepted, distinct request
  id. A replay of a settled id is suppressed, not paid.

How to read it quickly:

- `by_source.miner == total` means the miner served everything.
- Any `fallback_local` means Ditto still answered correctly, but without the
  miner. The outcome says why.
- `rejected_incorrect` is the most important signal. It means the miner
  returned well-formed but wrong vectors.

## 5. Measurements

```bash
uv run python -m query_adapter_pilot.bench --json "$TMPDIR/qa-bench.json"
uv run python -m query_adapter_pilot.bench --requests 400 --tenants 32 --cache-capacity 8 --concurrency 8 --json OUT
uv run python -m query_adapter_pilot.bench --local-only    # no miner slice needed
```

Defaults: 200 requests, 16 tenants, cache 4, concurrency 4. A default run
finishes in a few seconds on a laptop.

The bench runs these phases:

| Phase | What it times |
|---|---|
| `local.warm` | equivalent local baseline: `reference.apply` with weights already in memory |
| `local.cold` | local baseline from wire bytes: JSON parse, base64 decode, digest re-derivation and check, apply |
| `local.warm_concurrent` | `local.warm` at `--concurrency` threads |
| `miner.warm` | in-process `start_miner` over loopback HTTP. A primed hot set no bigger than half the cache, so cache hits. |
| `miner.cold` | round-robin over all tenants with `tenants > cache_capacity`, so misses |
| `miner.multi_tenant` | round-robin over all tenants at `--concurrency` |

Each phase reports p50/p95/p99 latency, throughput, CPU µs per request
(`time.process_time` delta), and parity against the reference. Miner phases
also report miner-reported `compute_us` and end-to-end overhead. The run also
reports serving-cost inputs: params and bytes per adapter, resident adapters
per GiB, and wire bytes per provision and per adapt request and response.

For in-process miner phases, CPU time includes both the client and the miner
threads.

These are local loopback numbers. They are inputs, not savings. The bench makes
no economic claim (`economic_claims: "none"`). Record results in
[`measurements.md`](measurements.md).

## Troubleshooting

- **`miner exited ... before ready`.** The miner CLI failed at startup. Look
  in `<out-dir>/<scenario>/miner.log`.
- **`client exited N`.** Look in `client.log`. Exit 0 is required whenever the
  run completes, including all-fallback runs.
- **`miner exited ... on SIGTERM (want 0)`.** The miner must handle SIGTERM
  cleanly, even while slow-fault handlers are sleeping.
- **Spurious `timeout` outside the timeout scenario.** Non-timeout scenarios
  already use `--timeout-ms 2000`. A slower host needs a larger
  `DEFAULT_TIMEOUT_MS` in `scripts/prove.py`.

## Future work (not implemented)

Hosting real per-user adapters on third-party miners needs a private path that
this pilot does not build. The proposed direction:

- **Confidential-compute miners with remote attestation.** Miners run the
  adapter server inside a TEE (for example, a confidential VM or GPU enclave).
  Before getting any work, they present an attestation that binds the measured
  image, its configuration, and a fresh key.
- **Ditto-held keys and key release.** Per-user adapter weights stay encrypted
  at rest under keys that Ditto controls. Ditto releases a tenant's key only
  to an enclave whose attestation verifies against an allow-listed
  measurement, scoped to that tenant and version, and revocable when a version
  is retired or a user opts out.
- **Encrypted query and response channels.** Ditto opens a channel that
  terminates inside the attested enclave, for example attested TLS or HPKE to
  the enclave key. The host operator never sees queries or adjusted vectors.
- **No plaintext on untrusted hosts.** Embeddings and weights never exist in
  plaintext outside Ditto or a verified enclave. The verification and fallback
  used in this pilot (parity sampling, local recompute) stay in place as a
  defense in depth.
- **Follow-on candidates** for the same hosting pattern, each needing its own
  contract version and proof:
  - learned retrieval weights (per-user scoring or fusion weights over
    HNSW candidates);
  - a small per-user or shared reranker over the candidate set Ditto
    already retrieves.

Each of these would need its own design review, a new contract version, and an
explicit policy decision before any non-synthetic traffic. None of them is
implied by this pilot's results.
