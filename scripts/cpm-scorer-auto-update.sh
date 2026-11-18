#!/usr/bin/env bash
# Root-owned timer: authenticate a signed cpm-scorer image, check its identity,
# then drain the running scorer and activate the image. The same protocol as
# generator-scorer-auto-update.sh, except that a release is a container image
# that ditto-cpm-scorer.service runs (current.env names its exact digest)
# rather than a native bundle under releases/.
set -euo pipefail
ROOT="${CPM_SCORER_RELEASE_ROOT:-/opt/ditto-cpm-scorer}"
REPOSITORY=ghcr.io/ditto-assistant/ditto-subnet-stack
CHANNEL="$REPOSITORY:cpm-scorer-stable-1"
# The timer follows the channel. The supervised Ansible bootstrap pins one
# immutable release tag and the revision it must carry.
REF="${CPM_SCORER_REF:-$CHANNEL}"
EXPECTED_REVISION="${CPM_SCORER_EXPECTED_REVISION:-}"
UNIT=ditto-cpm-scorer
CONTAINER=ditto-cpm-scorer
if [[ "$REF" != "$CHANNEL" && ! "$REF" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-stack:cpm-scorer-sha-[0-9a-f]{40}$ ]]; then
  echo "cpm-scorer-auto-update: refusing reference $REF" >&2
  exit 1
fi
[[ -z "$EXPECTED_REVISION" || "$EXPECTED_REVISION" =~ ^[0-9a-f]{40}$ ]]
install -d -m 0755 "$ROOT"
exec 9>"$ROOT/update.lock"
flock -n 9 || exit 0
docker pull --platform linux/amd64 "$REF" >/dev/null
exact="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$REF" | awk -v prefix="$REPOSITORY@" 'index($0,prefix)==1 {print; exit}')"
[[ "$exact" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$ ]]
cosign verify --certificate-identity 'https://github.com/ditto-assistant/ditto-subnet/.github/workflows/release.yml@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com "$exact" >/dev/null
label() { docker image inspect --format "{{ index .Config.Labels \"$1\" }}" "$exact"; }
revision="$(label org.opencontainers.image.revision)"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
test "$(label io.heyditto.cpm-scorer.update-protocol)" = 1
if [ -n "$EXPECTED_REVISION" ] && [ "$revision" != "$EXPECTED_REVISION" ]; then
  echo "cpm-scorer-auto-update: $REF carries revision $revision, expected $EXPECTED_REVISION" >&2
  exit 1
fi
if [ -f "$ROOT/failed-candidate" ] && [ "$(cat "$ROOT/failed-candidate")" = "$exact" ] && [ -n "$(find "$ROOT/failed-candidate" -mmin -1440 -print -quit)" ]; then
  echo "cpm-scorer-auto-update: failed candidate suppressed: $exact"
  exit 0
fi
staging="$(mktemp "$ROOT/.updater.XXXXXX")"
trap 'rm -f -- "$staging"' EXIT
# Only signed release code runs, offline and without the scorer bearer: the
# token file is mounted by the service unit, never by this updater.
identity="$(docker run --rm --network none --pull never "$exact" version)"
python3 - "$revision" "$identity" <<'PY'
import json, sys
identity = json.loads(sys.argv[2])
assert identity['revision'] == sys.argv[1], identity
assert isinstance(identity['cpm_score_version'], int) and identity['cpm_score_version'] >= 1, identity
assert identity['scorer_version'].endswith('+' + sys.argv[1]), identity
PY
docker run --rm --network none --pull never --entrypoint cat "$exact" /opt/cpm-scorer/updater.sh >"$staging"
bash -n "$staging"

image_of() { sed -n 's/^CPM_SCORER_IMAGE=//p' "$1" 2>/dev/null || true; }
image_id() { docker image inspect --format '{{.Id}}' "$1"; }
running_image() { docker inspect --format '{{.Image}}' "$CONTAINER" 2>/dev/null; }
container_id() { docker inspect --format '{{.Id}}' "$CONTAINER" 2>/dev/null || true; }
previous="$(image_of "$ROOT/last-good.env")"
want="$(image_id "$exact")"
if [ "$previous" = "$exact" ] && systemctl is-active --quiet "$UNIT" && [ "$(running_image || true)" = "$want" ]; then
  echo "cpm-scorer-auto-update: already running $revision"
  exit 0
