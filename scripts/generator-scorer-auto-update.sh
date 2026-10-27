#!/usr/bin/env bash
# Root-owned timer: authenticate a release, prepare it, then drain and activate.
set -euo pipefail
ROOT="${GENERATOR_SCORER_RELEASE_ROOT:-/opt/ditto-generator-scorer}"
REPOSITORY=ghcr.io/ditto-assistant/ditto-subnet-stack
CHANNEL="$REPOSITORY:generator-scorer-stable-1"
UNIT=ditto-generator-scorer
install -d -m 0755 "$ROOT/releases"
exec 9>"$ROOT/update.lock"
flock -n 9 || exit 0
docker pull --platform linux/amd64 "$CHANNEL" >/dev/null
exact="$(docker image inspect --format '{{range .RepoDigests}}{{println .}}{{end}}' "$CHANNEL" | awk -v prefix="$REPOSITORY@" 'index($0,prefix)==1 {print; exit}')"
[[ "$exact" =~ ^ghcr\.io/ditto-assistant/ditto-subnet-stack@sha256:[0-9a-f]{64}$ ]]
cosign verify --certificate-identity 'https://github.com/ditto-assistant/ditto-subnet/.github/workflows/release.yml@refs/heads/main' \
  --certificate-oidc-issuer https://token.actions.githubusercontent.com "$exact" >/dev/null
revision="$(docker image inspect --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}' "$exact")"
[[ "$revision" =~ ^[0-9a-f]{40}$ ]]
test "$(docker image inspect --format '{{ index .Config.Labels "io.heyditto.generator-scorer.update-protocol" }}' "$exact")" = 1
if [ -f "$ROOT/failed-candidate" ] && [ "$(cat "$ROOT/failed-candidate")" = "$exact" ]; then
  echo "generator-scorer-auto-update: failed candidate suppressed: $exact"
  exit 0
fi
destination="$ROOT/releases/$revision"
staging="$(mktemp -d "$ROOT/releases/.staging.XXXXXX")"
container=''
cleanup() { [ -z "$container" ] || docker rm -f "$container" >/dev/null 2>&1 || true; rm -rf -- "$staging"; }
trap cleanup EXIT
container="$(docker create "$exact")"
docker cp "$container:/release/." "$staging/"
docker rm "$container" >/dev/null
container=''
# Only signed release code runs; the scorer bearer is never loaded by this unit.
"$staging/challenge-scorer" version >"$staging/identity.json"
python3 - "$staging/identity.json" "$revision" <<'PY'
import json, re, subprocess, sys
identity = json.load(open(sys.argv[1]))
assert identity['revision'] == sys.argv[2]
assert identity['update_protocol'] == 1
assert identity['max_cases'] >= 80
for key in ('builder_image', 'runner_image'):
    image = identity[key]
    assert re.fullmatch(r'[a-z0-9./:_-]+@sha256:[0-9a-f]{64}', image), image
    subprocess.run(['docker', 'pull', image], check=True)
PY
if [ -d "$destination" ]; then
  cmp "$staging/challenge-scorer" "$destination/challenge-scorer"
  cmp "$staging/updater.sh" "$destination/updater.sh"
else
  chmod 0755 "$staging" "$staging/challenge-scorer" "$staging/updater.sh"
  mv "$staging" "$destination"
fi
previous="$(readlink -f "$ROOT/last-good" 2>/dev/null || true)"
running_binary() {
  local pid
  pid="$(systemctl show "$UNIT" --property MainPID --value)"
  [ "$pid" != 0 ] && readlink -f "/proc/$pid/exe"
}
if [ "$previous" = "$destination" ] && [ "$(running_binary || true)" = "$destination/challenge-scorer" ]; then
  echo "generator-scorer-auto-update: already running $revision"
  exit 0
fi
activate() { rm -f "$ROOT/current.next"; ln -s "$1" "$ROOT/current.next"; mv -Tf "$ROOT/current.next" "$ROOT/current"; }
initial_pid="$(systemctl show "$UNIT" --property MainPID --value)"
activate "$destination"
# SIGUSR1 stops claiming, completes the current lease and exits. Restart=always
# starts the new binary through current. The old bootstrap binary needs one
# supervised Ansible restart before enabling this timer.
if systemctl is-active --quiet "$UNIT"; then
  if ! systemctl kill --kill-whom=main --signal=SIGUSR1 "$UNIT"; then
    [ -z "$previous" ] || activate "$previous"
    exit 1
  fi
else
  # An inactive service has no lease to drain. Let the adoption/rollback path
  # handle a failed start rather than leaving a changed link without recovery.
  systemctl start "$UNIT" || true
fi
adopted=false
startup_deadline=-1
for ((attempt=0; attempt<${GENERATOR_SCORER_ADOPTION_POLLS:-840}; attempt++)); do
  if systemctl is-active --quiet "$UNIT" && [ "$(running_binary || true)" = "$destination/challenge-scorer" ]; then
    adopted_pid="$(systemctl show "$UNIT" --property MainPID --value)"
    sleep 10
    if [ "$adopted_pid" = "$(systemctl show "$UNIT" --property MainPID --value)" ] && systemctl is-active --quiet "$UNIT" && [ "$(running_binary || true)" = "$destination/challenge-scorer" ]; then adopted=true; break; fi
  fi
  # The long budget is only for the old process to finish its lease. Once
  # it exits, a crash loop gets one minute, not the entire drain deadline.
  if [ "$(systemctl show "$UNIT" --property MainPID --value)" != "$initial_pid" ] || ! systemctl is-active --quiet "$UNIT"; then
    if [ "$startup_deadline" -lt 0 ]; then startup_deadline=$((SECONDS + ${GENERATOR_SCORER_STARTUP_SECONDS:-60})); fi
    if [ "$SECONDS" -ge "$startup_deadline" ]; then break; fi
  fi
  sleep 5
done
if [ "$adopted" != true ]; then
  printf '%s\n' "$exact" >"$ROOT/failed-candidate.next"
  mv -f "$ROOT/failed-candidate.next" "$ROOT/failed-candidate"
  if [ -n "$previous" ]; then
    activate "$previous"
    systemctl kill --kill-whom=main --signal=SIGUSR1 "$UNIT" || true
    systemctl start "$UNIT" || true
  fi
  echo 'generator-scorer-auto-update: activation failed; rollback requested and candidate suppressed' >&2
  exit 1
fi
# Preserve the previous version for recovery; self-refresh only after adoption.
updater_path="${GENERATOR_SCORER_UPDATER_PATH:-/usr/local/sbin/ditto-generator-scorer-auto-update}"
install -m 0755 "$destination/updater.sh" "$updater_path.next"
mv -f "$updater_path.next" "$updater_path"
rm -f "$ROOT/last-good.next"
ln -s "$destination" "$ROOT/last-good.next"
mv -Tf "$ROOT/last-good.next" "$ROOT/last-good"
rm -f "$ROOT/failed-candidate"
printf '%s\n' "$exact" >"$ROOT/active-descriptor"
echo "generator-scorer-auto-update: active revision=$revision descriptor=$exact"
