#!/usr/bin/env bash
#
# Scripted update for the Ditto Platform API:
#   fetch -> reset -> preflight -> uv sync -> build dashboard -> set deploy
#   config -> ensure Pylon -> migrate -> pm2 start/reload/recreate -> verify
#   the app is serving the commit that was checked out.
# NOT zero-downtime: ditto-api is a single fork-mode pm2 process, so the reload
# below is a stop/start with ~6s of refused connections (measured), not a
# rolling handover. See scripts/ecosystem.config.js.
# This script exits non-zero if the API does not come back up; see the
# verification block at the bottom. It must never report success on a dead app,
# and it must never leave the host looking deployed when it is not.
# Invoked on the host by the ditto-platform deploy workflow (push dev|main ->
# IAP SSH). DITTO_DEPLOY_BRANCH defaults to the current branch; CI passes the
# branch that was pushed so the checkout is deterministic.
#
# --------------------------------------------------------------------------
# FAILURE SEMANTICS (the 2026-07-25 near-outage)
#
# origin/main carried two alembic heads (#436 and #437 each extended the same
# parent and merged independently), so `alembic upgrade head` exited with
# `Multiple head revisions are present` and this script -- correctly -- stopped.
# What was wrong is what it stopped ON TOP OF: the new revision was already
# checked out, pm2 had not been touched, and the old process kept serving. Every
# git-layer signal said the deploy had landed. `git rev-parse HEAD` reported the
# new SHA, the process was an hour older than the checkout, and a downstream
# release used "platform #436 is live" as a precondition. Nothing was lying;
# nothing was being asked the right question either.
#
# Three rules now hold:
#
#   1. Checks that can fail run BEFORE anything on the host is mutated. The
#      single-alembic-head assertion is pure git + stdlib python, so it happens
#      before `uv sync` and long before the database is opened.
#   2. A failure before pm2 is touched rolls the checkout back to the revision
#      the RUNNING process reports. That window is exactly the window in which
#      the old build is known to still be serving, so restoring the checkout
#      restores the truth rather than inventing a new state. After pm2 has been
#      restarted the checkout is left alone: the new build is (or should be)
#      live, and reversing code under a running process would be a second lie.
#   3. The deploy does not pass until the API reports the commit that was
#      checked out. "Checked out" and "in effect" are different facts, so the
#      script asks the process, not the filesystem.
#
# Every outcome is recorded in logs/last-deploy.json (gitignored) for the next
# operator, and the previous record is this script's fallback rollback target.
#
# What the rollback does NOT do is un-apply migrations. It rewinds code, not
# schema. That is not a new requirement: this deploy is a stop/start, so the
# ordinary path already runs the new schema against the old process for the
# seconds between `alembic upgrade` and the restart. Migrations therefore have
# to be backward-compatible with the previous revision either way, and the
# rollback relies on exactly that property rather than introducing it.
#
# THE RELAY IS RELEASED SEPARATELY. The GitHub deploy workflow builds its
# wheel/lock artifact in parallel with this ordinary Platform deploy, then
# rolls ditto-api-relay-1 and -2 one at a time after this script has migrated
# and verified the API. Never reload those apps here: doing both in one PM2
# command caused the 2026-08-02 inference outage window.

set -euo pipefail
cd "$(dirname "$0")/.."
mkdir -p logs

# pm2, the deploy plan, and the JSON parsing below are all Node; the migration
# preflight is stdlib python3 (deliberately not `uv run`, so it can run before
# `uv sync`). Both are checked up front so a missing interpreter fails before
# the host is touched rather than halfway through.
command -v node >/dev/null 2>&1 || { echo "ERROR: node not found (pm2 requires it)" >&2; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "ERROR: python3 not found (migration preflight requires it)" >&2; exit 1; }

# --------------------------------------------------------------------------
# Deploy state. Read by the EXIT trap to decide whether the checkout has to be
# rolled back, and written to logs/last-deploy.json on the way out.
deploy_stage="startup"
deploy_target=""          # commit this run is trying to put into service
deploy_running_commit=""  # commit the process that is serving RIGHT NOW reports
deploy_rollback_source="" # where deploy_running_commit came from, for the log
deploy_pm2_touched=0      # 1 once pm2 has been asked to start/reload/delete
deploy_synced=0           # 1 once uv sync has rewritten .venv
deploy_state_file="logs/last-deploy.json"
health_snapshot=""

