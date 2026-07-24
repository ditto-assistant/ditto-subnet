---
name: github
description: Manage Ditto GitHub issues, branches, single and stacked pull requests, descriptions, reviews, team routing, repo discovery, and non-interactive gh and gh-stack CLI workflows. Use for creating, committing, navigating, rebasing, syncing, submitting, or repairing GitHub PR stacks, especially with multi-repo-temp-clone.
---

# GitHub (Ditto Assistant)

Unified skill for issue management, PR publication, stacked branches, descriptions, and team routing across the `ditto-assistant` GitHub org.

**User Input**: $ARGUMENTS

---

## Intent Routing

| Input Pattern | Workflow |
|---|---|
| "create issue", "new issue", file paths with description, raw problem statement | **Create Issue** |
| Issue number, URL, "work on", or empty (on non-PR branch) | **Work on Issue** |
| "pr description", "pr body", or empty on a branch with an open PR | **PR Description** |
| "review", "review this PR", PR URL | **Review PR** (see [`code-review`](../code-review) if available) |
| "commit", "submit", "stack", "dependent PRs", branch creation or repair | **Branches and PR Stacks** |

If ambiguous, default to **PR Description** when on a branch with an open PR, else **Create Issue**.

---

## Org & Repo Map

**Org**: `ditto-assistant`

| Repo | Purpose | Stack |
|---|---|---|
| `ditto-app` | Main web/mobile app (React 19, Capacitor) | TypeScript, Bun, Firebase hosting |
| `backend` | API server + LLM orchestration + Stripe + encryption | Go 1.24, just, Cloud Run, Turso/PostgreSQL, sqlc |
| `landing-astro` | Marketing site, docs, blog (heyditto.ai) | Astro, content-driven |
| `internal-docs` | Private team docs (product, runbooks, meeting notes) | Markdown |
| `ditto-mcp` | Ditto's MCP server (memory + tools) | TypeScript |
| `ditto-cli` | CLI for Ditto operations | — |
| `ditto-code` | Code-mode workspace | — |
| `DittoMobile` | Native mobile shell | — |
| `ditto-internal-skills` | This repo — private Claude Code skills | Markdown + shell |

When a task spans **both** `ditto-app` and `backend` (most feature work does), prefer the [`multi-repo-temp-clone`](../multi-repo-temp-clone/SKILL.md) skill to set up a clean cross-repo workspace.

---

## Team Directory

| Name | GitHub | Surfaces |
|---|---|---|
| Peyton Spencer | `Peyton-Spencer` | Founder. Full-stack — backend, ditto-app, MCP, simulator, landing-astro |
| Omar Barazanji | `omarzanji` | Co-developer. Backend + ditto-app. **CODEOWNER on both repos** |
| Alan G | `AlanGOmniAura` | Full-stack contributor — backend + ditto-app |
| Nick Allen | `nicktallen` | Mobile (Capacitor / Android Play Store) + docs/content/UI |

**CODEOWNERS** on `ditto-app` and `backend`: `* @peyton-spencer @omarzanji` — both will get auto-requested on every PR.

### Routing heuristics

| Topic | First reviewer |
|---|---|
| LLM providers, agents, MCP, retrieval, encryption | Peyton |
| Stripe, billing, subscriptions, accounts | Peyton |
| Mobile (Capacitor, Android, iOS) | Nick |
| Landing pages, marketing copy, docs site | Nick |
| Artifact engine, document editing, file storage | Omar |
| DevOps, deploys, infra emergencies | Peyton |

After assigning, offer to notify them via the [`slack`](../slack/SKILL.md) skill.

---

## Create Issue

### Input Discovery

Input may be file paths, a direct description, or empty. If empty, ask what the issue should be about. **Wait for input.**

### Context Gathering

1. Check existing issues: `gh issue list --repo ditto-assistant/<repo> --search "<keyword>" --state all`
2. Search the repo for existing implementations near the area of concern
3. If the topic might already be in Ditto memory, use [`ditto-memories`](../ditto-memories/SKILL.md) to surface related context

### Clarifying Questions

Ask only the blockingly important ones (skip when crystal clear):

- Which repo? (`ditto-app` / `backend` / `landing-astro` / cross-repo)
- Scope & acceptance criteria
- Priority (P0/P1/P2)
- Should this be `fast-track`? (see [`fast-track`](../fast-track/SKILL.md))
- Assignee

### Drafting

**Title**: Imperative mood, ≤72 chars. No prefix needed on issues (we reserve prefixes for PR titles).

**Body**:

```markdown
## Summary
[1-3 sentences: what and why]

## Background
[Context, motivation, constraints]

## Proposed Solution
[Clear description — link code with file_path:line_number when possible]

## Acceptance Criteria
- [ ] [Specific requirement]
```

### Creation

```bash
gh issue create \
  --repo ditto-assistant/<repo> \
  --title "<title>" \
  --body-file /tmp/issue-body.md \
  --assignee @me
```

