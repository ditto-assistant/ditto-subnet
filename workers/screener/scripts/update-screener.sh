#!/usr/bin/env bash
set -euo pipefail

# Exact-commit, rollback-capable deployment for the isolated production worker.

SCREENER_ROOT="${SCREENER_ROOT:-/opt/ditto/screener}"
SCREENER_USER="${SCREENER_USER:-deploy}"
SCREENER_GROUP="${SCREENER_GROUP:-ditto}"
SCREENER_UNIT="${SCREENER_UNIT:-ditto-screener}"
SCREENER_EXPECTED_SHA="${SCREENER_EXPECTED_SHA:?missing SCREENER_EXPECTED_SHA}"
SCREENER_UV_BIN="${SCREENER_UV_BIN:-/usr/local/bin/uv}"
SCREENER_REPOSITORY_URL="${SCREENER_REPOSITORY_URL:-git@github.com:ditto-assistant/ditto-subnet.git}"
SCREENER_CACHE_GC_INTERVAL_SECONDS="${SCREENER_CACHE_GC_INTERVAL_SECONDS:-3600}"
SCREENER_CACHE_KEEP_STORAGE="${SCREENER_CACHE_KEEP_STORAGE:-40GB}"
SCREENER_GCP_PROJECT="${SCREENER_GCP_PROJECT:-ditto-app-dev}"
SCREENER_SOURCE_REVIEW_SECRET_ID="${SCREENER_SOURCE_REVIEW_SECRET_ID:-validator-openrouter-key}"
SCREENER_DNS_PROBE_IMAGE="${SCREENER_DNS_PROBE_IMAGE:-python:3.12-alpine@sha256:6d43704baacd1bfbe7c295d7f13079d5d8104ed33568873133f8fc69980419df}"

checkout="$SCREENER_ROOT/src"
source_dir="$checkout/workers/screener"
venv="$source_dir/.venv"
env_file="$SCREENER_ROOT/screener.env"
unit_source="$source_dir/deploy/ditto-screener.service"
unit_file="/etc/systemd/system/${SCREENER_UNIT}.service"
gc_state_dir="$SCREENER_ROOT/state"
gc_marker="$gc_state_dir/last-cache-gc"
# SHA the currently-active, health-verified process is running. Written ONLY
# after a restart passes health + the post-restart SHA check, so it is proof of
# what is RUNNING — unlike git HEAD, which a run interrupted between `reset` and
# a healthy restart leaves pointing at a not-yet-running commit.
deployed_marker="$gc_state_dir/deployed-sha"
l2_mode_marker="$gc_state_dir/deployed-l2-mode"
secret_dir="$SCREENER_ROOT/secrets"
source_review_key="$secret_dir/source-review-openrouter.key"
lock_file="$(dirname "$SCREENER_ROOT")/.screener-deploy.lock"
l2_analyzer_image="ditto-screener-l2-analyzer:active"

if [[ "${EUID}" -ne 0 ]]; then
  echo "update-screener.sh must run as root" >&2
  exit 1
fi

# Serialize deploys against first-boot bootstrap and other deploy runs: both
# mutate the same checkout / env / unit. bootstrap-screener.sh holds this lock
# across its whole body and exports SCREENER_DEPLOY_LOCK_HELD=1 when it invokes
# this script, so we don't re-lock (and deadlock) inside it.
if [[ "${SCREENER_DEPLOY_LOCK_HELD:-}" != "1" ]]; then
  exec {lock_fd}>"$lock_file"
  if ! flock -w 2400 "$lock_fd"; then
    echo "could not acquire deploy lock ($lock_file) within 40m" >&2
    exit 1
  fi
fi

ensure_state_dir() {
  # The updater runs as root, but the worker atomically writes its settings
  # cache and L2 audit data here. Repair ownership on every deploy, including
  # the no-op fast path, so a legacy root-created directory cannot pin the
  # worker to bootstrap settings.
  install -d -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0750 "$gc_state_dir"
}

ensure_enabled() {
  # First boot restarts the unit but a reboot then short-circuits on the
  # bootstrap marker, so the unit must be ENABLED to come back after a reboot.
  systemctl is-enabled --quiet "$SCREENER_UNIT" 2>/dev/null \
    || systemctl enable "$SCREENER_UNIT" >/dev/null
}

