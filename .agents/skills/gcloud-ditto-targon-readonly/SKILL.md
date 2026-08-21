---
name: gcloud-ditto-targon-readonly
description: Safely read Targon rental logs and workload state for SN118 diagnosis using the TARGON_API_KEY stored in GCP Secret Manager. Use when the user asks for Targon rental logs, Kaniko/builder logs, a wrk- workload, Targon API state, or live screening-build failure evidence. Streams the key from gcloud; never prints it.
---

# Read Targon rental logs

Use the bundled script for every production Targon log or state read. Do not export `TARGON_API_KEY`, pass it on a command line, print Secret Manager output, or call probe/create/delete commands through this workflow.

## Workflow

1. Confirm the question is answerable with a read-only Targon API call (`logs`, `state`, or `list`). Stop for requested creates, deploys, probes, suspends, or deletes.
2. Identify the workload uid (`wrk-...`) from Platform `resource_id`, Backroom, or `list`. Do not guess a uid from a miner name alone when `list` can resolve it.
3. Run the smallest command that answers the question:

   ```bash
   .agents/skills/gcloud-ditto-targon-readonly/scripts/query_targon.sh \
     state wrk-xxxxxxxxxxxxxxxx
   .agents/skills/gcloud-ditto-targon-readonly/scripts/query_targon.sh \
     logs wrk-xxxxxxxxxxxxxxxx --tail 400 --include-state
   .agents/skills/gcloud-ditto-targon-readonly/scripts/query_targon.sh list
   ```

4. Prefer `--tail` large enough to cover the failure (Kaniko compiles are long; start at 400 and raise toward 4000). Corroborate provider logs with the production database and deployed builder identity; Targon status `running` is not proof that Kaniko finished.
5. Report uid, status, timestamps, and the relevant log tail. Redact tokens, passwords, private object URLs, and Secret Manager values. Distinguish Targon logs from Platform row state.

## Guardrails

- Treat this workflow as read-only even if the user has broader credentials.
- Stream `TARGON_API_KEY` from `gcloud secrets versions access latest --project=ditto-app-dev --secret=TARGON_API_KEY` into `targon_cli --api-key-stdin`. Never assign the key to a shell variable, write it to a file, or ask the user to paste it.
- Do not use `/opt/ditto-platform/.env` for this key. That file is the database workflow; Targon uses Secret Manager.
- Do not invoke `targon-smoke.sh` probes, `sweep-oneshots --apply`, or other mutating CLI commands from this skill.
- Default API timeout is 60 seconds. Set `TARGON_TIMEOUT_SECONDS` only to a positive number no greater than 120.
- Production org slug is `ditto`. Override with `TARGON_ORG_SLUG` only when the task names another org.

## Failure handling

- If Secret Manager or gcloud auth fails, report the exact non-secret error and suggest `gcloud auth login`; do not seek the key from another store or print `.env`.
- If the uid is rejected, copy it exactly from Platform/Targon (`wrk-` plus lowercase alphanumeric). Do not pass names, image refs, or agent ids as uids.
- If logs are truncated before `DITTO_SUBMISSION_BUILD_FAILED=` or Kaniko completion, raise `--tail` and fetch again. Absence from a short tail is not proof the stage never ran.
- Fetch logs while the replica is still `running`. After `error` or delete, `GET .../logs` often 404s even if the Targon UI showed the same stream moments earlier. In that case report `state.message` and retry a still-running peer uid from `list` rather than inventing the stage from a screenshot.