# Extract one top-level string field from a JSON document on stdin. Empty when
# the document is absent, unparseable, or lacks the field -- callers treat that
# as "cannot tell", never as a match.
json_string_field() {
  node -e '
    let raw = "";
    process.stdin.on("data", (c) => (raw += c)).on("end", () => {
      let value = "";
      try {
        const doc = JSON.parse(raw);
        if (doc && typeof doc[process.argv[1]] === "string") value = doc[process.argv[1]];
      } catch { value = ""; }
      process.stdout.write(value);
    });
  ' "$1"
}

# The API's own liveness URL. API_PORT comes from the process env if the caller
# set one, else from .env (Ansible owns that file and it exists before any
# deploy), else the documented default.
resolve_health_url() {
  local port="${API_PORT:-}"
  if [ -z "$port" ]; then
    port="$(sed -n 's|^API_PORT=||p' .env 2>/dev/null | tail -n 1)"
  fi
  printf 'http://127.0.0.1:%s/health' "${port:-8000}"
}

# The local /health URL for a given pm2 service app, or empty if it has none.
#
# Every app listed here is held to the full bar below: it must answer HTTP 200
# AND report the commit this deploy checked out. Without this mapping the
# verify loop only checked `ditto-api` by name and waved every other service
# app through on pm2 status alone -- which is exactly the defect #425 closed
# ("pm2 `online` is NOT proof of life"; pm2 reports online for a process that
# never bound its port) and would have left the relay outside the #441 commit
# assertion. Add a case here whenever a new HTTP service app joins
# scripts/ecosystem.config.js.
app_health_url_for() {
  case "$1" in
    ditto-api) resolve_health_url ;;
    *) printf '' ;;
  esac
}

# Ask the running process which commit it was started from. `/health` reports
# the revision resolved at ITS boot, which is the only trustworthy statement
# about what is actually in service. Empty when the API is not answering, is
# too old to carry the field, or reports "unknown" (no git in the checkout).
probe_running_commit() {
  local url="$1" commit
  commit="$(curl -s -m 5 "$url" 2>/dev/null | json_string_field commit || true)"
  if [ "$commit" = "unknown" ]; then
    commit=""
  fi
  printf '%s' "$commit"
}

# Last commit this script is known to have put INTO SERVICE, from its own
# record. Only used when the API cannot answer for itself. A record from a
# failed run names a revision that was never running, so it is ignored: a wrong
# rollback target is worse than none.
last_recorded_deploy_commit() {
  local result
  [ -f "$deploy_state_file" ] || return 0
  result="$(json_string_field result < "$deploy_state_file" 2>/dev/null || true)"
  [ "$result" = "ok" ] || return 0
  json_string_field target_commit < "$deploy_state_file" 2>/dev/null || true
}

record_deploy_state() {
  local result="$1" code="$2" rolled_back="$3" next_state
  next_state="$(mktemp "${deploy_state_file}.XXXXXX")" || return 0
  printf '{"finished_at":"%s","result":"%s","exit_code":%s,"stage":"%s","branch":"%s","target_commit":"%s","previous_commit":"%s","rolled_back":"%s","head_after":"%s"}\n' \
    "$(date -u +%Y-%m-%dT%H:%M:%SZ)" "$result" "$code" "$deploy_stage" \
    "${branch:-}" "$deploy_target" "$deploy_running_commit" "$rolled_back" \
    "$(git rev-parse HEAD 2>/dev/null || echo unknown)" \
    > "$next_state" 2>/dev/null || { rm -f "$next_state"; return 0; }
  mv "$next_state" "$deploy_state_file" 2>/dev/null || rm -f "$next_state"
}