Add labels with repeated `--label` flags. After creation, offer to notify the assignee via the [`slack`](../slack/SKILL.md) skill.

---

## Work on Issue

### Discovery

Parse user input for an issue number (`599`, `#599`, or a GitHub URL). If none provided, list assigned issues across both main repos:

```bash
gh issue list --repo ditto-assistant/ditto-app --assignee "@me" --state open --limit 10 \
  --json number,title,labels,createdAt,updatedAt
gh issue list --repo ditto-assistant/backend  --assignee "@me" --state open --limit 10 \
  --json number,title,labels,createdAt,updatedAt
```

Present and **wait for selection**.

### Analysis

1. Read issue body, labels, comments, linked PRs
2. Detect sparse issues (empty body, no acceptance criteria)
3. If the issue references a past incident or AMA, search Ditto memories — see [`ditto-memories`](../ditto-memories/SKILL.md)
4. Identify affected surfaces (see Repo Map above)

### Plan & Implementation

Scale the plan to complexity:

- **Simple** (1-2 files): bullet list
- **Medium**: numbered steps grouped by surface
- **Cross-repo**: use [`multi-repo-temp-clone`](../multi-repo-temp-clone/SKILL.md)

Implement incrementally, run tests/lint frequently, fix errors as you go.

### Completion

After implementation:
1. `git status` and show diff stat.
2. Use **Branches and PR Stacks** below when the request includes delivery or publication. Otherwise wait for the user's commit instruction.
3. Never merge unless the user separately authorizes it.

---

## Branches and PR Stacks

Use `gh stack` as Ditto's default branch and PR workflow. A one-branch change is still a stack. For multi-repo work, use [`multi-repo-temp-clone`](../multi-repo-temp-clone/SKILL.md); each repository has its own stack, and related PRs must cross-link their dependency and merge order.

GitHub Stacked PRs is currently a private preview. Exit code `9` means the repository is not enabled; report the blocker instead of silently publishing ordinary PRs.

### Prerequisites

```bash
gh auth status
gh extension list | grep -F 'gh stack'
git config rerere.enabled true
git config remote.pushDefault origin
```

Install a missing extension with `gh extension install github/gh-stack`.

### Non-interactive rules

- Always pass branch names to `init`, `add`, and `checkout`.
- Always use `gh stack submit --auto`; add `--open` only for requested review-ready publication.
- Always use `gh stack view --json`; the default opens a TUI.
- Stage only task-owned files. Never use `git add -A` in a shared or pre-existing checkout.
- Put foundational changes below dependent consumers.
- Pass `--remote origin` when multiple remotes exist.
- Treat commits, pushed heads, checks and reviews, merge state, deployment, and live runtime as separate evidence.
- Do not merge unless the user separately authorizes it. The extension does not merge stacks.

### Create and commit

```bash
git fetch origin
git switch main
git pull --ff-only origin main
gh stack init task/short-description
git add path/to/file1 path/to/file2
git diff --cached --stat
git commit -m "feat: describe the change"
```

Use `gh stack add task/dependent-layer` only when a new concern is independently reviewable. Use imperative commit subjects; semantic prefixes are optional.

### Inspect and navigate

```bash
gh stack view --json
gh stack up
gh stack down
gh stack top
gh stack bottom
gh stack checkout task/branch-name
```

When a lower layer needs changes, commit there and run `gh stack rebase --upstack`.

### Publish and sync

Fetch and inspect remote state before writing, then:

```bash
gh stack submit --auto --remote origin
gh stack view --json
```

New PRs are drafts by default. Add `--open` only for ready-for-review publication. Use `gh pr edit` after submission to set the concise Ditto body described below, then verify exact heads and checks.

Use `gh stack push --remote origin` to update branches without creating PRs. Use `gh stack sync --remote origin` for routine synchronization; add `--prune` only when merged local branches should be deleted.

### Conflict recovery

Exit code `3` means rebase conflict. Resolve only reported files, stage them, and run `gh stack rebase --continue`; use `gh stack rebase --abort` when resolution is unsafe or unclear.

If local and remote stack definitions diverge, inspect both. `sync` may abort without mutation. `gh stack unstack --local` removes only local tracking; plain `unstack` also changes the GitHub Stack object.

### Restacking and stack maintenance

Use cascading rebase for routine maintenance when `main` or a lower branch moves:

```bash
gh stack rebase --remote origin     # trunk through stack top
gh stack rebase --upstack          # current branch through stack top
gh stack rebase --no-trunk         # realign only inter-branch ancestry
```

Resolve reported conflicts, stage only resolved files, and continue with `gh stack rebase --continue`. Use `--abort` when abandoning a resolution.