verify_installed_signing_contract() {
  # Exercise the installed venv, not the checkout import path. uv caches local
  # directory packages by name/version; a protocol source change without a
  # fresh install can otherwise leave the worker importing an older wheel while
  # CI imports the newer workspace source. That skew crashes only after an
  # expensive screen, when the worker signs its verdict.
  runuser -u "$SCREENER_USER" -- \
    "$venv/bin/python" "$source_dir/scripts/verify-installed-signing-contract.py"
}

record_deployed_sha() {
  mkdir -p "$gc_state_dir"
  printf '%s\n' "$1" >"$deployed_marker"
}

record_l2_mode() {
  mkdir -p "$gc_state_dir"
  printf '%s\n' "$1" >"$l2_mode_marker"
}

ensure_imds_guard() {
  # Block the GCE metadata server (169.254.169.254) from Docker container/build
  # networks. A submission-controlled Dockerfile builds and runs with network
  # access; left open, a hostile RUN step mints the VM's attached-SA token (the
  # shared platform runtime SA) and reads platform/validator secrets. Container
  # and build traffic to metadata traverses the FORWARD path (DOCKER-USER chain);
  # the host's own gcloud uses OUTPUT and is unaffected. GCE also uses the same
  # IP as the VM's DNS resolver, so TCP/UDP 53 must remain reachable from Docker.
  #
  # bootstrap-screener.sh installs the same guard at fleet first boot; running it
  # here too means every deploy re-ensures it on the pet VM (hand-provisioned,
  # never ran bootstrap) and self-heals any instance where it drifted. Idempotent
  # and file-diff gated so a no-op deploy neither reloads systemd nor restarts the
  # worker — no downtime. Docker requires iptables, so it is always present here.
  local guard_bin=/usr/local/sbin/ditto-imds-guard
  local guard_unit=/etc/systemd/system/ditto-imds-guard.service
  local tmp changed=0
  tmp="$(mktemp)"
  cat >"$tmp" <<'GUARD'
#!/usr/bin/env bash
set -euo pipefail
# DOCKER-USER is created by dockerd; ensure it exists before inserting.
iptables -N DOCKER-USER 2>/dev/null || true
# Keep the policy in a dedicated chain so DNS exceptions precede the metadata
# drop unambiguously. Build a unique replacement first so the active policy is
# never flushed in place and metadata stays protected throughout the swap.
guard_tmp="DITTO-IMDS-GUARD-$$"
iptables -N "$guard_tmp"
iptables -A "$guard_tmp" -p udp -d 169.254.169.254/32 --dport 53 -j ACCEPT
iptables -A "$guard_tmp" -p tcp -d 169.254.169.254/32 --dport 53 -j ACCEPT
iptables -A "$guard_tmp" -d 169.254.169.254/32 -j DROP
iptables -I DOCKER-USER 1 -j "$guard_tmp"
# RootlessKit/slirp traffic originates from an unprivileged host process and
# traverses OUTPUT, not DOCKER-USER. Preserve metadata DNS, but deny every
# non-root host process the metadata API and attached service-account token.
iptables -I OUTPUT 1 -m owner ! --uid-owner 0 -d 169.254.169.254/32 -j "$guard_tmp"
# The replacement now protects metadata. Remove the DNS-breaking legacy rule
# and old jump before renaming the referenced replacement to the stable name.
while iptables -D DOCKER-USER -d 169.254.169.254/32 -j DROP 2>/dev/null; do :; done
while iptables -D DOCKER-USER -j DITTO-IMDS-GUARD 2>/dev/null; do :; done
while iptables -D OUTPUT -m owner ! --uid-owner 0 -d 169.254.169.254/32 -j DITTO-IMDS-GUARD 2>/dev/null; do :; done
iptables -F DITTO-IMDS-GUARD 2>/dev/null || true
iptables -X DITTO-IMDS-GUARD 2>/dev/null || true
iptables -E "$guard_tmp" DITTO-IMDS-GUARD

GUARD
  if ! cmp -s "$tmp" "$guard_bin"; then
    install -m 0755 "$tmp" "$guard_bin"
    changed=1
  fi
  rm -f "$tmp"
  tmp="$(mktemp)"
  cat >"$tmp" <<'UNIT'
[Unit]
Description=Block cloud metadata (IMDS) from Docker container/build networks
After=docker.service
Wants=docker.service
PartOf=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
ExecStart=/usr/local/sbin/ditto-imds-guard

[Install]
WantedBy=multi-user.target
UNIT
  if ! cmp -s "$tmp" "$guard_unit"; then
    install -m 0644 "$tmp" "$guard_unit"
    changed=1
  fi
  rm -f "$tmp"
  # Applying the rule is the security control: fail the deploy if it cannot be
  # installed rather than run a worker exposed to metadata exfil.
  if [[ "$changed" -eq 1 ]]; then
    systemctl daemon-reload
    systemctl restart ditto-imds-guard.service
  else
    systemctl start ditto-imds-guard.service
  fi
  systemctl is-enabled --quiet ditto-imds-guard.service 2>/dev/null \
    || systemctl enable ditto-imds-guard.service >/dev/null
}