fi
activate() {
  printf 'CPM_SCORER_IMAGE=%s\n' "$1" >"$ROOT/current.env.next"
  mv -f "$ROOT/current.env.next" "$ROOT/current.env"
}
restart_unit() { systemctl reset-failed "$UNIT" 2>/dev/null || true; systemctl start "$UNIT" || true; }
initial_container="$(container_id)"
activate "$exact"
# SIGUSR1 stops leasing, posts the run in hand and exits 0 (cpm-scorer serve).
# Restart=always then starts the image current.env names.
if systemctl is-active --quiet "$UNIT"; then
  if ! docker kill --signal=SIGUSR1 "$CONTAINER" >/dev/null; then
    [ -z "$previous" ] || activate "$previous"
    exit 1
  fi
else
  # An inactive service has no lease to drain. Let the adoption/rollback path
  # handle a failed start rather than leaving a changed image without recovery.
  restart_unit
fi
adopted=false
startup_deadline=-1
# The drain budget covers the longest run Platform will lease (its default
# lease_ttl_seconds is 4 h). Once the old container exits, a crash loop gets
# CPM_SCORER_STARTUP_SECONDS, not the whole drain budget.
for ((attempt=0; attempt<${CPM_SCORER_ADOPTION_POLLS:-2880}; attempt++)); do
  if systemctl is-active --quiet "$UNIT" && [ "$(running_image || true)" = "$want" ]; then
    adopted_container="$(container_id)"
    # Long enough for torch to import and the scorer to reach the GPU and its
    # token: a container that dies on startup changes ID or stops here.
    sleep "${CPM_SCORER_SETTLE_SECONDS:-45}"
    if [ "$adopted_container" = "$(container_id)" ] && systemctl is-active --quiet "$UNIT" && [ "$(running_image || true)" = "$want" ]; then adopted=true; break; fi
  fi
  if [ "$(container_id)" != "$initial_container" ] || ! systemctl is-active --quiet "$UNIT"; then
    if [ "$startup_deadline" -lt 0 ]; then startup_deadline=$((SECONDS + ${CPM_SCORER_STARTUP_SECONDS:-180})); fi
    if [ "$SECONDS" -ge "$startup_deadline" ]; then break; fi
  fi
  sleep 5
done
if [ "$adopted" != true ]; then
  if [ -n "$previous" ]; then
    activate "$previous"
    docker kill --signal=SIGUSR1 "$CONTAINER" >/dev/null 2>&1 || true
    restart_unit
  fi
  # Recording diagnostics must never prevent recovery (for example when the
  # marker path is unwritable or the filesystem is full).
  if ! { printf '%s\n' "$exact" >"$ROOT/failed-candidate.next" && mv -f "$ROOT/failed-candidate.next" "$ROOT/failed-candidate"; }; then
    echo 'cpm-scorer-auto-update: could not record failed candidate' >&2
  fi
  echo 'cpm-scorer-auto-update: activation failed; rollback requested' >&2
  exit 1
fi
# Self-refresh only after adoption, then keep the adopted and previous images
# (the unit runs with --pull never, so a rollback needs the previous image
# local) and prune older untagged releases from the root volume.
updater_path="${CPM_SCORER_UPDATER_PATH:-/usr/local/sbin/ditto-cpm-scorer-auto-update}"
install -m 0755 "$staging" "$updater_path.next"
mv -f "$updater_path.next" "$updater_path"
cp "$ROOT/current.env" "$ROOT/last-good.env.next"
mv -f "$ROOT/last-good.env.next" "$ROOT/last-good.env"
docker tag "$exact" ditto-cpm-scorer-local:current
if [ -n "$previous" ] && [ "$previous" != "$exact" ]; then
  docker tag "$previous" ditto-cpm-scorer-local:previous 2>/dev/null || true
fi
docker image prune --force --filter label=io.heyditto.cpm-scorer.update-protocol >/dev/null 2>&1 || true
rm -f "$ROOT/failed-candidate"
printf '%s\n' "$exact" >"$ROOT/active-descriptor"
echo "cpm-scorer-auto-update: active revision=$revision descriptor=$exact"
