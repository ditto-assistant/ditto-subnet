---
name: github
description: Manage ditto-subnet monorepo issues, branches, worktrees, single or stacked pull requests, current-main resyncs, conflicts, descriptions, checks, and reviews with noninteractive gh and gh stack. Use for any PR publication or stack maintenance in the monorepo; pair with ditto-subnet-worktree instead of multi-repo-temp-clone.
---

# GitHub for ditto-subnet

Use GitHub Stacked Pull Requests for every change, including one-branch work. Never merge unless the user separately authorizes it.

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

New PRs remain drafts unless the user requests review-ready publication. Stage explicit paths; never use `git add -A` in a shared or pre-existing worktree.

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

Resolve only reported conflicts, stage only resolved files, and run `gh stack rebase --continue`. Abort when the semantic resolution is unclear. Do not invoke the interactive `gh stack modify` TUI.

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

Use a valid semantic-release type (`feat:`, `fix:`, `chore:`, and repository-supported scopes). Test-only changes use `chore(tests):`, never `test:`.

## Final verification

```bash
git status --short --branch
gh stack view --json
gh pr checks <number>
gh pr view <number> --json headRefOid,baseRefName,mergeable,mergeStateStatus,isDraft,url
```

Report exact PR URLs, heads, check state, worktree path, and any deployment or activation gap. Do not call historical checks current after a rebase or force-push.
