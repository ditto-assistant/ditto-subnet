#!/usr/bin/env bash
# Bounded BuildKit cache garbage collection for a dedicated screener host.
#
# Runs against the rootless screener executor daemon (DOCKER_HOST) only. It
# invokes BuildKit's own pruner with a retained-storage budget and a minimum
# record age. BuildKit skips records referenced by an in-flight build, so an
# active screening build is never disturbed. It never passes the all-records flag, never
# touches containers, volumes, networks, or images, and never runs a system
# prune. Every step is logged to stdout (journald under systemd).
set -euo pipefail

: "${DOCKER_HOST:?DOCKER_HOST must point at the rootless screener executor}"
keep_storage="${SCREENER_CACHE_GC_KEEP_STORAGE:?SCREENER_CACHE_GC_KEEP_STORAGE is required}"
# An empty minimum age is valid: no age floor (matches update-screener.sh).
min_age="${SCREENER_CACHE_GC_MIN_AGE-}"
# Log the filesystem holding the BuildKit cache. The role passes the executor
# home: df of that directory itself is stat-able by the unit user even though it
# is mode 0700 (only paths beneath it are not), and its data root is on the same
# mount. "/" is only the fallback when the variable is unset.
df_path="${SCREENER_CACHE_GC_DF_PATH:-/}"
dry_run="${SCREENER_CACHE_GC_DRY_RUN:-false}"

log() { printf 'ditto-screener-cache-gc: %s\n' "$*"; }
fail() { printf 'ditto-screener-cache-gc: ERROR: %s\n' "$*" >&2; exit 1; }

# Fail closed on malformed policy values rather than passing them to docker.
[[ "$keep_storage" =~ ^[0-9]+([.][0-9]+)?([KkMmGgTt]([Ii]?[Bb])?|[Bb])?$ ]] \
  || fail "invalid keep-storage '$keep_storage' (expected e.g. 40GB)"
[[ -z "$min_age" || "$min_age" =~ ^[0-9]+[smh]$ ]] \
  || fail "invalid minimum age '$min_age' (expected e.g. 1h or empty)"
[[ "$dry_run" == true || "$dry_run" == false ]] \
  || fail "invalid dry-run flag '$dry_run' (expected true or false)"

report() {
  log "$1: docker system df"
  docker system df || fail "docker system df failed ($1)"
  log "$1: df -h $df_path"
  df -h "$df_path" || log "WARNING: df -h $df_path failed ($1); disk usage not logged"
}

log "policy keep-storage=$keep_storage min-age=${min_age:-none} dry-run=$dry_run docker-host=$DOCKER_HOST"
docker version --format '{{.Server.Version}}' >/dev/null \
  || fail "rootless screener executor is not reachable at $DOCKER_HOST"

report before
prune_args=(builder prune --force --keep-storage "$keep_storage")
[[ -z "$min_age" ]] || prune_args+=(--filter "until=$min_age")
if [[ "$dry_run" == true ]]; then
  log "dry-run: would run docker ${prune_args[*]}"
else
  docker "${prune_args[@]}" \
    || fail "docker builder prune failed"
  report after
fi
log "done"
