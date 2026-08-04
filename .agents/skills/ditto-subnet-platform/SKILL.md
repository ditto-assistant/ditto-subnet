---
name: ditto-subnet-platform
description: Implement or diagnose subnet Platform API, PostgreSQL/Alembic, scheduling and lease control, Platform dashboard, and public Backroom changes inside the ditto-subnet monorepo. Use for API or wire-model changes, database migrations, admin endpoints, OAuth, operator UI, dashboard behavior, generated OpenAPI clients, or any change where Platform API consumers must move atomically.
---

# Ditto Subnet Platform

Keep Platform, generated contracts, dashboard consumers, and Backroom consumers in one monorepo stack.

## Orient

Run the context lookup first:

```bash
python3 .agents/skills/ditto-subnet-context/scripts/lookup-context.py \
  --max-topics 3 "$ARGUMENTS"
```

Pass the user's task text verbatim. If there is no task text, omit the query so
the lookup returns the monorepo overview instead of manufacturing broad owners.

Read [`references/platform-index.md`](references/platform-index.md), then only the returned source anchors and nearest guidance.

## Change workflow

1. Decide which owner changes: API/database, dashboard, Backroom, or a shared contract.
2. Trace the authoritative producer before editing consumers. Do not infer API semantics from rendered UI.
3. For schema changes, fetch current `main`, prove one Alembic head, use safe hot-table helpers, and test the merge result.
4. For API contract changes, update every in-tree consumer and regenerate Backroom types with `apps/backroom/scripts/platform-contract/generate.sh`.
5. Preserve release selection: Platform API changes affect Backroom; dashboard-only changes do not.
6. Run focused tests first, then each affected component's full gate.

## Safety

- Use the production DB only through the read-only GCP skill. Never mutate production while diagnosing.
- Keep Cloudflare OAuth, sessions, administrator lists, and Platform admin tokens server-side.
- Never revive repository dispatch or bot contract-sync PRs. Same-repository generated artifacts are ordinary reviewed changes.
- Distinguish an exact source SHA, green CI, merge, release, deployment, migration success, and browser-visible behavior.
