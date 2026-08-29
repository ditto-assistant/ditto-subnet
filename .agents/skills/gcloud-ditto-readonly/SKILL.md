---
name: gcloud-ditto-readonly
description: Safely read SN118 production Platform Postgres, Platform API pm2 logs, GCE screener-worker journals, Cloud Run screening-job logs, Targon rental logs/state, and Platform app-VM disk inventory via gcloud. Use for prod DB lookups, counts, audits, EXPLAIN ANALYZE, API 500 tracebacks, screener fleet bootstrap or stuck-worker diagnosis, Cloud Run build/smoke/source-review job failures, Targon rental logs, Kaniko/builder logs, wrk- workloads, Targon API state, live screening-build diagnosis, or a host disk-full during platform-deploy. Never prints credentials.
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

JSON `null` is not SQL NULL: `col IS NOT NULL` is true for a stored JSON
`null`, so presence checks on jsonb payloads (for example
`observation->'review_audit'`) must compare against `'null'::jsonb` or the
"present" branch silently includes empty values.

## Platform API logs

The API runs under pm2 as `ditto-api` (user `deploy`). Live logs are
`/opt/ditto-subnet/apps/platform/logs/`; `/opt/ditto-platform/logs/` is the
pre-cutover tree and is stale — check file mtimes before trusting either, and
re-resolve with `pm2 describe ditto-api` if the layout has moved again.
`ditto-api.err.log` is empty by design: access lines, app logging, and
unhandled-exception tracebacks (`error_envelope ... unhandled exception in
request handler`) all land in `ditto-api.out.log`, which is multi-GB and
unrotated — never cat it; use the bounded script.

```bash
.agents/skills/gcloud-ditto-readonly/scripts/read_platform_logs.sh tail 200
.agents/skills/gcloud-ditto-readonly/scripts/read_platform_logs.sh grep 'submission-source-reviews/.*complete' 3 120
.agents/skills/gcloud-ditto-readonly/scripts/read_platform_logs.sh grep 'unhandled exception' 10 200
.agents/skills/gcloud-ditto-readonly/scripts/read_platform_logs.sh --file relay-1 tail 100
```

A request line ending `-> ERR in Nms` is immediately followed by the
traceback. VM app logs do NOT ship to Cloud Logging; `gcloud logging read`
on `resource.type="gce_instance"` finds nothing.

## GCE screener workers

Production screening workers are ephemeral instances labeled
`env=prod,role=screener-fleet`. Discover the current names and zones first;
never assume a previous instance still exists. The bounded helper opens no
free-form shell and reads only the bounded worker logfile or
`ditto-screener` systemd journal through IAP SSH.

```bash
.agents/skills/gcloud-ditto-readonly/scripts/read_screener_worker_logs.sh list
.agents/skills/gcloud-ditto-readonly/scripts/read_screener_worker_logs.sh logs ditto-screener-fleet-ab12 500
.agents/skills/gcloud-ditto-readonly/scripts/read_screener_worker_logs.sh journal ditto-screener-fleet-ab12 15 500
```

Correlate worker journal evidence with Backroom `get_screener_capacity` and the
exact screening attempt. A RUNNING VM or successful startup script is not a
healthy worker: require a current Platform heartbeat, a stable GCE target, and
an actual lease claim or polling loop. Keep reads bounded to 1-1440 minutes and
1-2000 lines. Do not run arbitrary SSH commands, restart services, edit the VM,
or read environment/credential files from this skill.

## Cloud Run screening job logs

Cloud Run screening lanes (GCP fallback of the Targon-first stack) log only
to Cloud Logging, and Platform's `replica_logs` stub returns `""` for Cloud
Run, so the DB replica-trace columns are empty for gcp rows — Cloud Logging
is the only artifact. The rental loop deletes failed jobs after capture, so
`gcloud run jobs describe` usually 404s.

Job names (from the launching Platform loop): Kaniko builds are
`ditto-miner-build-<build_id hex[:12]>`, source reviews are
`ditto-source-<review_id hex[:16]>`, runtime smokes are short-lived internal
Services. Worker stdout/stderr is in `run.googleapis.com%2F{stdout,stderr}`;
`Container called exit(N)` lands in `run.googleapis.com%2Fvarlog%2Fsystem`
(builder stage exit codes 71-76 mean SOURCE/KANIKO/ARCHIVE/UPLOAD/COMPLETE/
CONTRACT); execution-level failures in `cloudaudit.googleapis.com%2Fsystem_event`.

```bash
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name=~"ditto-source-" AND severity>=WARNING' \
  --project=ditto-app-dev --freshness=4h --limit=30 \
  --format='value(timestamp, resource.labels.job_name, textPayload)'
gcloud logging read 'resource.type="cloud_run_job" AND resource.labels.job_name="ditto-miner-build-<build12>"' \
  --project=ditto-app-dev --freshness=6h --limit=25 \
  --format='value(timestamp, logName, severity, textPayload)'
```

Resolve the name suffix from the Platform row (`build_id` /
`review_id`), not from `provider_resource_id` (often cleared after release).

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