# Runs on every exit, successful or not. On failure before pm2 was touched it
# restores the checkout to what is actually running, so the git layer stops
# claiming a deploy that did not happen.
on_deploy_exit() {
  local code="$1" rolled_back="no" head_now
  set +e
  trap - EXIT
  [ -n "$health_snapshot" ] && rm -f "$health_snapshot"

  if [ "$code" -eq 0 ]; then
    record_deploy_state ok "$code" "$rolled_back"
    return 0
  fi

  head_now="$(git rev-parse HEAD 2>/dev/null)"
  echo "" >&2
  echo "==> DEPLOY FAILED at stage '$deploy_stage'" >&2
  echo "    target revision:  ${deploy_target:-unknown}" >&2
  echo "    running revision: ${deploy_running_commit:-unknown}${deploy_rollback_source:+ (from $deploy_rollback_source)}" >&2

  if [ "$deploy_pm2_touched" -eq 1 ]; then
    # pm2 has already been restarted, so the process is the new build (or is
    # down and pm2 owns bringing it back). Rewinding the checkout underneath it
    # would create the inverse of the bug this guards against. Rolling code back
    # from here is a deploy of the previous revision, not a `git reset`.
    echo "    pm2 was already restarted; the checkout is LEFT AT the target revision." >&2
    echo "    To go back, deploy the previous revision -- do not reset the checkout by hand." >&2
  elif [ -n "$deploy_running_commit" ] && [ "$deploy_running_commit" != "$head_now" ]; then
    echo "    pm2 was not touched, so the old build is still serving." >&2
    echo "    Rolling the checkout back to $deploy_running_commit so the host does not" >&2
    echo "    report a deploy that never took effect." >&2
    if git reset --hard "$deploy_running_commit" >/dev/null 2>&1; then
      rolled_back="yes"
      if [ "$deploy_synced" -eq 1 ]; then
        echo "    re-syncing dependencies for the restored revision" >&2
        uv sync >/dev/null 2>&1 || \
          echo "    WARNING: uv sync failed after rollback; run 'uv sync' by hand" >&2
      fi
    else
      rolled_back="failed"
      echo "    WARNING: rollback failed. The checkout is at ${head_now:-unknown} while the" >&2
      echo "    process serves ${deploy_running_commit}. Reconcile before trusting git here." >&2
    fi
  else
    echo "    Could not determine the revision in service, so the checkout was NOT changed." >&2
    echo "    Treat ${head_now:-the checkout} as checked out but NOT proven to be running;" >&2
    echo "    confirm with: curl -s localhost:\${API_PORT:-8000}/health" >&2
  fi

  record_deploy_state failed "$code" "$rolled_back"
  exit "$code"
}
trap 'on_deploy_exit $?' EXIT

deploy_stage="fetch"
branch="${DITTO_DEPLOY_BRANCH:-$(git rev-parse --abbrev-ref HEAD)}"
echo "==> fetching + resetting to origin/$branch"
git fetch --prune origin
# -fB force-(re)points the local branch at origin and checks it out, discarding
# any host-side tracked-file drift so the deploy can't wedge. .env,
# .env.deploy, .venv, and logs are gitignored, so they survive (NEVER
# `git clean -x` here).
git checkout -fB "$branch" "origin/$branch"
git reset --hard "origin/$branch"
deploy_target="$(git rev-parse HEAD)"

# --------------------------------------------------------------------------
# Preflight. Everything here is read-only with respect to the host: it decides
# whether the deploy may proceed before `uv sync`, the database, or pm2 are
# involved.
deploy_stage="preflight"

# Establish the rollback target FIRST, while the old build is still the one
# serving. The running process's own answer wins over this script's last record,
# because the record describes what a deploy intended and /health describes what
# is actually in memory -- and those disagreeing is the whole failure mode.
deploy_running_commit="$(probe_running_commit "$(resolve_health_url)")"
if [ -n "$deploy_running_commit" ]; then
  deploy_rollback_source="/health"
else
  deploy_running_commit="$(last_recorded_deploy_commit)"
  if [ -n "$deploy_running_commit" ]; then
    deploy_rollback_source="$deploy_state_file"
  fi
fi
echo "==> deploying $deploy_target (in service: ${deploy_running_commit:-unknown})"

