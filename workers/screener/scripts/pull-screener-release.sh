#!/usr/bin/env bash
set -euo pipefail

# Pull and authenticate the release channel published by release.yml, then
# converge this GCE overflow worker to that exact commit. This replaces the
# GitHub-hosted, label-discovery SSH reconciliation loop.

SCREENER_ROOT="${SCREENER_ROOT:-/opt/ditto/screener}"
SCREENER_RELEASE_STATE_DIR="${SCREENER_RELEASE_STATE_DIR:-$SCREENER_ROOT/state/release-pull}"
SCREENER_RELEASE_CHANNEL="${SCREENER_RELEASE_CHANNEL:-ghcr.io/ditto-assistant/ditto-subnet-stack:screener-fleet-stable-1}"
SCREENER_DESCRIPTOR_REPOSITORY="ghcr.io/ditto-assistant/ditto-subnet-stack"
SCREENER_UPDATE_SCRIPT="${SCREENER_UPDATE_SCRIPT:-$SCREENER_ROOT/src/workers/screener/scripts/update-screener.sh}"
SCREENER_SERVICE="${SCREENER_SERVICE:-ditto-screener}"
EXPECTED_FORMAT_VERSION=1
EXPECTED_UPDATE_PROTOCOL=1

log() { printf 'pull-screener-release: %s\n' "$*" >&2; }
die() { log "error: $*"; exit 1; }
manifest_value() {
  awk -F= -v key="$2" '$1 == key {print substr($0,index($0,"=")+1); exit}' "$1"
}

validate_manifest() {
  local file="$1" line key count=0 seen='|' allowed
  allowed=' FLEET_FORMAT_VERSION FLEET_VERSION FLEET_REVISION FLEET_UPDATE_PROTOCOL SUBMISSION_BUILDER_IMAGE '
  [ -f "$file" ] && [ ! -L "$file" ] || return 1
  while IFS= read -r line || [ -n "$line" ]; do
    [ -n "$line" ] || continue
    [[ "$line" =~ ^([A-Z][A-Z0-9_]*)=([^[:space:]]+)$ ]] || return 1
    key="${BASH_REMATCH[1]}"
    [[ "$allowed" == *" $key "* ]] || return 1
    [[ "$seen" != *"|$key|"* ]] || return 1
    seen="${seen}${key}|"
    count=$((count + 1))
  done <"$file"
  [ "$count" -eq 5 ] || return 1
  for key in $allowed; do [[ "$seen" == *"|$key|"* ]] || return 1; done
  [ "$(manifest_value "$file" FLEET_FORMAT_VERSION)" = "$EXPECTED_FORMAT_VERSION" ] || return 1
  [[ "$(manifest_value "$file" FLEET_VERSION)" =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || return 1
  [[ "$(manifest_value "$file" FLEET_REVISION)" =~ ^[0-9a-f]{40}$ ]] || return 1
  [ "$(manifest_value "$file" FLEET_UPDATE_PROTOCOL)" = "$EXPECTED_UPDATE_PROTOCOL" ] || return 1
  [[ "$(manifest_value "$file" SUBMISSION_BUILDER_IMAGE)" =~ ^us-central1-docker\.pkg\.dev/ditto-app-dev/ditto-public-builders/submission-builder@sha256:[0-9a-f]{64}$ ]]
}

[ "$(id -u)" -eq 0 ] || die "run as root"
for command in cosign docker flock systemctl; do
  command -v "$command" >/dev/null 2>&1 || die "$command is required"
done
[ -x "$SCREENER_UPDATE_SCRIPT" ] || die "update script is unavailable: $SCREENER_UPDATE_SCRIPT"

install -d -o root -g root -m 0700 "$SCREENER_RELEASE_STATE_DIR"
exec 9>"$SCREENER_RELEASE_STATE_DIR/lock"
flock -n 9 || { log "another pull update is active"; exit 0; }

docker pull --platform linux/amd64 "$SCREENER_RELEASE_CHANNEL" >/dev/null
exact="$({
  docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' \
    "$SCREENER_RELEASE_CHANNEL"
} | awk -v repo="$SCREENER_DESCRIPTOR_REPOSITORY@" 'index($0, repo) == 1 {print; exit}')"
[[ "$exact" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$ ]] || \
  die "release channel did not resolve to an immutable Ditto descriptor"

if [ -f "$SCREENER_RELEASE_STATE_DIR/failed-descriptor" ] && \
   [ "$(cat "$SCREENER_RELEASE_STATE_DIR/failed-descriptor")" = "$exact" ]; then
  log "descriptor remains suppressed after a failed activation: $exact"
  exit 0
fi

cosign verify \
  --certificate-identity-regexp \
    '^https://github.com/ditto-assistant/ditto-subnet/.github/workflows/release.yml@refs/heads/main$' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com \
  "$exact" >/dev/null

temporary="$(mktemp -d)"
container=""
cleanup() {
  [ -z "$container" ] || docker rm -f "$container" >/dev/null 2>&1 || true
  rm -rf -- "$temporary"
}
trap cleanup EXIT
container="$(docker create --platform linux/amd64 "$exact")"
docker cp "$container:/release/manifest.env" "$temporary/manifest.env"
docker rm "$container" >/dev/null
container=""
validate_manifest "$temporary/manifest.env" || die "release manifest failed its closed schema"
[ "$(docker image inspect --format '{{ index .Config.Labels "io.heyditto.screener.fleet-release" }}' "$exact")" = true ] || \
  die "descriptor release label is invalid"
[ "$(docker image inspect --format '{{ index .Config.Labels "io.heyditto.screener.fleet-update-protocol" }}' "$exact")" = "$EXPECTED_UPDATE_PROTOCOL" ] || \
  die "descriptor update protocol label is invalid"

revision="$(manifest_value "$temporary/manifest.env" FLEET_REVISION)"
deployed_sha="$(cat "$SCREENER_ROOT/state/deployed-sha" 2>/dev/null || true)"
managed_descriptor="$(cat "$SCREENER_RELEASE_STATE_DIR/managed-descriptor" 2>/dev/null || true)"
if [ "$managed_descriptor" = "$exact" ] && [ "$deployed_sha" = "$revision" ] && \
   systemctl is-active --quiet "$SCREENER_SERVICE"; then
  log "healthy worker already consumes $exact"
  exit 0
fi

if ! SCREENER_EXPECTED_SHA="$revision" bash "$SCREENER_UPDATE_SCRIPT"; then
  printf '%s\n' "$exact" >"$SCREENER_RELEASE_STATE_DIR/failed-descriptor"
  die "activation failed and was suppressed until a new signed descriptor is published"
fi

printf '%s\n' "$exact" >"$SCREENER_RELEASE_STATE_DIR/managed-descriptor"
rm -f "$SCREENER_RELEASE_STATE_DIR/failed-descriptor"
log "activated $revision from authenticated descriptor $exact"
