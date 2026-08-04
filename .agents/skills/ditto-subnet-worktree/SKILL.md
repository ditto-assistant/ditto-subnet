---
name: ditto-subnet-worktree
description: Create and operate isolated git worktrees for ditto-subnet monorepo tasks, including new branches, existing gh stacks, current-main resyncs, and concurrent agents. Use instead of multi-repo-temp-clone whenever every affected subnet component is already under apps, services, packages, research, infra, or the root validator/miner tree.
---

# Ditto Subnet Worktree

Use one monorepo worktree so every component shares one commit, branch, dependency graph, and PR stack.

## New task

From any clean or dirty checkout of this repository, run:

```bash
.agents/skills/ditto-subnet-worktree/scripts/create-worktree.sh <short-slug>
```

The script creates `agent/<short-slug>` from fetched `origin/main` under the primary checkout's sibling `worktrees/` directory. It fails closed on an existing branch or target.

Then enter the printed directory, read `AGENTS.md` plus nested guidance, run `$ditto-subnet-context`, and initialize even a one-branch change with:

```bash
gh stack init agent/<short-slug>
```

## Existing stack

Do not clone the repository again. Add a detached worktree at the remote stack top, then import the stack inside it:

```bash
git fetch origin <top-branch>
git worktree add --detach <new-path> origin/<top-branch>
cd <new-path>
gh stack checkout <top-pr-number>
gh stack view --json
```

Use `gh stack checkout <owning-branch>` before changing a lower layer and `gh stack rebase --upstack` afterward. Fetch and compare the live upstream head before resyncing imported code.

## Boundaries

- Treat the original checkout and other worktrees as read-only unless they own the task.
- Never copy `.env`, credentials, build caches, or virtual environments into a worktree.
- Stage only task-owned files. Do not clean or delete other worktrees automatically.
- Do not push, submit, merge, deploy, or apply infrastructure unless the user authorized that action.
- Report the worktree path, branch/stack, validation, exact pushed SHA, and remaining activation boundary.

Use sibling-repository work only for a legacy cleanup that cannot live in this monorepo. Prefer GitHub API reads and a single-repo worktree over coordinated temp clones.
