#!/usr/bin/env bash
# Bootstrap has no secret permissions. Armed/sealed boots must already be ready.
set -euo pipefail
umask 077
ulimit -c 0
readonly ROOT=/opt/sn118-collector
readonly STATE=/var/lib/sn118-collector-key-ceremony
readonly ROLE='${role}'
readonly PROJECT='${project}'
readonly REVISION='${git_revision}'
readonly METADATA=http://metadata.google.internal/computeMetadata/v1
readonly BOOTSTRAP_USER=collector-bootstrap
readonly UV_VERSION=0.11.28
readonly UV_SHA256=49fe42df9f42056037473f3876adec1615709b57d3470ed39178ff420f3afb9f
readonly ORIGIN=https://github.com/ditto-assistant/ditto-subnet.git

[[ "$${ROLE}" == registration || "$${ROLE}" == transfer ]]
[[ "$${PROJECT}" =~ ^[a-z][a-z0-9-]{4,28}[a-z0-9]$ ]]
[[ "$${REVISION}" =~ ^[0-9a-f]{40}$ ]]
if [[ -f "$${STATE}/ready" ]]; then
  test "$(git -C "$${ROOT}" rev-parse HEAD)" = "$${REVISION}"
  git -C "$${ROOT}" diff-index --quiet HEAD --
  test -x "$${ROOT}/.venv/bin/python"
  exit 0
fi
# A skipped bootstrap phase cannot install packages or generate keys.
tags="$(curl --fail --silent --show-error -H 'Metadata-Flavor: Google' "$${METADATA}/instance/tags")"
grep -Fq "collector-$${ROLE}-bootstrap" <<<"$${tags}"
install -d -m 0700 "$${ROOT}" "$${STATE}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git procps python3 python3-venv
id -u "$${BOOTSTRAP_USER}" >/dev/null 2>&1 || useradd --system --home-dir "$${ROOT}" --shell /usr/sbin/nologin "$${BOOTSTRAP_USER}"
chown "$${BOOTSTRAP_USER}:$${BOOTSTRAP_USER}" "$${ROOT}"
runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" init --quiet
if origin="$(runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" remote get-url --all origin 2>/dev/null)"; then
  test "$${origin}" = "$${ORIGIN}"
else
  runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" remote add origin "$${ORIGIN}"
fi
# A failed first fetch leaves no HEAD and may resume against the validated
# origin. Existing source must already match this pin and stay unmodified;
# never replace a different checkout or erase ceremony state during recovery.
if head="$(runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" rev-parse --verify HEAD 2>/dev/null)"; then
  test "$${head}" = "$${REVISION}"
  runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" diff-index --quiet HEAD --
fi
runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" fetch --filter=blob:none origin refs/heads/main:refs/remotes/origin/main
runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" merge-base --is-ancestor "$${REVISION}" refs/remotes/origin/main
runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" checkout --detach "$${REVISION}"
runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" diff-index --quiet HEAD --
runuser -u "$${BOOTSTRAP_USER}" -- python3 -m venv "$${ROOT}/bootstrap-venv"
printf 'uv==%s --hash=sha256:%s\n' "$${UV_VERSION}" "$${UV_SHA256}" >"$${ROOT}/uv-requirements.txt"
chown "$${BOOTSTRAP_USER}:$${BOOTSTRAP_USER}" "$${ROOT}/uv-requirements.txt"
runuser -u "$${BOOTSTRAP_USER}" -- "$${ROOT}/bootstrap-venv/bin/pip" install --disable-pip-version-check --no-deps --only-binary=:all: --require-hashes -r "$${ROOT}/uv-requirements.txt"
runuser -u "$${BOOTSTRAP_USER}" -- "$${ROOT}/bootstrap-venv/bin/uv" sync --directory "$${ROOT}" --frozen --no-dev
"$${ROOT}/.venv/bin/python" -I -c 'import importlib.metadata; assert importlib.metadata.version("bittensor") == "10.5.0"'
pkill -KILL -u "$(id -u "$${BOOTSTRAP_USER}")" >/dev/null 2>&1 || true
rm -rf "$${ROOT}/.cache" "$${ROOT}/bootstrap-venv" "$${ROOT}/uv-requirements.txt"
chown -R root:root "$${ROOT}"
userdel "$${BOOTSTRAP_USER}"
# API TLS hostname remains intact. Armed/sealed firewall allows only this VIP.
printf '\n199.36.153.4 secretmanager.googleapis.com\n' >>/etc/hosts

cat >/usr/local/sbin/collector-delegate-ceremony <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
umask 077
ulimit -c 0
readonly ROOT=/opt/sn118-collector
test "$(git -C "$${ROOT}" rev-parse HEAD)" = '${git_revision}'
git -C "$${ROOT}" diff-index --quiet HEAD --
test -z "$(git -C "$${ROOT}" ls-files --others --exclude-standard -- ':!.venv' ':!*.egg-info')"
[[ "$${1:-}" == generate || "$${1:-}" == verify ]]
offline=(${offline_addresses})
args=()
for address in "$${offline[@]}"; do args+=(--forbidden-address "$${address}"); done
exec "$${ROOT}/.venv/bin/python" -I "$${ROOT}/scripts/collector_delegate_key.py" \
  --project '${project}' --role '${role}' --mode "$1" "$${args[@]}" \
  --confirm "$${1^^} GCP COLLECTOR $${ROLE^^} DELEGATE"
RUNNER
# The wrapper needs its own constant role, not an inherited shell environment.
sed -i '/readonly ROOT=/a readonly ROLE=${role}' /usr/local/sbin/collector-delegate-ceremony
chmod 0700 /usr/local/sbin/collector-delegate-ceremony
# Public-only ceremony state is preserved. No signer journals or units/timers
# are initialized/installed here, and no activation marker is created.
printf '%s\n' "$${REVISION}" >"$${STATE}/ready"
chmod 0600 "$${STATE}/ready"
