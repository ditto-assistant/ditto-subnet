#!/usr/bin/env bash
# Bounded retention for the PostgreSQL logging collector directory.
#
# PostgreSQL's log_rotation_age / log_rotation_size only START a new file; they
# never reclaim an old one. That is what filled ditto-pg-platform's root
# filesystem on 2026-09-08 (12 GB of postgresql-YYYY-MM-DD.log, ~635 MB/day, no
# retention since 2026-07-21 — issue #1745). This script applies two explicit
# bounds, in order:
#
#   1. age:     delete collector files whose mtime is older than
#               POSTGRES_LOG_GC_RETENTION_DAYS days;
#   2. ceiling: while the directory still exceeds POSTGRES_LOG_GC_MAX_TOTAL_MB,
#               delete the oldest remaining file.
#
# The ceiling pass is the hard bound: a burst of slow-query volume cannot outrun
# it the way a pure age policy can. Files PostgreSQL is currently writing are
# never deleted (the collector's current_logfiles entries plus the newest file by
# mtime), so the bound in practice is the ceiling plus one live file.
#
# It only ever deletes regular files matching POSTGRES_LOG_GC_GLOB directly
# inside POSTGRES_LOG_GC_DIR. It never recurses, never touches the data
# directory, WAL, backups, or any other path, and never stops PostgreSQL.
set -euo pipefail

dir="${POSTGRES_LOG_GC_DIR:?POSTGRES_LOG_GC_DIR is required}"
glob="${POSTGRES_LOG_GC_GLOB:-postgresql-*.log}"
retention_days="${POSTGRES_LOG_GC_RETENTION_DAYS:?POSTGRES_LOG_GC_RETENTION_DAYS is required}"
max_total_mb="${POSTGRES_LOG_GC_MAX_TOTAL_MB:?POSTGRES_LOG_GC_MAX_TOTAL_MB is required}"
# Optional: the collector's current_logfiles map in PGDATA. Every path it names
# is protected even if it is older than the retention window.
current_logfiles="${POSTGRES_LOG_GC_CURRENT_LOGFILES:-}"
dry_run="${POSTGRES_LOG_GC_DRY_RUN:-false}"

log() { printf 'ditto-postgres-log-gc: %s\n' "$*"; }
fail() {
  printf 'ditto-postgres-log-gc: ERROR: %s\n' "$*" >&2
  exit 1
}

# Fail closed on a malformed policy rather than deleting on a guessed bound.
[[ "$retention_days" =~ ^[0-9]+$ && "$retention_days" -ge 1 ]] ||
  fail "invalid retention days '$retention_days' (expected an integer >= 1)"
[[ "$max_total_mb" =~ ^[0-9]+$ && "$max_total_mb" -ge 1 ]] ||
  fail "invalid max total MB '$max_total_mb' (expected an integer >= 1)"
[[ "$dry_run" == true || "$dry_run" == false ]] ||
  fail "invalid dry-run flag '$dry_run' (expected true or false)"
# The glob is expanded by find, not the shell; keep it a single filename segment
# so it can never walk out of the log directory.
[[ -n "$glob" && "$glob" != */* && "$glob" != .. && "$glob" != . ]] ||
  fail "invalid glob '$glob' (expected a single-segment filename pattern)"
[[ -d "$dir" ]] || fail "log directory '$dir' does not exist"

max_total_bytes=$((max_total_mb * 1024 * 1024))

# mtime<TAB>size<TAB>path for every candidate, oldest first. Collector filenames
# come from log_filename, so they contain no tabs or newlines.
list_candidates() {
  find "$dir" -maxdepth 1 -type f -name "$glob" -printf '%T@\t%s\t%p\n' | sort -n
}

count_candidates() {
  list_candidates | wc -l | tr -d ' '
}

total_bytes() {
  local sum=0 mtime size path
  while IFS=$'\t' read -r mtime size path; do
    sum=$((sum + size))
  done < <(list_candidates)
  printf '%s\n' "$sum"
}

protected=()
is_protected() {
  local candidate="$1" entry
  for entry in ${protected[@]+"${protected[@]}"}; do
    [[ "$candidate" == "$entry" ]] && return 0
  done
  return 1
}

# Protect whatever the collector is writing right now.
if [[ -n "$current_logfiles" && -r "$current_logfiles" ]]; then
  while read -r _ live_path; do
    [[ -n "$live_path" ]] || continue
    # current_logfiles records the log_directory-relative path when log_directory
    # is relative; resolve such entries inside the managed directory.
    [[ "$live_path" == /* ]] || live_path="$dir/${live_path##*/}"
    protected+=("$live_path")
    log "protecting live collector file $live_path"
  done <"$current_logfiles"
elif [[ -n "$current_logfiles" ]]; then
  log "WARNING: current_logfiles '$current_logfiles' is not readable; using newest-file protection only"
fi

# Always protect the newest file, whether or not current_logfiles was readable.
newest="$(list_candidates | tail -1 | cut -f3-)"
if [[ -n "$newest" ]] && ! is_protected "$newest"; then
  protected+=("$newest")
  log "protecting newest collector file $newest"
fi

remove() {
  local path="$1" reason="$2"
  if [[ "$dry_run" == true ]]; then
    log "dry-run: would delete $path ($reason)"
    return 0
  fi
  rm -f -- "$path" || fail "failed to delete $path"
  log "deleted $path ($reason)"
}

before_bytes="$(total_bytes)"
log "policy dir=$dir glob=$glob retention-days=$retention_days max-total-mb=$max_total_mb dry-run=$dry_run"
log "before: $before_bytes bytes in $(count_candidates) collector files"
df -h "$dir" || log "WARNING: df -h $dir failed (before); disk usage not logged"

# Pass 1 — age.
while IFS=$'\t' read -r mtime size path; do
  is_protected "$path" && continue
  remove "$path" "older than ${retention_days}d"
done < <(find "$dir" -maxdepth 1 -type f -name "$glob" -mtime "+$retention_days" \
  -printf '%T@\t%s\t%p\n' | sort -n)

# Pass 2 — hard size ceiling, oldest first. `running` is decremented for dry runs
# too, so a rehearsal logs the complete plan rather than only its first step.
running="$(total_bytes)"
if ((running > max_total_bytes)); then
  log "still $running bytes over the ${max_total_bytes}-byte ceiling; pruning oldest first"
  while IFS=$'\t' read -r mtime size path; do
    ((running > max_total_bytes)) || break
    is_protected "$path" && continue
    remove "$path" "over the ${max_total_mb}MB ceiling"
    running=$((running - size))
  done < <(list_candidates)
  if ((running > max_total_bytes)); then
    log "WARNING: $running bytes remain above the ceiling; only live collector files are left"
  fi
fi

after_bytes="$(total_bytes)"
log "after: $after_bytes bytes in $(count_candidates) collector files (reclaimed $((before_bytes - after_bytes)) bytes)"
df -h "$dir" || log "WARNING: df -h $dir failed (after); disk usage not logged"
log "done"
