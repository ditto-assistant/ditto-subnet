# Ten-task public Coding practice

This Git directory contains the complete public dataset in a deterministic
7.6 MB archive, its descriptor, file manifest, public source provenance and
digest-pinned runtime image references. Datagen expands it to about 43 MB.
The upstream MIT licence notices are included in the source snapshots.
The source tasks come from public SWE-bench Verified and Multilingual instances;
they are excluded from private scoring. No hosted dataset account is needed.

| Task | Language | Memory condition |
| --- | --- | --- |
| PUBLIC-GIN-2121 | Go | V2 irrelevant |
| PUBLIC-GIN-3820 | Go | V4 current override |
| PUBLIC-PREACT-2927 | JavaScript | V0 none |
| PUBLIC-PREACT-3345 | JavaScript | V4 current override |
| PUBLIC-PREACT-3739 | JavaScript | V3 stale/conflicting |
| PUBLIC-PYTEST-5262 | Python | V0 none |
| PUBLIC-PYTEST-7571 | Python | V1 relevant |
| PUBLIC-PYTEST-7982 | Python | V2 irrelevant |
| PUBLIC-TOKIO-6603 | Rust | V3 stale/conflicting |
| PUBLIC-TOKIO-6752 | Rust | V1 relevant |

## Setup and one-task grading

From `research/dittobench-coding-datagen`, with Python 3.12 or 3.13, uv,
Docker (linux/amd64) and jq available:

```bash
uv sync --locked --group dev
uv run dittobench-coding-datagen unpack-public-practice \
  --archive practice/v2/coding-public-v2-2026-09-04-r2.tar.gz \
  --descriptor practice/v2/coding-public-v2-2026-09-04-r2.release.json \
  --output /tmp/coding-public-pack
uv run dittobench-coding-datagen prepare-public-workspace \
  --pack /tmp/coding-public-pack --task PUBLIC-GIN-2121 \
  --output /tmp/coding-public-task
docker pull "$(jq -r '."PUBLIC-GIN-2121"' practice/v2/images.json)"
# Read issue.json and memory.json; let your harness edit only workspace/.
uv run dittobench-coding-datagen run-public-task \
  --pack /tmp/coding-public-pack --task PUBLIC-GIN-2121 \
  --workspace /tmp/coding-public-task/workspace \
  --image "$(jq -r '."PUBLIC-GIN-2121"' practice/v2/images.json)" \
  --output /tmp/coding-public-task-result.json
```

Output directories must be new. Image download needs network access during
setup; grading runs without network access. Runtime images are substantially
larger than the task archive, so pull only the tasks you want initially.
The grader uses the exact image bound in each runtime policy, verifies the
candidate's editable paths, restores public grading files and checks declared
test identities. The original workspace contains the bug and should not resolve.

## Ten-task local score

Prepare one directory per task under `/tmp/coding-public-workspaces`, using
`prepare-public-workspace --output /tmp/coding-public-workspaces/<task-id>`.
Use your own harness to edit each `workspace/`, and pull the images from
`images.json`. Then:

```bash
uv run dittobench-coding-datagen run-public-practice \
  --pack /tmp/coding-public-pack --workspaces /tmp/coding-public-workspaces \
  --images practice/v2/images.json \
  --harness-artifact-sha256 <64-character-sha256-of-your-harness> \
  --output /tmp/coding-public-score.json
```

The report contains per-task results, condition summaries and the fraction
resolved. It is non-authoritative and contributes nothing to private ranking,
weights or rewards. The artifact digest is a local user-supplied report label.
This command grades edited workspaces; it does not launch a miner artifact or
implement the future hosted v2 seed/run protocol. The existing Rust starter's
v1 protocol regression tests remain separate.

For reproducibility, unpack the archive and run `build-public-v2-release`
against the resulting pack. The regenerated archive must have SHA-256
`4d91d8f8fb5fee246ec5ec96d048afef99942728e0027fcee5bc8ca1f14817cb`.
Read `provenance.json` for original issue links and snapshot/grader identities.
Private tasks and private grading resources are never accepted as local
practice inputs by these release commands.