# Single alembic head, asserted rather than worked around.
#
# `alembic upgrade heads` (plural) would also "work" here: it applies every
# head, so a deploy never stops on divergence again. That is the wrong trade.
# Two heads mean two branches were merged that each extended the same parent
# and were never reconciled with each other -- neither was tested against the
# other's schema, and alembic picks an interleaving nobody reviewed. When they
# touch the same table, applying both silently produces a schema no branch
# intended. A stopped deploy is recoverable in minutes; a wrong schema applied
# to production under a v7 rollout is not.
#
# So the divergence still stops the deploy -- but now it stops it HERE, before
# uv sync and before the database is opened, and with the offending revisions
# and the exact `alembic merge` in the message, instead of alembic's bare
# "Multiple head revisions are present" from the middle of the sequence.
echo "==> checking migrations resolve to a single head"
if ! migration_head="$(python3 scripts/check_migration_order.py --head)"; then
  echo "ERROR: refusing to deploy $deploy_target: divergent migration history." >&2
  echo "       No migration ran, the database was not opened, and the running process" >&2
  echo "       was not touched; the checkout is restored below. See ditto-platform#440" >&2
  echo "       for the shape of the merge revision this needs." >&2
  exit 1
fi
echo "    single head: $migration_head"

deploy_stage="sync"
echo "==> syncing dependencies"
uv sync
deploy_synced=1

deploy_stage="dashboard-build"
# Build the dashboard SPA the factory serves from dashboard/dist. Ordered with
# the other pre-pm2 stages on purpose: a failed build aborts the deploy while
# the old process (and its previously built dist/, which git reset does not
# touch) is still serving, so the EXIT trap's rollback rules apply unchanged.
# A checkout without the dashboard is served API-only by the factory, so the
# build is skipped on the same condition rather than failing the deploy.
if [ -f dashboard/package.json ]; then
  echo "==> building dashboard"
  (cd dashboard && npm ci --no-audit --no-fund && npm run build)
else
  echo "==> no dashboard/package.json; skipping dashboard build"
fi

deploy_stage="deploy-config"
# Ansible is the only writer of .env. Deploy-owned values live in a separate
# mode-0600 file so a converge cannot erase them and concurrent deploy/converge
# writes cannot race on one file.
deploy_env_file=.env.deploy
touch "$deploy_env_file"
chmod 0600 "$deploy_env_file"
deploy_owned_keys=(
  DITTO_UPLOAD_PAYMENT_ADDRESS
  DITTO_DASHBOARD_WANDB_URL
  DITTO_TAOSTATS_API_KEY
  DITTO_TAOSTATS_VALIDATOR_NAMES_URL
  SUBTENSOR_ARCHIVE_RPC_API_KEY
  SUBTENSOR_ARCHIVE_RPC_AUTH_MODE
  SUBTENSOR_ARCHIVE_RPC_URL
)

# Recover deterministically from duplicate, truncated, or no-final-newline
# state. This file owns only the keys above; retain the shell-effective last
# complete assignment for each and atomically discard incomplete fragments.
normalize_deploy_env() {
  local next_env key value
  next_env="$(mktemp "${deploy_env_file}.XXXXXX")"
  for key in "${deploy_owned_keys[@]}"; do
    value="$(sed -n "s|^${key}=||p" "$deploy_env_file" 2>/dev/null | tail -n 1)"
    [ -n "$value" ] && printf '%s=%s\n' "$key" "$value" >> "$next_env"
  done
  chmod 0600 "$next_env"
  mv "$next_env" "$deploy_env_file"
}
normalize_deploy_env

