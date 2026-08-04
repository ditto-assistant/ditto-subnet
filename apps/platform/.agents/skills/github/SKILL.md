---
name: github
description: Manage Platform-related branches and pull-request stacks as part of the ditto-subnet monorepo. Use for GitHub issue, branch, rebase, current-main sync, review, check, or PR work started from apps/platform; route all stack operations through the repository-root GitHub and worktree skills.
---

# GitHub from Platform

Read and follow the canonical monorepo skill at
`../../../../../.agents/skills/github/SKILL.md`.

Platform is a directory in `ditto-subnet`, not a separate development repo.
Use one monorepo worktree and change in-tree consumers atomically. Do not use
multi-repository temp clones, repository dispatch, or contract-sync bot PRs.
