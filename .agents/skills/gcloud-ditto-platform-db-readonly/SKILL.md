---
name: gcloud-ditto-platform-db-readonly
description: Safely inspect and query the production PostgreSQL database used by ditto-assistant/ditto-platform through the GCP-hosted platform VM. Use for production DB lookups, counts, audits, incident diagnosis, schema inspection, measured EXPLAIN ANALYZE plans, or correlating platform records when the user asks to read the prod ditto-platform database with gcloud. Enforces read-only transactions and rejects SQL or psql input that could mutate data or escape to the remote shell.
---

# Read the ditto-platform production database

Use the bundled scripts for every production query. Do not retrieve the database password locally, print environment files, open a free-form remote shell, or invoke `psql` directly.

## Workflow

1. Confirm the question is answerable with read-only database access. Refuse or stop for requested writes, DDL, permission changes, maintenance commands, or destructive actions.
2. Inspect `ditto/db/models.py`, query modules, and Alembic migrations when table or column names are uncertain.
3. Write a narrow query. Select only necessary columns, filter by exact identifiers, and add a sensible `LIMIT` for row-returning exploration. Use aggregates for counts.
4. Run a normal read with one of:

   ```bash
   .agents/skills/gcloud-ditto-platform-db-readonly/scripts/query_prod_db.sh 'SELECT count(*) FROM agents'
   .agents/skills/gcloud-ditto-platform-db-readonly/scripts/query_prod_db.sh ./query.sql
   printf '%s\n' 'SELECT now()' | .agents/skills/gcloud-ditto-platform-db-readonly/scripts/query_prod_db.sh -
   ```

5. Measure a read with the dedicated wrapper:

   ```bash
   .agents/skills/gcloud-ditto-platform-db-readonly/scripts/explain_analyze_prod_db.sh 'SELECT agent_id FROM agents WHERE status = '\''evaluating'\'' ORDER BY created_at LIMIT 50'
   ```

   `EXPLAIN ANALYZE` executes the read query. Keep predicates narrow and retain the default timeout; use plain `EXPLAIN` through `query_prod_db.sh` first for queries expected to scan or aggregate a large part of production.
6. Report the query's factual result and relevant caveats. Redact tokens, passwords, private object URLs, full artifact contents, and other secrets. Distinguish a database observation from deployed code state or live service behavior.

## Guardrails

- Treat production access as read-only even if the user has broader credentials.
- Never modify the helpers to bypass validation or transaction protections during a query task.
- Never use `gcloud secrets versions access` for this workflow. Credentials stay in `/opt/ditto-platform/.env` on the production VM.
- Do not query credential/token columns unless the user explicitly needs a narrowly scoped security investigation; redact values in all output.
- Default statement timeout is 30 seconds. Set `DITTO_DB_STATEMENT_TIMEOUT_MS` only to a positive integer no greater than 120000.
- The fixed target is project `ditto-app-dev`, zone `us-central1-a`, instance `ditto-platform-prod`, runtime env `/opt/ditto-platform/.env`, and database name from that env. Connect through GCP IAP because direct port 22 is not exposed. Re-verify infrastructure source before changing any target.

## Failure handling

- If authentication or SSH fails, report the exact non-secret error and suggest `gcloud auth login` or checking the active account; do not seek alternate credentials.
- If validation rejects a legitimate read, simplify it into `SELECT`, `WITH ... SELECT`, `TABLE`, `VALUES`, `EXPLAIN`, or `SHOW` statements without psql meta-commands. Use `explain_analyze_prod_db.sh` instead of hand-writing an analyzed plan.
- If the database reports a read-only violation, stop. Do not retry outside the protected transaction.