# Upsert a deploy-owned KEY=VALUE, replacing an existing line or appending.
# Skips empty values so a missing deploy variable never blanks a working key.
upsert_env() {
  local key="$1" value="$2" next_env
  [ -n "$value" ] || return 0
  echo "==> setting $key from deploy env"
  next_env="$(mktemp "${deploy_env_file}.XXXXXX")"
  if grep -q "^${key}=" "$deploy_env_file" 2>/dev/null; then
    # `|` delimiter + escape any `|`/`&`/`\` in the value so URLs/addresses are safe.
    local esc=${value//\\/\\\\}; esc=${esc//|/\\|}; esc=${esc//&/\\&}
    if ! sed "s|^${key}=.*|${key}=${esc}|" "$deploy_env_file" > "$next_env"; then
      rm -f "$next_env"
      return 1
    fi
  else
    cp "$deploy_env_file" "$next_env"
    printf '%s=%s\n' "$key" "$value" >> "$next_env"
  fi
  chmod 0600 "$next_env"
  mv "$next_env" "$deploy_env_file"
}

# One-way transition for hosts that predate .env.deploy. Copy only a missing
# runtime key and preserve the shell-effective last assignment. Deploy inputs
# and fresh Secret Manager reads below remain authoritative and overwrite it.
copy_base_env_if_missing() {
  local key="$1" value
  grep -q "^${key}=" "$deploy_env_file" 2>/dev/null && return 0
  value="$(sed -n "s|^${key}=||p" .env 2>/dev/null | tail -n 1)"
  upsert_env "$key" "$value"
}

for deploy_owned_key in "${deploy_owned_keys[@]}"; do
  copy_base_env_if_missing "$deploy_owned_key"
done
unset deploy_owned_key

# Deploy-supplied values (GitHub Environment secret / variable, passed by
# deploy.yml): the upload payment address (required at boot) and the public
# wandb project URL injected into the served dashboard's telemetry link.
upsert_env DITTO_UPLOAD_PAYMENT_ADDRESS "${DITTO_UPLOAD_PAYMENT_ADDRESS:-}"
upsert_env DITTO_DASHBOARD_WANDB_URL "${DITTO_DASHBOARD_WANDB_URL:-}"
upsert_env SUBTENSOR_ARCHIVE_RPC_URL "${SUBTENSOR_ARCHIVE_RPC_URL:-}"
upsert_env SUBTENSOR_ARCHIVE_RPC_AUTH_MODE \
  "${SUBTENSOR_ARCHIVE_RPC_AUTH_MODE:-}"

# Validator-name enrichment is optional decoration. Read its API key directly
# on the VM via the attached runtime service account so the value never crosses
# GitHub Actions or SSH. A failed/slow Secret Manager lookup keeps any existing
# .env.deploy value and must not block a platform deploy.
taostats_secret_project="${DITTO_TAOSTATS_SECRET_PROJECT:-ditto-app-dev}"
taostats_secret_id="${DITTO_TAOSTATS_SECRET_ID:-platform-taostats-api-key}"
taostats_api_key=""
if command -v gcloud >/dev/null 2>&1 && \
  taostats_api_key="$(timeout 15s gcloud secrets versions access latest \
    --project="$taostats_secret_project" \
    --secret="$taostats_secret_id" 2>/dev/null)"; then
  upsert_env DITTO_TAOSTATS_API_KEY "$taostats_api_key"
  upsert_env DITTO_TAOSTATS_VALIDATOR_NAMES_URL \
    "https://api.taostats.io/api/dtao/validator/available/v1?netuid=118"
else
  echo "==> Taostats key unavailable; keeping validator-name enrichment unchanged" >&2
fi
unset taostats_api_key taostats_secret_id taostats_secret_project

# Historical payment verification prefers an operator-configured archive RPC,
# but its credential is optional. Read it on the VM so it never crosses Actions
# or SSH. If the secret has not been created (or access is temporarily down),
# preserve an existing value; with none present the client uses the free public
# archive list. A rejected/stale key also fails through to that same list.
archive_rpc_secret_project="${SUBTENSOR_ARCHIVE_RPC_SECRET_PROJECT:-ditto-app-dev}"
archive_rpc_secret_id="${SUBTENSOR_ARCHIVE_RPC_SECRET_ID:-platform-subtensor-archive-rpc-api-key}"
archive_rpc_api_key=""
if command -v gcloud >/dev/null 2>&1 && \
  archive_rpc_api_key="$(timeout 15s gcloud secrets versions access latest \
    --project="$archive_rpc_secret_project" \
    --secret="$archive_rpc_secret_id" 2>/dev/null)"; then
  upsert_env SUBTENSOR_ARCHIVE_RPC_API_KEY "$archive_rpc_api_key"
else
  echo "==> archive RPC key unavailable; free archive fallback remains enabled" >&2
fi
unset archive_rpc_api_key archive_rpc_secret_id archive_rpc_secret_project

set -a
. ./.env
. ./.env.deploy
set +a

deploy_stage="infra"
# Ensure the Docker infra this host needs is up (Pylon on a deployed host; the
# full local stack in dev). See DITTO_COMPOSE_SERVICES in scripts/start.sh.
compose_services="${DITTO_COMPOSE_SERVICES:-postgres minio pylon}"
echo "==> ensuring infra ($compose_services)"
# shellcheck disable=SC2086
docker compose up -d --wait $compose_services

deploy_stage="migrate"
# `head`, singular, on purpose -- see the preflight assertion above for why the
# plural form was rejected. By this point the history is already known to
# resolve to exactly one head, so this cannot fail with "Multiple head
# revisions are present"; a failure here is a real migration failure, and the
# EXIT trap rolls the checkout back to whatever is still serving.
echo "==> applying migrations (head $migration_head)"
uv run alembic upgrade head

# --------------------------------------------------------------------------
# Start / reload / recreate.
#
# `pm2 reload <ecosystem.config.js>` does NOT reconcile the fields that decide
# how a process is launched: `script`, `interpreter`, `interpreter_args`,
# `exec_mode`, and `cwd` are kept from pm2's saved dump even when the ecosystem
# file changes them. `args` and env ARE reconciled. Changing `script` and
# reloading therefore relaunches the OLD program with the NEW args.
#
# That is exactly how the API went down in prod: the app moved from
# `script: "uv"` to `script: ".venv/bin/python"` with `args: "-m
# ditto.api_server"`, pm2 reloaded into `/usr/local/bin/uv -m ditto.api_server`,
# uv exited on `unexpected argument '-m' found`, pm2 parked the app in `waiting
# restart` with pid 0, and the site served 502 -- while this script exited 0.
#
# scripts/pm2_deploy_plan.js diffs each app's running launch identity against
# what the ecosystem file resolves to and picks per app:
#   start    -- pm2 does not know it yet (first deploy on a fresh host)
#   recreate -- launch identity drifted; `pm2 delete` + `pm2 start`
#   reload   -- identity matches; in-place `pm2 reload` (the ordinary path)
#
# Reload stays the default for ordinary code-only deploys. Today that is a
# stop/start anyway (single fork-mode process), but keeping the distinction
# means a future move to `exec_mode: "cluster"` gets real zero-downtime reloads
# without reintroducing this hazard.
deploy_stage="pm2-plan"
echo "==> planning pm2 actions"
pm2_plan="$(pm2 jlist 2>/dev/null | node scripts/pm2_deploy_plan.js scripts/ecosystem.config.js)"
[ -n "$pm2_plan" ] || { echo "ERROR: empty pm2 deploy plan; refusing to touch pm2" >&2; exit 1; }

# Space-separated name lists rather than arrays: pm2 app names never contain
# whitespace, and this keeps the script working on bash 3.2 as well as the
# host's bash 5.
fresh_apps=""
reload_apps=""
service_apps=""
oneshot_apps=""
# Column 5 (the configured script path) is unused here; fail_deploy reads it
# back out of "$pm2_plan" only when it has to explain a failure.
while IFS=$'\t' read -r action name role err_log _ reason; do
  [ -n "$name" ] || continue
  if [[ "$name" == ditto-api-relay-* ]]; then
    echo "    $name: managed by the rolling relay release; ordinary deploy skips it"
    continue
  fi
  case "$role" in
    oneshot) oneshot_apps="$oneshot_apps $name" ;;
    *) service_apps="$service_apps $name" ;;
  esac
  case "$action" in
    recreate)
      echo "    $name: recreate ($reason)"
      # Drift is only fixable by dropping pm2's saved definition. `|| true`:
      # a delete race must not abort a deploy that is about to re-start it.
      # From here on the old process is gone, so the EXIT trap must not rewind
      # the checkout underneath whatever pm2 brings up.
      deploy_stage="pm2-apply"
      deploy_pm2_touched=1
      pm2 delete "$name" >/dev/null 2>&1 || true
      fresh_apps="$fresh_apps $name"
      ;;
    start)
      echo "    $name: start ($reason)"
      fresh_apps="$fresh_apps $name"
      ;;
    *)
      echo "    $name: reload ($reason)"
      reload_apps="$reload_apps $name"
      ;;
  esac
