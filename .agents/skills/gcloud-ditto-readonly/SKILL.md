---
name: gcloud-ditto-readonly
description: Safely read SN118 production Platform Postgres, Targon rental logs/state, and Platform app-VM disk inventory via gcloud. Use for prod DB lookups, counts, audits, EXPLAIN ANALYZE, Targon rental logs, Kaniko/builder logs, wrk- workloads, Targon API state, live screening-build diagnosis, or a host disk-full during platform-deploy. Never prints credentials.
---

# Read-only SN118 production debug

Use the bundled scripts. Do not print credentials, open a free-form remote shell, or mutate production.

Pick the smallest surface that answers the question. Corroborate Targon logs with the database and the live `/health` commit. W&B remains `$wandb-ops`.

## Database

Credentials stay in `/opt/ditto-platform/.env` on the production VM. Never use `gcloud secrets versions access` for this workflow.

```bash
.agents/skills/gcloud-ditto-readonly/scripts/query_prod_db.sh 'SELECT count(*) FROM agents'
.agents/skills/gcloud-ditto-readonly/scripts/query_prod_db.sh ./query.sql
printf '%s\n' 'SELECT now()' | .agents/skills/gcloud-ditto-readonly/scripts/query_prod_db.sh -
.agents/skills/gcloud-ditto-readonly/scripts/explain_analyze_prod_db.sh 'SELECT agent_id FROM agents WHERE status = '\''evaluating'\'' ORDER BY created_at LIMIT 50'
```

1. Refuse writes, DDL, permission changes, and maintenance commands.
2. Inspect `ditto/db/models.py` and Alembic when names are uncertain.
3. Select only needed columns, filter by exact identifiers, `LIMIT` row dumps, aggregate for counts.
4. Report facts vs deployed code vs live Targon state separately. Redact tokens, passwords, private object URLs, and full artifacts.

Target is project `ditto-app-dev`, zone `us-central1-a`, instance `ditto-platform-prod`, env `/opt/ditto-platform/.env`. Connect through IAP. Default statement timeout 30s; `DITTO_DB_STATEMENT_TIMEOUT_MS` at most 120000.

## Targon logs

Stream `TARGON_API_KEY` only through `scripts/query_targon.sh` into `targon_cli --api-key-stdin`. Never run Secret Manager access outside that wrapper, export the key, put it on argv, or ask the user to paste it. Do not use the VM `.env` for this key.

```bash
.agents/skills/gcloud-ditto-readonly/scripts/query_targon.sh state wrk-xxxxxxxxxxxxxxxx
.agents/skills/gcloud-ditto-readonly/scripts/query_targon.sh logs wrk-xxxxxxxxxxxxxxxx --tail 400 --include-state
.agents/skills/gcloud-ditto-readonly/scripts/query_targon.sh list
```

Read-only `logs` / `state` / `list` only. Stop for creates, deploys, probes, suspends, or deletes. Resolve `wrk-` uids from Platform `resource_id` or `list`. Fetch logs while the replica is `running`; after `error` or delete, `GET .../logs` often 404s. Targon `exit code 2` after a successful complete is often teardown, not ARCHIVE. Kaniko compiles are long; start `--tail` at 400. `TARGON_TIMEOUT_SECONDS` at most 120. Org slug `ditto`.

## Host disk

Read-only inventory when `platform-deploy` fails with `No space left on device`
during `git fetch`. `/tmp` is tmpfs and does not free `/`.

```bash
.agents/skills/gcloud-ditto-readonly/scripts/inspect_platform_disk.sh
```

Cache reclaim and boot-disk growth are `$ditto-subnet-release-ops`
([`platform-host-disk.md`](../ditto-subnet-release-ops/references/platform-host-disk.md)).
Do not truncate live logs, delete relay traces, or resize the disk from this
skill.

## Failures

If gcloud/SSH/Secret Manager auth fails, report the non-secret error and suggest `gcloud auth login`. Do not hunt another credential store or print `.env`. If a DB statement is rejected, keep it as `SELECT`/`WITH`/`TABLE`/`VALUES`/`EXPLAIN`/`SHOW`. If Postgres reports a read-only violation, stop.