`gh stack modify` is GitHub's interactive TUI for dropping, folding, inserting, reordering, or renaming layers. It stages the structural plan and applies it with `Ctrl+S`. Agents must not invoke this interactive command. Hand it to the user, or—with explicit authorization and an exact recorded branch order—rebuild local tracking using `gh stack unstack --local` and `gh stack init --base <trunk> <branches...>`. Run `gh stack submit --auto` afterward to update PR bases and the GitHub Stack object.

For backend stacks containing Goose migrations, rebase onto current `origin/main` before renumbering. Every branch-new `YYYYMMDDHHMMSS_*.sql` version must be unique and strictly above main's newest migration version or an already-migrated database can silently skip it. Rename on the owning branch with `git mv`, preserve relative order, rebase descendants once, then run:

```bash
./scripts/migrate-check.sh
go build ./...
go test ./pkg/database/migrations/...
```

Prefer a repository-provided deterministic stack renumber script when available. It must fail closed on a dirty tree, unproven branch ownership, or a stack not rebased onto current `origin/main`; it must never edit migration bodies or bypass commit hooks.

Exit codes: `2` not in a stack; `3` conflict; `4` API failure; `5` invalid arguments; `6` ambiguous branch; `7` rebase active; `8` stack lock; `9` Stacked PRs unavailable.

---

## PR Description

### Find PR

```bash
gh pr view --json number,title,state,headRefName,baseRefName
```

If no PR exists and publication is in scope, use **Branches and PR Stacks** above.

### Analyze Commits and Changes

```bash
git log origin/main..HEAD --oneline
git diff --stat origin/main..HEAD
```

**Never read full diffs** — use `--stat` only.

### Generate Title

PR titles in Ditto repos use **semantic prefixes loosely**. Use a prefix when it's a clean fit; omit when the change is hard to categorize. Looking at recent merges:

| Prefix | When | Examples from history |
|---|---|---|
| `fix:` | Bug fix, behavior correction | `fix: autoscroll Settings → Models list (file-browser pattern)` |
| `feat:` / `feat(scope):` | New feature or capability | `feat: add About tab to settings with version and release notes` |
| `chore:` | Maintenance, deps, infra cleanup | (any non-user-visible change) |
| `[codex]` prefix | Fix or update auto-generated by Codex agent | `[codex] fix Cloudflare PWA chunk refresh` |
| _no prefix_ | Bigger or non-categorizable changes | `Improve response validation logging`, `Render --- as horizontal rules in doc editor` |

Imperative mood, lowercase after prefix, no trailing period. <72 chars total.

### Generate Description

Ditto repos do NOT have a PR template. Keep descriptions short and useful:

```markdown
## Summary
<2-3 sentences>

## Changes
- <bullet 1>
- <bullet 2>

## Test plan
- [ ] <how to verify locally>
- [ ] <regression to watch>

Fixes #<issue-number>   <!-- if applicable -->
```

For `fix:` PRs, lead with the **observable bug** in the Summary, not the code change.

### Set Description

```bash
gh pr edit <NUMBER> --body "$(cat <<'EOF'
## Summary
…
EOF
)"
```

Confirm with link to PR.

---

## Mark Generated Files as Viewed

Backend PRs that touch sqlc-generated code can be noisy. Mark generated files as viewed to reduce review fatigue:

```bash
PR=<number>
gh api repos/ditto-assistant/backend/pulls/$PR/files --paginate \
  --jq '.[] | select(.filename | test("(_sqlc\\.go|\\.templ\\.go|pkg/database/turso/.*\\.go)$")) | .filename' \
  | while read -r f; do
      gh api --method PUT "repos/ditto-assistant/backend/pulls/$PR/files/$(printf '%s' "$f" | jq -sRr @uri)/viewed" >/dev/null
    done
```

---

## Resolve PR Review Comments

After addressing feedback, resolve the corresponding review threads.

### List unresolved threads

```bash
PR=<number>
REPO=ditto-app   # or backend
gh api graphql -f query="query {
  repository(owner: \"ditto-assistant\", name: \"$REPO\") {
    pullRequest(number: $PR) {
      reviewThreads(first: 50) {
        nodes { id isResolved comments(first: 1) { nodes { body } } }
      }
    }
  }
}" --jq '.data.repository.pullRequest.reviewThreads.nodes[] | select(.isResolved == false) | .id + " | " + (.comments.nodes[0].body | split("\n")[0])'
```

### Resolve

```bash
gh api graphql -f query='mutation { resolveReviewThread(input: {threadId: "<THREAD_ID>"}) { thread { id isResolved } } }'
```

For the full background-agent flow that fetches comments, applies fixes, resubmits, and resolves threads in one shot, use the [`handle-review-comments`](../handle-review-comments/SKILL.md) skill.

---

## Notes

- Always use `--jq` on `gh` API calls to minimize tokens
- For large PRs, GitHub's diff endpoint won't return everything — use local `git` instead
- `fast-track` label is a real label on `backend` — see [`fast-track`](../fast-track/SKILL.md) for when to use it
- Don't list sub-issues as checkboxes in parent body — GitHub renders them automatically
