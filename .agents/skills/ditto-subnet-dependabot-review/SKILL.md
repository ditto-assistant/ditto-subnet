---
name: ditto-subnet-dependabot-review
description: Security-review open Dependabot and other dependency PRs against upstream source, then squash-merge approved updates or drive Dependabot with ignore/close comments. Use when the user runs /dependabot-review, asks to review dependency PRs, merge Dependabot, audit a lockfile or image bump, or says not to merge deps without reviewing upstream.
---

# Dependabot security review

Do not merge a dependency PR from its title, Dependabot summary, or a green
unrelated check. Inspect upstream source. Merge only after the user authorizes
it; this skill does not grant merge authority.

## Inventory

```bash
gh pr list --repo ditto-assistant/ditto-subnet --state open \
  --author "app/dependabot" --limit 100 \
  --json number,title,url,headRefName,labels,mergeable,mergeStateStatus,additions,deletions,changedFiles
```

Also treat human PRs that only bump a lockfile, image tag, or action SHA as
dependency reviews. Config lives in [`.github/dependabot.yml`](../../../.github/dependabot.yml).

## Review each PR

1. Read `gh pr diff` and `gh pr checks`. The owning-component job must pass
   (dashboard job for `apps/platform/dashboard`, DittoBench image jobs for
   `services/dittobench-api` Docker/Go, screener jobs for worker pins). A
   compose-config pass is not evidence the image or formatter works.
2. Confirm the diff touches only the claimed package and its lock/hash file.
3. Fetch upstream: tagged compare, release notes, `dist/` or built artifact
   (not just `package.json`), and GHSA/OSV/PyPI/npm registry hashes.
4. Classify blast radius, then approve or block.

| Class | Examples here | Bar |
|---|---|---|
| Production runtime | screener image, dittobench sandbox CLI, `golang.org/x/sys`, numpy IAP pin | Upstream source + compatibility with repo pins |
| Build backend | Hermes `setuptools` bound | Changelog for the newly allowed major; no C-ext or custom compiler use |
| CI action | `hashicorp/setup-packer` commit pin | Official repo, verified commits, **compare `dist/` and `action.yml`**, not lockfile-only |
| Dev/tooling | `@types/*`, oxlint, vite patch, jest-dom | Changelog + owning CI; formatter minors are not patches |

Default **block**:

- Runtime or image major (Python 3.12→3.14, Docker CLI 28→29).
- Version outside `requires-python`, `python_version`, or a digest pin that
  those constraints still describe. Several Python packages here are
  `>=3.12,<3.14` or `>=3.11,<3.14`.
- Image tag with no manifest for the CI/build platform.
- Formatter minor (Prettier documents output drift; `pnpm format:check` is
  required and will fail without a companion reformat).
- Action SHA not on the official repo, or `dist/` / `action.yml` changing in
  a way the changelog does not explain.
- Hash file whose sha256 values do not all exist on the official registry.
- Owning-component CI red.

Default **approve** when the owning job is green and the lockfile is clean:

- Official patch whose hashes/sums match the registry.
- Types-only DefinitelyTyped bump.
- Dev linter or bundler patch whose upstream notes are bugfixes, not a
  supply-chain or plugin-exec surprise.
- Official `golang.org/x/sys` bump when this repo only uses stable
  `unix.Open`/`Openat`/`Fstat`/`Fsync`/`Unlinkat` (see
  `services/dittobench-api/cmd/dittobench-api/transcript.go`).
- Action SHA bump whose `dist/index.js` blob matches a tagged release.

## Repo-specific traps

- **Screener Python image** (`workers/screener/Dockerfile`) is
  `python:3.12-slim@sha256:…`. Dependabot will offer `3.14-slim`. The worker
  `requires-python = ">=3.12,<3.14"`. Ignore the 3.14 minor; keep 3.12 digest
  updates.
- **Embedder fetch stage** (`apps/platform/docker/embedder/Dockerfile`) uses
  `python:3.11-slim` only to run a pinned `huggingface_hub[cli]`. The
  runtime is TEI. Do not jump that interpreter three feature releases as a
  silent bump.
- **DittoBench sandbox** (`FROM docker:*-cli-alpine*` in
  `services/dittobench-api/Dockerfile`) talks to a **host** daemon to
  `docker image load` / `run` / `build` untrusted miner submissions. A CLI
  major is a sandbox compatibility change, not a tag bump. 29.x also broke
  CI here when the tag had no matching platform manifest.
- **setup-packer** in `.github/workflows/screener-bake.yml` is commit-pinned
  and installs an explicit Packer version. Dependabot tracks untagged `main`.
  Compare `dist/index.js` to the latest tagged release; a lockfile-only SHA
  move that does not rebuild `dist/` is a no-op runtime.
- **numpy** in `workers/screener/requirements-iap.txt` is the IAP transport
  pin, not the screener runtime lock. Verify every `--hash=sha256` against
  `https://pypi.org/pypi/numpy/<ver>/json`. Extra official platform hashes
  are fine; unknown hashes are not.
- **Dashboard npm PRs** all edit `apps/platform/dashboard/package-lock.json`.
  Merge them one at a time, then `@dependabot rebase` the rest.

## Upstream commands

```bash
# Action SHA: who owns it, is it tagged, did dist/ change?
gh api repos/OWNER/ACTION/commits/SHA --jq '{sha,verified:.commit.verification.verified,message:.commit.message}'
gh api repos/OWNER/ACTION/compare/OLD...NEW --jq '{ahead:.ahead_by,files:[.files[].filename]}'
gh api repos/OWNER/ACTION/git/trees/SHA?recursive=1 --jq '.tree[] | select(.path|test("^(dist/|action.yml|src/)"))'

# Registry hashes
curl -sS "https://pypi.org/pypi/PKG/VER/json" | jq -r '.urls[].digests.sha256'
curl -sS "https://registry.npmjs.org/PKG/VER" | jq '{version,integrity:.dist.integrity}'

# Advisories
gh api "/advisories?ecosystem=ECOSYSTEM&affects=PKG"
```

Use `gh`, the registry, and the upstream compare. Do not infer safety from
the dashboard or a local default.

## Merge and Dependabot control

Squash only. Conventional subject, then `(#<n>)`:

```bash
gh pr review <n> --approve --body "<evidence>"
gh pr merge <n> --repo ditto-assistant/ditto-subnet --squash \
  --subject "chore(deps): <imperative summary> (#<n>)"
```

These PRs are not `gh stack` layers. Do not use `merge-async` unless the PR
is actually in a GitHub stack.

On a block, comment with the reason **and** the narrowest Dependabot command:

| Intent | Command |
|---|---|
| Stay on current major (Docker 28.x) | `@dependabot ignore this major version` |
| Stay on current minor (Python 3.12 vs 3.14, Prettier 3.8 vs 3.9) | `@dependabot ignore this minor version` |
| Stop tracking this package in this directory | `@dependabot ignore this dependency` |
| Close without ignore (will reopen) | `@dependabot close` |
| After an earlier merge lands | `@dependabot rebase` |

`ignore this major version` on a `3.12` → `3.14` Docker tag is wrong: both
are major 3, and it can suppress wanted 3.12 digest updates. Use **minor**.

Do not `@dependabot merge`. If the review is an approve, squash-merge it
yourself after the user authorized merges.
