# DittoBench coding datagen

This package is the **shadow-only** coding-repair corpus compiler and contract
validator for DittoBench. It deliberately does not alter the production
DittoBench score, `bench_version`, validator ledger, or weights.

## Security boundary

The public tree contains schemas, compiler code, leakage checks, and a disjoint
three-user/three-repository practice pack with nine tasks and eighteen unique
memory records. It never contains a production task manifest, upstream
SWE-bench instance IDs, gold patches, hidden evaluation tests, corpus keys, or
production user memories.

The nine practice tasks are deliberately static protocol demonstrations with
zero per-task entropy. They teach the public wire and local workflow only; a
signature-to-patch lookup is expected to solve them and they must never be used
for scoring, calibration claims, or private-corpus authoring.

The enforceable promise is that a miner process receives only one scoped task
envelope and no grader material before its patch is frozen. Ordinary Docker
cannot prevent a validator host owner from inspecting bytes used on that host.
Strict validator blindness requires a remote trusted grader or an attested
confidential runtime and is outside this component.

## Contract identities

- `coding_contract_version`: wire and grading semantics. This package starts at
  `1`; it is independent of DittoBench `bench_version`.
- `practice_pack_id`: immutable identity of the public, non-scoring fixture.
- `corpus_scope`: must be `public_practice` in this public compiler.
- `generation_mode`: must be `static_public_protocol_demo`.
- `task_entropy_bits`: must be `0`, making the static limitation explicit.
- `weight_eligible`: must be `false`.
- `manifest.files`: sorted SHA-256 and size identities for every emitted file.

Canonical JSON is UTF-8, sorted by key, compact, and newline-terminated. A
runtime verifies those exact bytes before parsing them.

## Commands

```bash
uv sync --locked --group dev

# Rebuild the committed public practice pack deterministically.
uv run dittobench-coding-datagen compile-practice \
  --source practice-source/source.json \
  --output practice/v1 --replace

# Verify canonical bytes, hashes, scope, counts, and miner-view leakage.
uv run dittobench-coding-datagen validate-pack practice/v1

# Materialize one public task for local agent development.
uv run dittobench-coding-datagen materialize \
  --pack practice/v1 \
  --task PRACTICE-LEDGER-001 \
  --output /tmp/ditto-coding-task

# Grade a local practice workspace. The runner restores protected public tests
# from the pack and isolates stdlib imports, but remains a non-adversarial
# convenience tool rather than production integrity evidence.
uv run dittobench-coding-datagen grade \
  --pack practice/v1 \
  --task PRACTICE-LEDGER-001 \
  --workspace /tmp/ditto-coding-task

# Audit the external v0.1 curation seed without importing it into this repo.
uv run dittobench-coding-datagen audit-curation \
  /path/to/coding-dataset --output /tmp/coding-audit.json
```

## Future production topology

The future scored lane requires a separately reviewed task service and grader:

```text
committed corpus root -> post-commit selection -> per-ticket visible capsule
-> writable authoring workspace -> frozen patch -> pristine networkless grader
-> typed signed repair evidence
```

The production compiler may reuse this package's canonical byte and validation
rules, but private corpus data must remain outside public Git and ordinary image
build contexts.

Private capsules must use a separately reviewed seeded generator with a stated
minimum entropy target per task. Its post-commit seed must derive identifiers,
values, structure, bug sites, and grader expectations from one sampled semantic
specification. Static fixture names or signature-dispatched patch catalogs are
not acceptable production inputs.

See [the shadow contract](docs/SHADOW-CONTRACT.md) for the future task lease,
authoring/grading boundary, binary repair result, signed evidence root, quorum,
and activation gates. The detailed
[private execution protocol](docs/PRIVATE-EXECUTION-PROTOCOL.md) fixes ownership,
wire, scoped miner memory, Luna routing, workspace tools, evidence, failure, and
retirement decisions for subsequent implementation PRs.
