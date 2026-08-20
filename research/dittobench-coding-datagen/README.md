# DittoBench coding datagen

This package is the **shadow-only** coding-repair corpus compiler and contract
validator for DittoBench. It deliberately does not alter the production
DittoBench score, `bench_version`, validator ledger, or weights.

Language-neutral Python/Go/Rust wire and digest vectors live in
`packages/dittobench-coding-contract`; this component must remain compatible
with those shared vectors.

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

# Exercise a loopback coding harness through /coding/health, /coding/seed,
# /coding/run, validator-owned typed tools, an authoritative workspace freeze,
# and a fresh practice grader.
uv run dittobench-coding-datagen evaluate-practice \
  --pack practice/v1 \
  --task PRACTICE-LEDGER-001 \
  --harness-url http://127.0.0.1:8080

# Audit the external v0.1 curation seed without importing it into this repo.
uv run dittobench-coding-datagen audit-curation \
  /path/to/coding-dataset --output /tmp/coding-audit.json
```

## Public practice runner

Every compiled task carries a manifest-bound public runtime policy. The current
pack allows edits only to `app.py` and exposes the fixed command IDs
`visible-unit` and `python-compile`. A harness never receives the temporary
workspace path.
It drives the workspace through a random loopback capability with these tools:

```text
repo.list_tree
repo.search
repo.read_file
repo.read_range
repo.apply_patch
repo.create_file (reserved; disabled by this pack)
repo.delete_file (reserved; disabled by this pack)
tests.run
build.run
git.status
git.diff
```

`repo.apply_patch` uses an expected file SHA-256 plus atomic, unambiguous text
replacements. After `/coding/run` returns, the evaluator revokes the capability,
freezes the changed-path and tree identities, reconstructs a pristine base, and
injects the public regression tests only for grading. The result remains binary
and `weight_eligible=false`.

Each of the nine tiny fixtures deliberately admits exactly one undecorated pure
function in `app.py`. The fixed build gate rejects imports, helper or class
definitions, module-level execution, dangerous builtins, dunder access, and
calls outside the fixture allowlist before test imports. This prevents a
practice patch from converting an early process exit into a false passing
grade; it is not a general policy for real repositories.

That one-function constraint is only a public v1 fixture limitation, not the
benchmark's difficulty target. The next public practice revision must include a
multi-file task large enough to require tree, search, and ranged-read tools;
private repository tasks may enable manifest-scoped create/delete operations.

This loopback runner is an offline protocol and integration-test fixture. It is
not an isolation boundary for arbitrary hostile local processes: a production
runner still requires the separately reviewed container, network, resource,
credential, and task-capability controls in the private execution protocol.

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