done <<<"$pm2_plan"

join_csv() { echo "$*" | tr -s ' ' | sed -e 's/^ //' -e 's/ /,/g'; }

deploy_stage="pm2-apply"
if [ -n "${fresh_apps// /}" ]; then
  echo "==> starting:$fresh_apps"
  deploy_pm2_touched=1
  pm2 start scripts/ecosystem.config.js --only "$(join_csv "$fresh_apps")" --update-env
fi
if [ -n "${reload_apps// /}" ]; then
  echo "==> reloading:$reload_apps"
  deploy_pm2_touched=1
  pm2 reload scripts/ecosystem.config.js --only "$(join_csv "$reload_apps")" --update-env
fi
pm2 save

# --------------------------------------------------------------------------
# Verify the deploy actually produced a live app RUNNING THIS REVISION.
#
# Two defects are closed here. The first (#425): the script above can succeed
# while the app is dead -- pm2 reporting `online` is NOT proof of life (it
# reports online for a process that never bound its port), so the gate requires
# the API to answer HTTP.
#
# The second is what hid the 2026-07-25 near-outage: an app can be alive,
# serving 200s, and running code from an hour ago. Being checked out and being
# in effect are different facts, and only the process can report the second one.
# `/health` carries the commit resolved at ITS boot, so the gate below also
# requires that commit to equal the one this deploy checked out. Any path that
# leaves old code in service -- a skipped reload, a reload that silently kept
# the old process, an operator's stale pm2 dump -- now fails the deploy instead
# of passing it.
deploy_stage="verify"
DITTO_HEALTH_TIMEOUT="${DITTO_HEALTH_TIMEOUT:-120}"
# Root `/health` is the purpose-built liveness probe (cheap DB + chain reachability
# check); it is also what deploy.yml polls through Caddy. `/api/v1/public/health`
# is a different thing -- an aggregate subnet rollup -- and is not a liveness probe.
# The per-app URL comes from app_health_url_for: with more than one HTTP app on
# the host there is no longer a single health URL for the whole deploy.
health_snapshot="$(mktemp "${TMPDIR:-/tmp}/ditto-health.XXXXXX")"