probe_docker_dns() {
  # Pull resolution happens in dockerd's host namespace; getent then exercises
  # the container/build FORWARD path that the IMDS guard controls. Keep the
  # image digest in lockstep with ditto_screener.gate._CANARY_IMAGE.
  docker run --rm --network bridge --entrypoint /bin/sh \
    "$SCREENER_DNS_PROBE_IMAGE" -c 'getent hosts github.com >/dev/null'
}

for path in "$checkout/.git" "$env_file" "$SCREENER_UV_BIN"; do
  if [[ ! -e "$path" ]]; then
    echo "required screener deployment path is missing: $path" >&2
    exit 1
  fi
done

env_value() {
  local key="$1"
  sed -n "s/^${key}=//p" "$env_file" | tail -n 1
}

configure_docker_endpoint() {
  local docker_host require_rootless security_options
  docker_host="$(env_value SCREENER_DOCKER_HOST)"
  require_rootless="$(env_value SCREENER_REQUIRE_ROOTLESS_DOCKER)"
  if [[ -n "$docker_host" ]]; then
    export DOCKER_HOST="$docker_host"
  fi
  if [[ "$require_rootless" == "1" || "$require_rootless" == "true" ]]; then
    security_options="$(docker info --format '{{json .SecurityOptions}}')"
    if [[ "$security_options" != *rootless* ]]; then
      echo "configured Docker endpoint is not rootless" >&2
      return 1
    fi
  fi
}

probe_platform() {
  local platform_url api_token hotkey response required supported floor
  platform_url="$(env_value SCREENER_PLATFORM_API_URL)"
  api_token="$(env_value SCREENER_API_TOKEN)"
  hotkey="$(env_value SCREENER_HOTKEY)"
  : "${platform_url:?missing SCREENER_PLATFORM_API_URL}"
  : "${api_token:?missing SCREENER_API_TOKEN}"
  : "${hotkey:?missing SCREENER_HOTKEY}"

  response="$(curl --fail --silent --show-error --config - \
    "$platform_url/api/v1/screener/queue?limit=1" <<CURL_CONFIG
header = "Authorization: Bearer $api_token"
header = "X-Screener-Hotkey: $hotkey"
CURL_CONFIG
  )"
  required="$(printf '%s' "$response" | "$venv/bin/python" -c \
    'import json,sys; print(json.load(sys.stdin)["required_policy_version"])')"
  read -r floor supported < <(runuser -u "$SCREENER_USER" -- \
    "$venv/bin/python" -c \
    'from ditto_screening_protocol import SCREENING_FLOOR_POLICY_VERSION, SCREENING_POLICY_VERSION; print(SCREENING_FLOOR_POLICY_VERSION, SCREENING_POLICY_VERSION)')
  if (( required < floor || required > supported )); then
    echo "screening policy mismatch: platform requires $required, worker supports $floor-$supported" >&2
    return 1
  fi
}

upsert_env() {
  local key="$1" value="$2" tmp
  tmp="$(mktemp)"
  grep -v "^${key}=" "$env_file" >"$tmp" || true
  printf '%s=%s\n' "$key" "$value" >>"$tmp"
  install -o root -g ditto -m 0640 "$tmp" "$env_file"
  rm -f "$tmp"
}

materialize_source_review_key() {
  local tmp
  command -v gcloud >/dev/null || {
    echo "gcloud is required to materialize the source review key" >&2
    return 1
  }
  install -d -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0750 "$secret_dir"
  tmp="$(mktemp)"
  if ! gcloud secrets versions access latest \
    --project="$SCREENER_GCP_PROJECT" \
    --secret="$SCREENER_SOURCE_REVIEW_SECRET_ID" >"$tmp"; then
    rm -f "$tmp"
    return 1
  fi
  install -o "$SCREENER_USER" -g ditto -m 0400 "$tmp" "$source_review_key"
  rm -f "$tmp"
  upsert_env SCREENER_SOURCE_REVIEW_API_KEY_FILE "$source_review_key"
}

