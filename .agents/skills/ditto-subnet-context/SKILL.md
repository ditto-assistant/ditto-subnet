---
name: ditto-subnet-context
description: Route any substantial ditto-subnet monorepo task to the owning components, contracts, invariants, source anchors, searches, and validation commands. Use at the start of a new chat or whenever work spans or could affect validator/miner code, Platform API or dashboard, Backroom, screeners, DittoBench, runtime profiling, research adapters, releases, deployments, GCP, Cloudflare, or Terraform.
---

# Ditto Subnet Context

Build task-specific context from the repository instead of loading a static handbook.

## Start

From the repository root, run:

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py "$ARGUMENTS"
```

Read only the returned anchors. Run the returned `rg` searches before broad exploration. Load a specialized skill when the result names one.

Useful modes:

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py --list
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py --check
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py --json "platform api backroom"
```

The curated index is [`references/context-index.json`](references/context-index.json). Update it whenever ownership, canonical paths, or validation commands move.

## Establish current truth

1. Read `AGENTS.md` and the nearest nested guidance.
2. Inspect `git status --short --branch`, `git worktree list`, and the relevant `gh stack view --json`.
3. Fetch before comparing `main`, release tags, PR heads, or imported `UPSTREAM.md` revisions.
4. Treat source, CI, merge, release, deployment, and live runtime as separate evidence.
5. Use a monorepo worktree through `$ditto-subnet-worktree`; do not create sibling-repository temp clones for components already present here.

## Cross-surface rule

Make one atomic monorepo change when a contract spans components. Keep migrations, generated contracts, callers, tests, release selection, and operational docs in the same stack. Do not recreate repository-dispatch or version-bump PR automation between directories.

## Specialized context

- Platform API, database, dashboard, or Backroom: `$ditto-subnet-platform`
- Validator, scoring, DittoBench, datagen, adapters, or protocol: `$ditto-subnet-benchmark`
- Python py-spy, Go pprof, live hot spots, or performance comparisons: `$ditto-subnet-runtime-profiling`
- Semantic release, deployments, screeners, Targon/GCE, GCP, Cloudflare, Terraform, or Ansible: `$ditto-subnet-release-ops`
- Branches, PR stacks, or current-main reconciliation: `$github`
- Quarantine triage, high-score ATH review, or precedent search: `$backroom-review`
- Isolated preview stacks, Foundry cheatcodes, localstack scoring: `$ditto-subnet-preview`

Keep conclusions live and evidence-backed. `UPSTREAM.md` records migration provenance; after cutover it is not an instruction to sync legacy repositories.