# Print one app's live state as "status<TAB>pid<TAB>restarts<TAB>exec_path".
pm2_app_state() {
  pm2 jlist 2>/dev/null | node -e '
    const name = process.argv[1];
    let raw = "";
    process.stdin.on("data", (c) => (raw += c)).on("end", () => {
      const at = raw.indexOf("[");
      let list = [];
      try { list = at === -1 ? [] : JSON.parse(raw.slice(at)); } catch { list = []; }
      const app = (Array.isArray(list) ? list : []).find((a) => a && a.name === name);
      if (!app) { console.log("missing\t0\t0\t"); return; }
      const env = app.pm2_env || {};
      console.log([env.status || "unknown", app.pid || env.pm_pid || 0,
                   env.restart_time || 0, env.pm_exec_path || ""].join("\t"));
    });
  ' "$1"
}

# Dump everything an operator needs to diagnose a failed deploy, then exit 1.
fail_deploy() {
  local app="$1" why="$2" state err_log want running_script
  state="$(pm2_app_state "$app")"
  running_script="$(printf '%s' "$state" | cut -f4)"
  echo "" >&2
  echo "ERROR: deploy failed -- $app $why" >&2
  echo "  pm2 status/pid/restarts/script: $state" >&2
  err_log="$(printf '%s\n' "$pm2_plan" | awk -F'\t' -v n="$app" '$2 == n { print $4; exit }')"
  want="$(printf '%s\n' "$pm2_plan" | awk -F'\t' -v n="$app" '$2 == n { print $5; exit }')"
  if [ -n "$err_log" ] && [ -s "$err_log" ]; then
    echo "  --- tail -n 50 $err_log ---" >&2
    tail -n 50 "$err_log" >&2
  else
    echo "  (no error log at ${err_log:-<unset>}; try: pm2 logs $app --lines 50)" >&2
  fi
  # Only raise stale-definition suspicion when the paths actually disagree.
  # A hint that points at the wrong cause is worse than no hint.
  if [ -n "$running_script" ] && [ -n "$want" ] && [ "$running_script" != "$want" ]; then
    echo "" >&2
    echo "  pm2 is running a STALE script path (expected $want)." >&2
    echo "  Recover with: pm2 delete $app && ./scripts/update.sh" >&2
  fi
  exit 1
}

