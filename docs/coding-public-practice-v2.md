# Coding public practice v2

Status: proposed shadow-only public practice contract. This document does not
change Coding weights, emissions, private release selection, provider access,
or deployment.

## Purpose

Public practice v2 gives miners a reproducible local development surface for
the Coding protocol. Its result is a development signal only. It is never
authoritative evidence, a validator consensus input, a leaderboard value, or a
reward input.

```text
Local Practice Score
    = miner-owned development result

Public Validator Canary
    = protocol and artifact qualification only

Private Validator Score
    = future shadow-only competitive evidence
```

Admission remains the existing unified order. A public canary pass is not
private-shadow admission: the exact screened artifact must already have a
durable Tool + Memory core-qualification decision, then a claimed certification
lease, before any private task.

The current `coding-practice-3x3-v1` pack remains supported until v2 is
published. It contains nine static protocol fixtures and is not retroactively
rescored or changed by this proposal.

## Public v2 release shape

One immutable public v2 release contains exactly ten tasks from four reviewed
repository families:

| Language family | Tasks |
| --- | --- |
| Python | 3 |
| TypeScript or JavaScript | 3 |
| Rust | 2 |
| Go | 2 |

The selection manifest assigns exactly two tasks to every memory condition:

```text
V0 no relevant memory          2
V1 relevant memory             2
V2 irrelevant memory           2
V3 stale/conflicting memory    2
V4 current override            2
```

The condition split demonstrates the wire and failure modes; it is not a
causal estimate of miner capability because public task difficulty differs by
repository. The local UI MUST label all condition summaries as practice
diagnostics.

Every task must be deterministic, networkless at run time, independently
buildable, and bounded by release policy. The reviewed release manifest binds
the public source licence, normalized source snapshot, visible issue text,
visible tests, public grader, runtime policy, task condition, and task digest.

## Local practice result

The future v2 runner emits one canonical result containing at least:

```text
public_release_id
artifact_sha256
task and grader identities
resolved and total counts
per-condition counts
protocol, build, patch, timeout and resource diagnostics
authoritative = false
leaderboard_eligible = false
reward_eligible = false
```

The Platform MUST reject any miner-claimed local result as competitive
evidence. A public result may be displayed or attached for debugging only.

## Public source snapshots

Large public repository snapshots are immutable release artifacts, not ordinary
monorepo fixtures. A snapshot export MUST omit `.git`, source credentials,
editor state, host paths, unrelated build caches, and hidden grader material.
It binds normalized paths, file modes, file sizes, and SHA-256 identities.

Public source provenance may be published. Public task snapshots, issue text,
tests, and graders may be fully inspectable because hardcoding is acceptable in
the local-practice lane. No public task, source issue, module, test pattern, or
reference patch may be reused as a private scoring task.

The sanitized snapshot v2 file identity includes a numeric normalized POSIX
mode. Regular files are exactly `0644` (`420` in canonical JSON) or `0755`
(`493`); every other mode fails closed. Staging, pack compilation, release
archiving, extraction, and validation preserve and re-verify the same mode.

Every staged snapshot also includes a required deterministic `archive.tar.gz`.
Its canonical gzip/tar bytes contain only the snapshot manifest and workspace
files under the mode-aware tree digest. Intake binds the archive SHA-256;
staging replays the archive, manifest, tree, and mode authorities before pack
compilation. An absent, non-canonical, appended, or mismatched archive fails
closed.

## Memory and retrieval

Public and private Coding data authorities contain raw memory text plus
structured metadata. Public practice v2 does not require Perplexity, another
external embedding provider, or prepared embedding vectors. Miners may use
lexical search, metadata filters, local models, or their own self-contained
retrieval strategy.

The reference starter remains a baseline. Its local retrieval choice is not a
public practice scoring rule and does not constrain private miner innovation.

## Task controls

Every staged task carries canonical, task-bound public controls:

```text
dittobench-coding-public-issue-v2
dittobench-coding-public-memory-v2
dittobench-coding-public-runtime-policy-v2
dittobench-coding-public-grader-v2
```

The issue binds bounded visible requirements. The memory control contains raw
records and structured validity/supersession metadata only; embedding or vector
fields are invalid. V0 contains no records and V1-V4 contain at least one.
Issue descriptions, constraints, and memory content containing unified-diff or
Git patch markers are invalid. Public provenance may link to an upstream issue,
but the task controls must not package a patch-form reference repair.

The runtime policy binds one immutable `linux/amd64` image digest, `network =
none`, normalized edit/create/delete paths, resource limits, and argv-only
commands. Shells, Git, inline Python, mutable `npx`, Cargo without `--offline`,
incomplete offline pip installation, URLs, and credential-shaped environment
variables fail closed. `--locked` is permitted but not required because a
historical snapshot may need the pinned runtime image's Cargo to refresh its
lockfile from the image-local cache.

The grader binds the same runtime image, ordered fail-to-pass and pass-to-pass
groups, distinct validator-only command IDs, and exact mode-aware files. Grader
files may be injected only into test-scoped destinations. Patch/diff files and
answer-, gold-, reference-, or solution-named source material are invalid.

The local task runner verifies that a locally available image resolves to the
bound digest, reconstructs a grading workspace from the immutable visible
snapshot plus only policy-authorized candidate edits, and injects the public
grader after reconstruction. Build and test commands execute in order inside
one disposable container with networking disabled, all Linux capabilities
dropped, and `no-new-privileges` enabled. A zero exit status is insufficient:
every declared fail-to-pass and pass-to-pass test identity must also be observed
before the task is reported resolved. The resulting task record remains
non-authoritative local evidence.

## Public validator canary

The public validator canary reuses only public v2 material. It verifies the
screened artifact and protocol path, including health, seed, ticket-scoped
inference, run, workspace freeze, and pristine public grading. A pass is a
coding-capability qualification gate only: it adds no Coding score and does not
bypass core qualification or the certification lease.

## Publication rules

The public release is content-addressed and must be verified before extraction.
A new release publishes a new immutable pack ID; it never mutates an existing
pack. Public release publication has no private-catalog credential, no Hippius
private-input access, and no private grader dependency.

Private task bytes, private repository epochs, private memories, hidden tests,
reference patches, private object keys, and decryption material remain outside
public Git, ordinary image builds, and public practice artifacts.