l2_mode() {
  grep '^SCREENER_L2_REVIEW_MODE=' "$env_file" 2>/dev/null \
    | tail -n 1 | cut -d= -f2- || true
}

ensure_l2_analyzer() {
  local sha="$1" current_label
  # Platform-managed settings can switch this worker from off to shadow/enforce
  # between leases. Keep the trusted analyzer ready at the deployed SHA so that
  # change never depends on a second host deployment or a static env toggle.
  current_label="$(docker image inspect --format \
    '{{index .Config.Labels "ai.heyditto.screener.sha"}}' \
    "$l2_analyzer_image" 2>/dev/null || true)"
  if [[ "$current_label" != "$sha" ]]; then
    echo "==> building trusted L2 analyzer for $sha"
    docker build \
      --file "$source_dir/deploy/l2-analyzer.Dockerfile" \
      --label "ai.heyditto.screener.sha=$sha" \
      --tag "$l2_analyzer_image" \
      "$source_dir"
  fi
  # Execute an inert command through the same non-root/no-network boundary so
  # a successful image build cannot hide an architecture/import/runtime error.
  printf '{}' | docker run -i --rm \
    --network none --read-only --cap-drop ALL \
    --security-opt no-new-privileges --pids-limit 64 --memory 256m --cpus 0.5 \
    --mount "type=bind,src=$source_dir,dst=/workspace,readonly" \
    --tmpfs /scratch:rw,noexec,nosuid,nodev,size=33554432,mode=1777 \
    "$l2_analyzer_image" build_structure >/dev/null
}

wait_for_health() {
  local consecutive_healthy=0
  for attempt in $(seq 1 30); do
    if systemctl is-active --quiet "$SCREENER_UNIT" && probe_platform; then
      consecutive_healthy=$((consecutive_healthy + 1))
      if [[ "$consecutive_healthy" -ge 3 ]]; then
        return 0
      fi
    else
      consecutive_healthy=0
    fi
    if [[ "$attempt" -eq 30 ]]; then
      return 1
    fi
    sleep 2
  done
}

maintain_cache() {
  mkdir -p "$gc_state_dir"
  local now last
  now="$(date +%s)"
  last=0
  if [[ -f "$gc_marker" ]]; then
    last="$(stat -c %Y "$gc_marker" 2>/dev/null || echo 0)"
  fi
  if (( now - last < SCREENER_CACHE_GC_INTERVAL_SECONDS )); then
    return 0
  fi
  # keep-storage is the bound. No age filter: an age filter exempts cache
  # created during a heavy screening burst (a policy-rescreen wave rebuilds
  # every submission in one day), which is exactly when the cache blows past
  # the budget. BuildKit prunes least-recently-used first and skips in-use
  # records, so an active build is never disturbed. The Docker daemon's own
  # builder GC (deploy/daemon.json) enforces the same budget continuously;
  # this pass is the backstop.
  docker builder prune --force --keep-storage "$SCREENER_CACHE_KEEP_STORAGE"
  docker image prune --force --filter until=168h
  touch "$gc_marker"
}

build_in_flight() {
  # A screening build in progress under the worker. A restart of docker (or the
  # worker) aborts it; the killed build now requeues as retryable-infra rather
  # than terminally rejecting the miner, but a requeue still throws away a full
  # rebuild, so disruptive maintenance defers to an idle run.
  pgrep -f "build -t ditto-screen" >/dev/null 2>&1
}

