#!/usr/bin/env bash
# Bootstrap the disposable production-hotkey generator. No mnemonic exists
# while this script has general NAT egress, and the VM service account has no
# Secret Manager grant until a separate protected Terraform arm apply.
set -euo pipefail
umask 077

exec > >(tee -a /var/log/validator-hotkey-admin-bootstrap.log) 2>&1
echo "==> validator hotkey admin bootstrap $(date -u +%FT%TZ)"

readonly PROJECT='${project}'
readonly SECRET='${secret}'
readonly REPOSITORY='${repository}'
readonly GIT_REVISION='${git_revision}'
readonly UV_SHA256='${uv_sha256}'
readonly UV_VERSION='${uv_version}'
readonly ARMED_TAG='${armed_tag}'
readonly BOOTSTRAP_USER=hotkey-bootstrap
readonly ROOT=/opt/ditto-hotkey-admin
readonly SOURCE="$${ROOT}/src"
readonly STATE=/var/lib/ditto-hotkey-admin

[[ "$${GIT_REVISION}" =~ ^[0-9a-f]{40}$ ]]
install -d -m 0700 "$${ROOT}" "$${STATE}"

# GCE startup scripts run after every boot. Preserve a completed bootstrap if
# the instance is restarted after general egress has been removed.
if [[ -f "$${STATE}/ready" ]]; then
  command -v gcloud >/dev/null
  test -x /usr/local/sbin/generate-validator-hotkey
  test "$(git -C "$${SOURCE}" remote get-url origin)" = "$${REPOSITORY}"
  test "$(git -C "$${SOURCE}" rev-parse HEAD)" = "$${GIT_REVISION}"
  echo "ready: existing exact bootstrap verified"
  exit 0
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl git procps python3 python3-venv
command -v gcloud >/dev/null

id -u "$${BOOTSTRAP_USER}" >/dev/null 2>&1 || \
  useradd --system --home-dir "$${ROOT}" --shell /usr/sbin/nologin \
    "$${BOOTSTRAP_USER}"
chown -R "$${BOOTSTRAP_USER}:$${BOOTSTRAP_USER}" "$${ROOT}"
rm -rf "$${SOURCE}" "$${ROOT}/bootstrap-venv" "$${ROOT}/project-venv"
runuser -u "$${BOOTSTRAP_USER}" -- git -C "$${ROOT}" init --quiet src
runuser -u "$${BOOTSTRAP_USER}" -- \
  git -C "$${SOURCE}" remote add origin "$${REPOSITORY}"
runuser -u "$${BOOTSTRAP_USER}" -- \
  git -C "$${SOURCE}" fetch --force --filter=blob:none origin \
  refs/heads/main:refs/remotes/origin/main
runuser -u "$${BOOTSTRAP_USER}" -- \
  git -C "$${SOURCE}" cat-file -e "$${GIT_REVISION}^{commit}"
runuser -u "$${BOOTSTRAP_USER}" -- \
  git -C "$${SOURCE}" merge-base --is-ancestor \
  "$${GIT_REVISION}" refs/remotes/origin/main
runuser -u "$${BOOTSTRAP_USER}" -- \
  git -C "$${SOURCE}" checkout --detach "$${GIT_REVISION}"
test "$(runuser -u "$${BOOTSTRAP_USER}" -- \
  git -C "$${SOURCE}" remote get-url origin)" = "$${REPOSITORY}"
test "$(runuser -u "$${BOOTSTRAP_USER}" -- \
  git -C "$${SOURCE}" rev-parse HEAD)" = "$${GIT_REVISION}"

runuser -u "$${BOOTSTRAP_USER}" -- \
  python3 -m venv "$${ROOT}/bootstrap-venv"
readonly UV_REQUIREMENTS="$${ROOT}/uv-requirements.txt"
printf 'uv==%s --hash=sha256:%s\n' "$${UV_VERSION}" "$${UV_SHA256}" > \
  "$${UV_REQUIREMENTS}"
chown "$${BOOTSTRAP_USER}:$${BOOTSTRAP_USER}" "$${UV_REQUIREMENTS}"
chmod 0400 "$${UV_REQUIREMENTS}"
runuser -u "$${BOOTSTRAP_USER}" -- \
  "$${ROOT}/bootstrap-venv/bin/pip" install \
  --disable-pip-version-check --no-deps --only-binary=:all: \
  --require-hashes -r "$${UV_REQUIREMENTS}"