echo "==> verifying apps came up (timeout ${DITTO_HEALTH_TIMEOUT}s)"
deadline=$((SECONDS + DITTO_HEALTH_TIMEOUT))

# Unquoted on purpose: word-splitting the space-separated name list.
# shellcheck disable=SC2086
for app in $service_apps; do
  http_code=""
  served_commit=""
  app_health_url="$(app_health_url_for "$app")"
  while :; do
    IFS=$'\t' read -r status pid restarts _exec_path <<<"$(pm2_app_state "$app")"
    # `errored` is terminal for a service: pm2 exhausted max_restarts.
    [ "$status" = "errored" ] && fail_deploy "$app" "is in pm2 state 'errored'"

    if [ "$status" = "online" ]; then
      if [ -z "$app_health_url" ]; then
        echo "    $app: online (pid $pid, $restarts restarts)"
        break
      fi
      # Ground truth for an HTTP app: does the port actually answer, and is the
      # answer coming from THIS revision?
      http_code="$(curl -s -o "$health_snapshot" -m 5 -w '%{http_code}' "$app_health_url" 2>/dev/null || true)"
      if [ "$http_code" = "200" ]; then
        served_commit="$(json_string_field commit < "$health_snapshot" || true)"
        # An empty or "unknown" commit means the process cannot report one (a
        # checkout without git history). Do not invent a mismatch from a
        # missing fact -- that would fail every deploy on such a host.
        if [ -z "$served_commit" ] || [ "$served_commit" = "unknown" ]; then
          echo "    $app: online and serving 200 at $app_health_url (pid $pid, $restarts restarts)"
          echo "    WARNING: $app does not report a commit; cannot confirm it is running $deploy_target" >&2
          break
        fi
        # A stale process can answer 200 while pm2 is still swapping it out, so
        # a mismatch keeps polling until the deadline rather than failing on
        # the first sample.
        if [ "$served_commit" = "$deploy_target" ]; then
          echo "    $app: online and serving 200 at $app_health_url on commit $served_commit (pid $pid, $restarts restarts)"
          break
        fi
      fi
    fi

    if [ "$SECONDS" -ge "$deadline" ]; then
      # Separate the failure shapes: never came up, up but unhealthy, or up and
      # healthy while running code this deploy did not check out.
      if [ "$http_code" = "200" ] && [ -n "$served_commit" ] && [ "$served_commit" != "$deploy_target" ]; then
        fail_deploy "$app" \
          "is serving commit $served_commit but this deploy checked out $deploy_target -- the process never restarted into this build"
      fi
      if [ "$status" = "online" ] && [ -n "$http_code" ] && [ "$http_code" != "000" ]; then
        fail_deploy "$app" "is serving but $app_health_url returned HTTP $http_code (dependency down?)"
      fi
      fail_deploy "$app" "did not come up within ${DITTO_HEALTH_TIMEOUT}s (pm2 status '$status')"
    fi
    sleep 3
  done
done

# One-shots (ditto-screened-image-cleanup) are cron-triggered with
# autorestart:false. `stopped` is their CORRECT terminal state between runs, so
# only an explicit pm2 `errored` counts as a failure here.
# shellcheck disable=SC2086
for app in $oneshot_apps; do
  IFS=$'\t' read -r status _pid _restarts _exec_path <<<"$(pm2_app_state "$app")"
  case "$status" in
    errored) fail_deploy "$app" "is in pm2 state 'errored'" ;;
    missing) fail_deploy "$app" "is not registered with pm2 after start" ;;
    *) echo "    $app: $status (one-shot; not required to be online)" ;;
  esac
done

deploy_stage="done"
echo "done. pm2 logs ditto-api"
# Machine-readable last line: the deploy workflow reads this to assert that the
# public host is serving the same revision the host was left on. Keep the
# `key=value` shape stable.
echo "deployed-commit=$deploy_target"
