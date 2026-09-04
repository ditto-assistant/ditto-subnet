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

Public v2 external staging is semantically validated before compilation. Its
issue, raw-memory, networkless runtime-policy, and grader controls are canonical
and task-bound; hashes alone do not admit structurally invalid controls.

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

# Build a deterministic public distribution artifact. It contains only the
# committed public practice pack, a copied manifest, a release descriptor, and
# release notes; it never reads private-corpus/.
uv run dittobench-coding-datagen build-public-release \
  --pack practice/v1 \
  --output /tmp/dittobench-coding-practice-release

# Verify a downloaded archive before using it locally.
uv run dittobench-coding-datagen verify-public-release \
  --archive /tmp/dittobench-coding-practice-release/coding-practice-3x3-v1.tar.gz \
  --descriptor /tmp/dittobench-coding-practice-release/coding-practice-3x3-v1.release.json

# Bind one sanitized public-v2 snapshot into a deterministic mode-preserving
# archive for external intake. Both commands emit the same canonical receipt.
uv run dittobench-coding-datagen build-snapshot-archive \
  --snapshot /path/to/snapshot \
  --archive /path/to/snapshot/archive.tar.gz
uv run dittobench-coding-datagen verify-snapshot-archive \
  --archive /path/to/snapshot/archive.tar.gz

# Validate one task's canonical issue, raw memory, runtime policy, and grader
# before building the complete ten-task intake.
uv run dittobench-coding-datagen validate-public-controls \
  --task-root /path/to/tasks/PUBLIC-V2-00 \
  --task-id PUBLIC-V2-00 \
  --condition v1_relevant

# Grade one miner-edited public-v2 workspace in the task's exact locally
# available image. Build and test commands share one disposable, networkless
# container; the emitted task result is non-authoritative.
uv run dittobench-coding-datagen run-public-task \
  --pack /path/to/coding-public-v2 \
  --task PUBLIC-V2-00 \
  --workspace /path/to/edited-workspace \
  --image repository/image@sha256:... \
  --output /tmp/PUBLIC-V2-00.result.json

# Curators can exercise the same execution core before a complete ten-task pack
# exists. This validates one external control set but grants no release status.
uv run dittobench-coding-datagen run-public-controls \
  --task-root /path/to/tasks/PUBLIC-V2-00 \
  --task PUBLIC-V2-00 \
  --condition v1_relevant \
  --workspace /path/to/edited-workspace \
  --image repository/image@sha256:... \
  --output /tmp/PUBLIC-V2-00.control-result.json

# Compile and verify the complete external ten-task v2 staging authority.
uv run dittobench-coding-datagen compile-public-v2-pack \
  --staging-root /path/to/public-v2-staging \
  --intake /path/to/public-v2-staging/intake.json \
  --output /tmp/coding-public-v2-pack
uv run dittobench-coding-datagen validate-public-v2-pack \
  /tmp/coding-public-v2-pack

# Build and independently verify the immutable public distribution artifacts.
uv run dittobench-coding-datagen build-public-v2-release \
  --pack /tmp/coding-public-v2-pack \
  --output /tmp/coding-public-v2-release
uv run dittobench-coding-datagen verify-public-v2-release \
  --archive /tmp/coding-public-v2-release/coding-public-v2.tar.gz \
  --descriptor /tmp/coding-public-v2-release/coding-public-v2.release.json

# Create a credential-free, immutable upload handoff. This validates the
# release directory but does not contact or upload to Hugging Face.
uv run dittobench-coding-datagen plan-public-v2-publication \
  --release-dir /tmp/coding-public-v2-release \
  --dataset-repository ditto-assistant/coding-practice \
  --revision main \
  --output /tmp/coding-public-v2-publication-plan.json

# Build and audit one private group from owner-controlled files outside Git.
# The destination directories must already exist with no group/other access;
# outputs are created once with mode 0600.
uv run dittobench-coding-datagen build-private-group \
  --source /protected/coding-private-v2/group-source.json \
  --output /protected/coding-private-v2/group-manifest.json
uv run dittobench-coding-datagen audit-private-group \
  --manifest /protected/coding-private-v2/group-manifest.json \
  --visible-snapshot /protected/coding-private-v2/visible \
  --hidden-grader /protected/coding-private-v2/grader \
  --memory-bundles /protected/coding-private-v2/memory \
  --overlap-review-sha256 <review-sha256> \
  --output /protected/coding-private-v2/group-audit.json

# Compile two deterministic base observations and two deterministic reference
# observations into the calibration authority required for release admission.
uv run dittobench-coding-datagen compile-private-calibration \
  --manifest /protected/coding-private-v2/group-manifest.json \
  --observation /protected/coding-private-v2/base-01.json \
  --observation /protected/coding-private-v2/base-02.json \
  --observation /protected/coding-private-v2/reference-01.json \
  --observation /protected/coding-private-v2/reference-02.json \
  --output /protected/coding-private-v2/group-calibration.json

# Verify every currently authored group while emitting only redacted counts and
# one digest. Partial corpora remain explicitly not ready for release.
uv run dittobench-coding-datagen audit-private-corpus-progress \
  --groups-dir /protected/coding-private-v2/groups \
  --output /protected/coding-private-v2/progress.json

# Compile exactly 50 audited groups, balanced as five groups in each of ten
# repository strata, into one create-only private release authority. Each group
# binds one immutable calibration runner profile; profiles may differ across
# task-bound language toolchains. Every group directory must also contain a
# canonical `group-semantic-review.json` with a release-unique semantic family.
uv run dittobench-coding-datagen compile-private-release \
  --groups-dir /protected/coding-private-v2/release-input/groups \
  --release-id coding-private-v2-r1 \
  --output /protected/coding-private-v2/release.json
uv run dittobench-coding-datagen verify-private-release \
  /protected/coding-private-v2/release.json

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

## Public distribution

The public practice pack may be mirrored to a public Hugging Face dataset only
as the deterministic release artifact produced by `build-public-release`. The
release descriptor binds the pack manifest and archive SHA-256; miners must
download the complete release directory and verify it before extracting the
archive. The manual
`publish-coding-practice.yml` workflow requires a pre-existing public dataset
repository named by the `HF_CODING_PRACTICE_DATASET_REPO` environment variable
and an environment-scoped `HF_TOKEN`; it has no private-catalog credential or
corpus input. The operator-owned dataset card remains at the repository root;
each immutable release contains its own `RELEASE.md` under the release path.

The destination path is content-addressed by the practice-pack ID and manifest
digest. A new pack revision therefore publishes a new immutable artifact rather
than replacing a prior miner reference. The practice pack remains static,
public, and permanently `weight_eligible=false`.

## Public certification canary

`certification/v1/manifest.json` is the content-addressed public authority for
the future qualified capability-certification lease. It binds one public
practice task, its visible runner policy, public grader-file identities, a
networkless resource profile, and the locked inference-policy file. Its
canonical SHA-256 is the future `canary_manifest_sha256`.

It is not a scoring task, private catalog entry, or miner-facing capability.
The canary uses only public `PRACTICE-LEDGER-001` material and remains
`weight_eligible=false`. A change requires regenerating its referenced public
pack and policy hashes together, then reviewing the new manifest digest before
Platform can issue any related certification lease.

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