rm -f "$${UV_REQUIREMENTS}"
runuser -u "$${BOOTSTRAP_USER}" -- env \
  UV_PROJECT_ENVIRONMENT="$${ROOT}/project-venv" \
  "$${ROOT}/bootstrap-venv/bin/uv" sync \
  --directory "$${SOURCE}" --frozen --no-dev

pkill -KILL -u "$(id -u "$${BOOTSTRAP_USER}")" >/dev/null 2>&1 || true
rm -rf "$${ROOT}/.cache"
chown -R root:root "$${ROOT}"
userdel "$${BOOTSTRAP_USER}"

# Resolve Secret Manager and the IAM Credentials allowed-locations preflight
# used by current gcloud through Google's VPC-SC-compatible restricted VIP once
# the Terraform arm phase removes general internet egress.
cat >>/etc/hosts <<'HOSTS'
199.36.153.4 secretmanager.googleapis.com iamcredentials.googleapis.com
199.36.153.5 secretmanager.googleapis.com iamcredentials.googleapis.com
199.36.153.6 secretmanager.googleapis.com iamcredentials.googleapis.com
199.36.153.7 secretmanager.googleapis.com iamcredentials.googleapis.com
HOSTS

install -d -m 0755 /usr/local/libexec
cat >/usr/local/libexec/run-validator-hotkey-generator <<'RUNNER'
#!/usr/bin/env bash
set -euo pipefail
umask 077
ulimit -c 0

readonly ROOT=/opt/ditto-hotkey-admin
readonly SOURCE="$${ROOT}/src"
readonly STATE=/var/lib/ditto-hotkey-admin
readonly METADATA=http://metadata.google.internal/computeMetadata/v1

tags="$(curl --fail --silent --show-error \
  -H 'Metadata-Flavor: Google' "$${METADATA}/instance/tags")"
grep -Fq '__ARMED_TAG__' <<<"$${tags}" || {
  echo 'refusing generation: Terraform has not armed this instance' >&2
  exit 2
}

test "$(git -C "$${SOURCE}" remote get-url origin)" = \
  'https://github.com/ditto-assistant/ditto-subnet.git'
git -C "$${SOURCE}" merge-base --is-ancestor \
  '__GIT_REVISION__' refs/remotes/origin/main
test "$(git -C "$${SOURCE}" rev-parse HEAD)" = '__GIT_REVISION__'
git -C "$${SOURCE}" diff-index --quiet HEAD --
test -z "$(git -C "$${SOURCE}" ls-files --others --exclude-standard)"

export CLOUDSDK_CORE_DISABLE_PROMPTS=1
export CLOUDSDK_CONFIG="$${STATE}/gcloud"
export UV_OFFLINE=1
export UV_PROJECT_ENVIRONMENT="$${ROOT}/project-venv"

exec "$${ROOT}/bootstrap-venv/bin/uv" run \
  --directory "$${SOURCE}" --frozen --no-dev \
  python infra/ansible/scripts/generate_validator_hotkey.py \
  --project '__PROJECT__' \
  --secret '__SECRET__' \
  --result-file "$${STATE}/result.env" \
  --lock-file "$${STATE}/generator.lock" \
  --confirm 'CREATE GCP VALIDATOR HOTKEY'
RUNNER
sed -i \
  -e "s/__GIT_REVISION__/$${GIT_REVISION}/g" \
  -e "s/__PROJECT__/$${PROJECT}/g" \
  -e "s/__SECRET__/$${SECRET}/g" \
  -e "s/__ARMED_TAG__/$${ARMED_TAG}/g" \
  /usr/local/libexec/run-validator-hotkey-generator
chmod 0700 /usr/local/libexec/run-validator-hotkey-generator

cat >/usr/local/sbin/generate-validator-hotkey <<'WRAPPER'
#!/usr/bin/env bash
set -euo pipefail
exec flock --exclusive --nonblock \
  /var/lib/ditto-hotkey-admin/operator.lock \
  /usr/local/libexec/run-validator-hotkey-generator
WRAPPER
chmod 0700 /usr/local/sbin/generate-validator-hotkey

touch "$${STATE}/ready"
chmod 0600 "$${STATE}/ready"
echo "ready: arm through protected Terraform before running the generator"
