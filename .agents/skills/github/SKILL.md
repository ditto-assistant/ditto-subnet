---
name: github
description: Manage ditto-subnet monorepo issues, branches, worktrees, single or stacked pull requests, current-main resyncs, conflicts, descriptions, checks, and reviews with noninteractive gh and gh stack. Use for any PR publication or stack maintenance in the monorepo; pair with ditto-subnet-worktree instead of multi-repo-temp-clone.
---

# GitHub for ditto-subnet

Use GitHub Stacked Pull Requests for every change, including one-branch work. Never merge unless the user separately authorizes it.

This skill is owned in this repository. Do not refresh it from `ditto-internal-skills` or other product repos.

## Prepare

Use `$ditto-subnet-worktree` for isolation and `$ditto-subnet-context` for component ownership. Then verify:

```bash
gh auth status
gh extension list | rg '^gh stack'
git status --short --branch
git config rerere.enabled true
git config remote.pushDefault origin
```

Always fetch before current-state claims. Treat local commit, pushed head, checks, review, merge, release, deployment, and live runtime as separate evidence.

Install a missing extension with `gh extension install github/gh-stack`. Exit code `9` means Stacked PRs are unavailable; report that instead of publishing ordinary PRs.

## Non-interactive rules

- Always pass branch names to `init`, `add`, and `checkout`.
- Always use `gh stack submit --auto`; add `--open` only for requested review-ready publication.
- Always use `gh stack view --json`; the default opens a TUI.
- Stage only task-owned files. Never use `git add -A` in a shared or pre-existing checkout.
- Put foundational changes below dependent consumers.
- Pass `--remote origin` when multiple remotes exist.
- Do not invoke the interactive `gh stack modify` TUI.

## Issues

Create and list against `ditto-assistant/ditto-subnet` only.

```bash
gh issue list --repo ditto-assistant/ditto-subnet --assignee "@me" --state open --limit 10 \
  --json number,title,labels,createdAt,updatedAt
```

```bash
gh issue create \
  --repo ditto-assistant/ditto-subnet \
  --title "<imperative title>" \
  --body-file /tmp/issue-body.md \
  --assignee @me
```

Issue titles are imperative and unprefixed. Reserve semantic prefixes for commits and PRs.

## New stack

```bash
git fetch origin main
gh stack init <branch-name>
git add <task-owned-paths>
git diff --cached --stat
git commit -m '<semantic subject>'
gh stack submit --auto --remote origin
gh stack view --json
```

New PRs remain drafts unless the user requests review-ready publication. Use `gh stack add <dependent-branch>` only when the new concern is independently reviewable.

`$ditto-subnet-worktree` creates `agent/<slug>` already tracking `origin/main`.
Run `gh stack init agent/<slug>` from that branch. If `gh stack submit` then
reports no commits, `@{upstream}` is still `origin/main` — `git status -sb`
showing `ahead N` is a real diff. Push the named branch and open a normal PR:

```bash
git push -u origin HEAD:<branch>
gh pr create --base main --head <branch> --title "..." --body-file /tmp/pr-body.md
```

## Existing stack

```bash
gh stack checkout <pr-number-or-branch>
gh stack view --json
```

When `main` moves:

```bash
gh stack rebase --remote origin
```

When a lower layer changes:

```bash
gh stack checkout <owning-branch>
# commit the focused change
gh stack rebase --upstack
gh stack submit --auto --remote origin
```

Resolve only reported conflicts, stage only resolved files, and run `gh stack rebase --continue`. Abort when the semantic resolution is unclear.

Use `gh stack push --remote origin` to update branches without creating PRs. Use `gh stack sync --remote origin` for routine synchronization; add `--prune` only when merged local branches should be deleted.

If local and remote stack definitions diverge, inspect both. `sync` may abort without mutation. `gh stack unstack --local` removes only local tracking; plain `unstack` also changes the GitHub Stack object.

Exit codes: `2` not in a stack; `3` conflict; `4` API failure; `5` invalid arguments; `6` ambiguous branch; `7` rebase active; `8` stack lock; `9` Stacked PRs unavailable.

## Merge a stack

Only after the user separately authorizes the merge. `gh stack` has no merge command, and a GitHub-linked stack rejects both `gh pr merge` and the synchronous REST merge. Use the async endpoint:

```bash
gh api -X PUT repos/ditto-assistant/ditto-subnet/pulls/<number>/merge-async \
  -f merge_method=squash \
  -f merge_action=default \
  -f commit_title='<conventional squash subject> (#<number>)'
```

It enqueues and returns `{"status":"pending",...}`. Poll `gh pr view <number> --json state` until `MERGED`. It fails closed on drafts — mark the layer ready first (`gh pr ready <number>`). The squash subject comes from `commit_title` (else the PR title) and must be a valid semantic-release type.

A stack merge is cascading: `merge-async` on any layer merges every unmerged PR up to and including that one. Prefer marking every layer ready, then issuing a single `merge-async` on the top PR.

To land only lower layers, merge the bottom PR, then `gh stack sync --remote origin`. Sync re-drafts upper PRs, so `gh pr ready` again before the next `merge-async`.

## Monorepo layering

- Put shared protocols and imported source refreshes below consumers.
- Put runtime implementation below release/deployment wiring.
- Keep generated contracts with the source change that requires them.
- Add agent tooling as an independent top layer unless production code depends on it.
- Do not open cross-repository version-bump or contract-sync PRs for code already in this repository.

Legacy repositories may receive small retirement PRs. Use a single-repository worktree or GitHub API, cross-link the monorepo cutover, and do not use coordinated temp clones.

## PR body

Keep it short:

```markdown
## Summary
- behavior and ownership change
- important safety or compatibility boundary

## Validation
- exact commands and results

## Activation
- merge, deploy, Terraform, or operator steps still required
```

Use a valid semantic-release type (`feat:`, `fix:`, `chore:`, `docs:`, `perf:`, and repository-supported scopes). `docs:` bumps like `chore:` (no release); `perf:` bumps like `fix:` (patch). Test-only changes use `chore(tests):`, never `test:`. Imperative mood, lowercase after the prefix, no trailing period, ≤72 characters.

After `gh stack submit --auto`, set the body with `gh pr edit` and verify exact heads and checks.

## Review threads

```bash
PR=<number>
gh api graphql -f query="query {
  repository(owner: \"ditto-assistant\", name: \"ditto-subnet\") {
    pullRequest(number: $PR) {
      reviewThreads(first: 50) {
        nodes { id isResolved comments(first: 1) { nodes { body } } }
      }
    }
  }
}" --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .id + " | " + (.comments.nodes[0].body | split("\n")[0])'
```

```bash
gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "<THREAD_ID>"}) { thread { id isResolved } } }'
```

## Final verification

```bash
git status --short --branch
gh stack view --json
gh pr checks <number>
gh pr view <number> --json headRefOid,baseRefName,mergeable,mergeStateStatus,isDraft,url
```

Report exact PR URLs, heads, check state, worktree path, and any deployment or activation gap. Do not call historical checks current after a rebase or force-push.