maintain_daemon_config() {
  # Repository-owned Docker daemon config: BuildKit's own GC enforces the
  # cache budget continuously (per build), so the disk stays bounded even
  # between updater passes. Docker is only restarted when the config actually
  # changes; a killed in-flight build reports retryable-infra and the lease
  # requeues the submission.
  local source target daemon_unit owner group executor_home
  if [[ -n "${DOCKER_HOST:-}" ]]; then
    source="$source_dir/deploy/rootless-daemon.json"
    # Must match install-rootless-docker.sh's daemon_root exactly. Maintaining
    # a lookalike file under the worker checkout would restart the daemon while
    # leaving its live security policy unchanged.
    executor_home="$(env_value SCREENER_EXECUTOR_HOME)"
    executor_home="${executor_home:-/var/lib/ditto-screener-docker}"
    target="$executor_home/docker/daemon.json"
    daemon_unit="ditto-screener-docker"
    owner="$(env_value SCREENER_EXECUTOR_USER)"
    owner="${owner:-ditto-builder}"
    group="$(env_value SCREENER_EXECUTOR_GROUP)"
    group="${group:-ditto-builder}"
    if [[ "$executor_home" != /var/lib/ditto-screener-docker ]] \
      || [[ ! "$owner" =~ ^[a-z_][a-z0-9_-]*$ ]] \
      || [[ ! "$group" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
      echo "invalid rootless executor daemon identity" >&2
      return 1
    fi
  else
    source="$source_dir/deploy/daemon.json"
    target="/etc/docker/daemon.json"
    daemon_unit="docker"
    owner="root"
    group="root"
  fi
  [[ -f "$source" ]] || return 0
  if cmp -s "$source" "$target" 2>/dev/null; then
    return 0
  fi
  if ! dockerd --validate --config-file "$source"; then
    echo "deploy/daemon.json failed dockerd validation; keeping current config" >&2
    return 1
  fi
  if build_in_flight; then
    echo "deferring daemon.json apply: a screening build is in flight" >&2
    return 0
  fi
  install -o "$owner" -g "$group" -m 0640 "$source" "$target"
  systemctl restart "$daemon_unit"
  # Requires=docker.service can propagate the stop to the worker; make sure
  # it is running again (no-op when the restart left it untouched).
  systemctl start "$SCREENER_UNIT"
}

maintain_logs() {
  # The unit appends to a plain log file forever; keep it bounded without
  # restarting the service (in-place truncation preserves the O_APPEND fd).
  local log_file="/opt/ditto/logs/${SCREENER_UNIT}.log"
  local max_bytes=$((64 * 1024 * 1024))
  [[ -f "$log_file" ]] || return 0
  local size
  size="$(stat -c %s "$log_file" 2>/dev/null || echo 0)"
  if (( size > max_bytes )); then
    local keep
    keep="$(tail -c $((max_bytes / 4)) "$log_file")"
    printf '%s\n' "$keep" >"$log_file"
  fi
}

ensure_state_dir
current_sha="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" rev-parse HEAD)"
current_origin="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" remote get-url origin)"
if [[ "$current_origin" != "$SCREENER_REPOSITORY_URL" ]]; then
  runuser -u "$SCREENER_USER" -- git -C "$checkout" remote set-url origin \
    "$SCREENER_REPOSITORY_URL"
fi

# Select and verify the deploy-owned daemon before any Docker command. An
# invalid/missing rootless endpoint fails the deployment without touching the
# currently running worker.
configure_docker_endpoint

# Refresh the protected key on every scheduled deployment run so Secret Manager
# rotation does not require an unrelated code change.
materialize_source_review_key

# A worker lease owns the complete build/runtime/review lifecycle. Older fleet
# instances were bootstrapped with `prefer`/`require`, which silently delegated
# builds to Platform's Cloud Run fallback even after the fleet-local default was
# corrected. Reconcile the durable env on every release and force the full
# restart path when repairing a running instance.
remote_build_mode_changed=false
if [[ "$(env_value SCREENER_REMOTE_BUILD_MODE)" != "off" ]]; then
  upsert_env SCREENER_REMOTE_BUILD_MODE off
  remote_build_mode_changed=true
fi

# Ensure the metadata guard before any path that could (re)start the worker or
# leave it running — runs on both the fast path and a full deploy so a pet VM
# that never had it, or an instance where it drifted, is protected every deploy.
ensure_imds_guard
probe_docker_dns

deployed_sha=""
[[ -f "$deployed_marker" ]] && deployed_sha="$(cat "$deployed_marker")"
deployed_l2_mode="off"
[[ -f "$l2_mode_marker" ]] && deployed_l2_mode="$(cat "$l2_mode_marker")"
requested_l2_mode="$(l2_mode)"
[[ -n "$requested_l2_mode" ]] || requested_l2_mode="off"
# Fast path gates on the RUNNING-verified SHA (marker), not git HEAD: a prior
# run that reset HEAD to the new SHA but died before a healthy restart must not
# be reported as deployed just because HEAD matches and the OLD process is up.
if [[ -n "$deployed_sha" ]] && [[ "$deployed_sha" == "$SCREENER_EXPECTED_SHA" ]] && \
  [[ "$current_sha" == "$SCREENER_EXPECTED_SHA" ]] && \
  [[ "$deployed_l2_mode" == "$requested_l2_mode" ]] && \
  [[ "$remote_build_mode_changed" == false ]] && \
  systemctl is-active --quiet "$SCREENER_UNIT" && \
  verify_installed_signing_contract; then
  ensure_l2_analyzer "$SCREENER_EXPECTED_SHA"
  probe_platform
  ensure_enabled
  maintain_daemon_config
  maintain_cache
  maintain_logs
  echo "healthy: $SCREENER_UNIT already at $current_sha"
  exit 0
fi

echo "==> fetching $SCREENER_EXPECTED_SHA"
runuser -u "$SCREENER_USER" -- git -C "$checkout" fetch --prune origin \
  "$SCREENER_EXPECTED_SHA"
resolved_sha="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" rev-parse FETCH_HEAD)"
if [[ "$resolved_sha" != "$SCREENER_EXPECTED_SHA" ]]; then
  echo "$SCREENER_EXPECTED_SHA resolved to unexpected commit $resolved_sha" >&2
  exit 1
fi

runuser -u "$SCREENER_USER" -- git -C "$checkout" reset --hard "$resolved_sha"
# git reset --hard leaves untracked files in place. Pre-extraction the worker
# shipped a ``ditto/screener`` namespace; a leftover ``ditto`` tree keeps
# shadowing the import path (``python -m ditto.screener`` half-resolves against
# it). Drop it so the checkout matches the commit. Scoped to ``ditto`` so the
# ignored ``.venv`` and sibling state/secret dirs are never touched.
runuser -u "$SCREENER_USER" -- git -C "$checkout" clean -fd -- \
  workers/screener/ditto
runuser -u "$SCREENER_USER" -- env UV_PROJECT_ENVIRONMENT="$venv" \
  "$SCREENER_UV_BIN" sync --frozen \
    --reinstall-package ditto-screening-protocol --project "$source_dir"
verify_installed_signing_contract
ensure_l2_analyzer "$resolved_sha"

if [[ ! -f "$unit_source" ]]; then
  echo "required screener unit is missing: $unit_source" >&2
  runuser -u "$SCREENER_USER" -- git -C "$checkout" reset --hard "$current_sha"
  runuser -u "$SCREENER_USER" -- env UV_PROJECT_ENVIRONMENT="$venv" \
    "$SCREENER_UV_BIN" sync --frozen \
      --reinstall-package ditto-screening-protocol --project "$source_dir"
  if [[ "$requested_l2_mode" != "$deployed_l2_mode" ]]; then
    upsert_env SCREENER_L2_REVIEW_MODE "$deployed_l2_mode"
  fi
  ensure_l2_analyzer "$current_sha"
  exit 1
fi

unit_backup="$(mktemp)"
unit_existed=false
if [[ -f "$unit_file" ]]; then
  cp "$unit_file" "$unit_backup"
  unit_existed=true
fi
cleanup() {
  rm -f "$unit_backup"
}
trap cleanup EXIT

install -o root -g root -m 0644 "$unit_source" "$unit_file"
systemctl daemon-reload
ensure_enabled

systemctl restart "$SCREENER_UNIT"
if ! wait_for_health; then
  echo "new screener failed health checks; rolling back to $current_sha" >&2
  runuser -u "$SCREENER_USER" -- git -C "$checkout" reset --hard "$current_sha"
  runuser -u "$SCREENER_USER" -- env UV_PROJECT_ENVIRONMENT="$venv" \
    "$SCREENER_UV_BIN" sync --frozen \
      --reinstall-package ditto-screening-protocol --project "$source_dir"
  if [[ "$requested_l2_mode" != "$deployed_l2_mode" ]]; then
    upsert_env SCREENER_L2_REVIEW_MODE "$deployed_l2_mode"
  fi
  ensure_l2_analyzer "$current_sha"
  if [[ "$unit_existed" == true ]]; then
    install -o root -g root -m 0644 "$unit_backup" "$unit_file"
  fi
  systemctl daemon-reload
  systemctl restart "$SCREENER_UNIT"
  wait_for_health || systemctl status "$SCREENER_UNIT" --no-pager >&2 || true
  exit 1
fi

actual_sha="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" rev-parse HEAD)"
if [[ "$actual_sha" != "$SCREENER_EXPECTED_SHA" ]]; then
  echo "healthy process is at unexpected commit $actual_sha" >&2
  exit 1
fi
# Only now is the new SHA proven running + healthy: record it so the next run's
# fast path can trust it (and so an interrupted future run cannot be misread).
record_deployed_sha "$actual_sha"
record_l2_mode "$requested_l2_mode"
maintain_daemon_config
maintain_cache
maintain_logs
echo "healthy: $SCREENER_UNIT active at $actual_sha; platform preflight accepted"
