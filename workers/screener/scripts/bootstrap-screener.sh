#!/usr/bin/env bash
set -euo pipefail

# Zero-touch first-boot provisioning for an autoscaled screener fleet instance.
#
# Invoked by the GCE instance-template startup script (infra repo,
# terraform/envs/gcp-platform/files/screener-fleet-startup.sh.tpl), which has
# already cloned this repository (read-only deploy key from Secret Manager) and
# exports the configuration below. Runs as root, is idempotent (marker file),
# and finishes by handing off to scripts/update-screener.sh — the same
# exact-commit updater the deploy workflow uses — so the definition of
# "healthy worker" lives in exactly one place.
#
# Pet-VM parity: the layout it produces (/opt/ditto/screener, deploy:ditto,
# screener.env, systemd unit) is byte-compatible with the hand-provisioned
# ditto-screener-prod host, which is what makes the label-driven deploy
# workflow able to treat pet and fleet instances identically.
#
# GOLDEN-IMAGE BAKE MODE (SCREENER_BAKE_ONLY=1): runs ONLY the slow,
# secret-free provisioning — base packages, Docker, the IMDS guard, uv, the
# service user/layout, a warm checkout + synced venv — then exits before any
# secret is fetched or the worker is started. Packer snapshots the result into
# the `ditto-screener-fleet` image family (see packer/screener-fleet.pkr.hcl).
# A fleet instance booted from that image runs this same script in normal mode;
# its idempotent guards skip everything already baked, so first boot goes
# straight to fetching secrets + the fast updater — cutting time-to-first-claim
# from ~5-10 min to ~1-2 min, which is what lets autoscaling relieve the pet VM
# promptly during a burst. NO SECRET is ever written into the image: the deploy
# key, mnemonic, and API token are all fetched at runtime only.

SCREENER_REPOSITORY_URL="${SCREENER_REPOSITORY_URL:-git@github.com:ditto-assistant/ditto-subnet.git}"
# Readiness port for MIG autohealing (0/unset disables the server). Threaded
# into screener.env below so the worker binds it.
SCREENER_READINESS_PORT="${SCREENER_READINESS_PORT:-0}"
# Bake mode (image build) vs normal first boot. Bake seeds the checkout from an
# uploaded copy (SCREENER_BAKE_SRC) instead of cloning, so no key is needed.
SCREENER_BAKE_ONLY="${SCREENER_BAKE_ONLY:-0}"
SCREENER_BAKE_SRC="${SCREENER_BAKE_SRC:-}"

# Runtime-only configuration. Not required (and not present) during a bake.
if [[ "$SCREENER_BAKE_ONLY" != "1" ]]; then
  SCREENER_GCP_PROJECT="${SCREENER_GCP_PROJECT:?missing SCREENER_GCP_PROJECT}"
  SCREENER_PLATFORM_API_URL="${SCREENER_PLATFORM_API_URL:?missing SCREENER_PLATFORM_API_URL}"
  SCREENER_HOTKEY="${SCREENER_HOTKEY:?missing SCREENER_HOTKEY}"
  NETUID="${NETUID:?missing NETUID}"
  SCREENER_MNEMONIC_SECRET="${SCREENER_MNEMONIC_SECRET:?missing SCREENER_MNEMONIC_SECRET}"
  SCREENER_API_TOKEN_SECRET="${SCREENER_API_TOKEN_SECRET:?missing SCREENER_API_TOKEN_SECRET}"
  SCREENER_DEPLOY_KEY_FILE="${SCREENER_DEPLOY_KEY_FILE:?missing SCREENER_DEPLOY_KEY_FILE}"
  SCREENER_EXPECTED_SHA="${SCREENER_EXPECTED_SHA:?missing SCREENER_EXPECTED_SHA}"
  if [[ ! "$SCREENER_EXPECTED_SHA" =~ ^[0-9a-f]{40}$ ]]; then
    echo "SCREENER_EXPECTED_SHA must be an immutable released commit" >&2
    exit 2
  fi
fi

SCREENER_ROOT=/opt/ditto/screener
SCREENER_USER=deploy
SCREENER_GROUP=ditto
LOGS_DIR=/opt/ditto/logs
SECRETS_DIR=/opt/ditto/secrets
STATE_DIR="$SCREENER_ROOT/state"
MARKER=/opt/ditto/.screener-bootstrapped
LOCK_FILE=/opt/ditto/.screener-deploy.lock

checkout="$SCREENER_ROOT/src"
source_dir="$checkout/workers/screener"
env_file="$SCREENER_ROOT/screener.env"

if [[ "${EUID}" -ne 0 ]]; then
  echo "bootstrap-screener.sh must run as root" >&2
  exit 1
fi

if [[ "$SCREENER_BAKE_ONLY" != "1" ]]; then
  install -d -m 0755 /opt/ditto
  if [[ -f "$MARKER" ]]; then
    echo "already bootstrapped ($MARKER exists)"
    exit 0
  fi
  # Hold the deploy lock across the whole mutating body so a scheduled deploy
  # (update-screener.sh over SSH) landing mid-bootstrap serializes behind it
  # instead of racing the checkout / env / unit. We pass the held flag down to
  # the updater we invoke so it does not try to re-acquire (and deadlock).
  exec {lock_fd}>"$LOCK_FILE"
  if ! flock -w 2400 "$lock_fd"; then
    echo "could not acquire deploy lock ($LOCK_FILE) within 40m" >&2
    exit 1
  fi
fi

export DEBIAN_FRONTEND=noninteractive

# --- Base packages + Docker engine (the gate shells out to `docker`) ---------
apt-get update -qq
apt-get install -y -qq git curl ca-certificates gnupg openssl

if ! command -v docker >/dev/null; then
  install -m 0644 /dev/null /usr/share/keyrings/docker.asc
  curl -fsSL https://download.docker.com/linux/debian/gpg >/usr/share/keyrings/docker.asc
  . /etc/os-release
  echo "deb [arch=amd64 signed-by=/usr/share/keyrings/docker.asc] https://download.docker.com/linux/debian ${VERSION_CODENAME} stable" \
    >/etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io
fi
apt-get install -y -qq \
  docker-ce-rootless-extras uidmap slirp4netns fuse-overlayfs dbus-user-session
systemctl enable --now docker

# --- Metadata (IMDS) guard: block metadata API, preserve GCE DNS -------------
# A submission-controlled Dockerfile builds and runs with network access. Left
# open, a hostile RUN step reaches the GCE metadata server and mints the VM's
# attached-SA token (the shared platform runtime SA), which can read platform /
# validator secrets and administer agent objects. Docker container/build traffic
# to the metadata IP traverses the FORWARD path (the DOCKER-USER chain), while
# the host's own gcloud uses OUTPUT. GCE also advertises the same IP as the VM's
# DNS resolver, so the guard must allow TCP/UDP 53 before dropping other traffic
# or every clean Docker build loses DNS.
# Installed as a oneshot that re-applies after docker/iptables restarts + reboot.
apt-get install -y -qq iptables
install -m 0755 /dev/stdin /usr/local/sbin/ditto-imds-guard <<'GUARD'
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
# traverses OUTPUT, not DOCKER-USER. Keep root-owned host administration able
# to use metadata while applying the same DNS-only policy to non-root callers.
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
cat >/etc/systemd/system/ditto-imds-guard.service <<'UNIT'
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
systemctl daemon-reload
systemctl enable ditto-imds-guard.service
# Apply now only when docker is already running (a bake builder may lack the
# DOCKER-USER chain until docker starts; the unit re-applies it on every boot).
systemctl start ditto-imds-guard.service 2>/dev/null || true

# gcloud ships on GCE Debian images; the updater needs it for Secret Manager.
command -v gcloud >/dev/null || {
  echo "gcloud is required (expected on GCE Debian images)" >&2
  exit 1
}

# --- uv (the worker runs from a uv-managed venv; updater expects this path) ---
if [[ ! -x /usr/local/bin/uv ]]; then
  # mktemp, not a predictable /tmp path: this runs as root, so a hardcoded
  # /tmp/uv-install.sh is a symlink/TOCTOU foothold for a local user.
  uv_installer="$(mktemp)"
  trap 'rm -f "$uv_installer"' EXIT
  curl -fsSL \
    https://github.com/astral-sh/uv/releases/download/0.11.28/uv-installer.sh \
    -o "$uv_installer"
  echo "b7b3fe80cad1142a2a5794050b7db7b3291d1bac1423b0732571dd9366e8ca8b  $uv_installer" \
    | sha256sum --check
  UV_INSTALL_DIR=/usr/local/bin sh "$uv_installer"
  rm -f "$uv_installer"
  trap - EXIT
fi

# --- Service user + directory layout (matches the pet VM / updater) ----------
getent group "$SCREENER_GROUP" >/dev/null || groupadd --system "$SCREENER_GROUP"
if ! id "$SCREENER_USER" >/dev/null 2>&1; then
  useradd --create-home --shell /bin/bash --gid "$SCREENER_GROUP" "$SCREENER_USER"
fi
install -d -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0755 "$SCREENER_ROOT"
install -d -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0750 "$STATE_DIR"
install -d -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0750 "$LOGS_DIR"
install -d -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0750 "$SECRETS_DIR"

# github.com host keys — PINNED (from https://api.github.com/meta ssh_keys),
# not ssh-keyscan. ssh-keyscan is trust-on-first-use, so a network attacker
# could impersonate github.com during the deploy user's git-over-ssh fetch and
# steer the root-run updater onto a malicious checkout. Pinning closes that TOFU
# window; refresh these lines if GitHub rotates its host keys.
ssh_dir="/home/$SCREENER_USER/.ssh"
install -d -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0700 "$ssh_dir"
cat >"$ssh_dir/known_hosts" <<'KNOWN_HOSTS'
github.com ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIOMqqnkVzrm0SdG6UOoqKLsabgH5C9okWi0dh2l9GKJl
github.com ecdsa-sha2-nistp256 AAAAE2VjZHNhLXNoYTItbmlzdHAyNTYAAAAIbmlzdHAyNTYAAABBBEmKSENjQEezOmxkZMy7opKgwFB9nkt5YRrYMjNuG5N87uRgg6CLrbo5wAdT/y6v0mKV0U2w0WZ2YB/++Tpockg=
github.com ssh-rsa AAAAB3NzaC1yc2EAAAADAQABAAABgQCj7ndNxQowgcQnjshcLrqPEiiphnt+VTTvDP6mHBL9j1aNUkY4Ue1gvwnGLVlOhGeYrnZaMgRK6+PKCUXaDbC7qtbW8gIkhL7aGCsOr/C56SJMy/BCZfxd1nWzAOxSDPgVsmerOBYfNqltV9/hWCqBywINIR+5dIg6JTJ72pcEpEjcYgXkE2YEFXV1JHnsKgbLWNlhScqb2UmyRkQyytRLtL+38TGxkxCflmO+5Z8CSSNY7GidjMIZ7Q4zMjA2n1nGrlTDkzwDCsw+wqFPGQA179cnfGWOWRVruj16z6XyvxvjJwbz0wQZ75XK5tKSb7FNyeIEs4TT4jk+S4dhPeAUC5y+bDYirYgM4GC7uEnztnZyaVWQ7B381AK4Qdrwt51ZqExKbQpTUNn+EjqoTwvqNj4kqx5QUCI0ThS/YkOxJCXmPUWZbhjpCg56i+2aB6CmK2JGhn57K5mj0MNdBXA4/WnwH6XoPWJzK5Nyu2zB3nAZp+S5hpQs+p1vN1/wsjk=
KNOWN_HOSTS
chown "$SCREENER_USER:$SCREENER_GROUP" "$ssh_dir/known_hosts"
chmod 0644 "$ssh_dir/known_hosts"

# --- Deploy key (runtime only): the deploy user fetches from the private repo
# on updates. Never installed during a bake, so no key lands in the image.
if [[ "$SCREENER_BAKE_ONLY" != "1" ]]; then
  install -o "$SCREENER_USER" -g "$SCREENER_GROUP" -m 0600 \
    "$SCREENER_DEPLOY_KEY_FILE" "$ssh_dir/id_ed25519"

  # Every networked git command below runs as the deploy user, so ssh must be
  # pointed at the deploy-owned key and pinned known_hosts installed above.
  # State that explicitly rather than inheriting it: `runuser -u` preserves the
  # environment, so a caller that exported GIT_SSH_COMMAND for its own root
  # clone would otherwise hand the deploy user root-only paths. That is not
  # hypothetical — the fleet startup script did exactly that, and the clone
  # failed with "Identity file /root/.ssh/screener_deploy_key not accessible:
  # Permission denied" then "Host key verification failed", leaving every
  # autoscaled node without a checkout and unable to ever become healthy.
  export GIT_SSH_COMMAND="ssh -i $ssh_dir/id_ed25519 -o IdentitiesOnly=yes -o UserKnownHostsFile=$ssh_dir/known_hosts"
fi

# --- Checkout ----------------------------------------------------------------
# Bake seeds it from an uploaded copy (no key needed); runtime clones with the
# deploy key. On a golden image the checkout is already present, so first boot
# skips this and the updater just fast-forwards to the deployed SHA.
if [[ ! -d "$checkout/.git" ]]; then
  if [[ "$SCREENER_BAKE_ONLY" == "1" && -n "$SCREENER_BAKE_SRC" ]]; then
    cp -a "$SCREENER_BAKE_SRC/." "$checkout/"
    chown -R "$SCREENER_USER:$SCREENER_GROUP" "$checkout"
    runuser -u "$SCREENER_USER" -- git config --global --add safe.directory "$checkout"
  else
    runuser -u "$SCREENER_USER" -- git clone --filter=blob:none --sparse \
      "$SCREENER_REPOSITORY_URL" "$checkout"
  fi
fi
runuser -u "$SCREENER_USER" -- git -C "$checkout" sparse-checkout set \
  workers/screener packages/ditto-screening-protocol

# A new clone and a golden-image checkout can both point at mutable or stale
# source. Resolve the reviewed release commit before executing any worker-owned
# installer, synchronizing dependencies, or opening the claim loop.
if [[ "$SCREENER_BAKE_ONLY" != "1" ]]; then
  runuser -u "$SCREENER_USER" -- git -C "$checkout" fetch --prune origin \
    "$SCREENER_EXPECTED_SHA"
  resolved_sha="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" rev-parse FETCH_HEAD)"
  if [[ "$resolved_sha" != "$SCREENER_EXPECTED_SHA" ]]; then
    echo "release SHA resolved to unexpected commit $resolved_sha" >&2
    exit 1
  fi
  runuser -u "$SCREENER_USER" -- git -C "$checkout" reset --hard "$resolved_sha"
fi

# Untrusted Dockerfiles are built only by a daemon running as the unprivileged
# screener identity. The system daemon remains temporarily for additive service
# dependency compatibility, but deploy is deliberately not in its docker group.
rootless_host="$(
  SCREENER_USER="$SCREENER_USER" \
  SCREENER_GROUP="$SCREENER_GROUP" \
  SCREENER_ROOT="$SCREENER_ROOT" \
  bash "$source_dir/scripts/install-rootless-docker.sh"
)"

# --- Bake: warm the venv, then stop (no secrets, no worker) -------------------
if [[ "$SCREENER_BAKE_ONLY" == "1" ]]; then
  runuser -u "$SCREENER_USER" -- env UV_PROJECT_ENVIRONMENT="$source_dir/.venv" \
    /usr/local/bin/uv sync --frozen \
      --reinstall-package ditto-screening-protocol --project "$source_dir"
  runuser -u "$SCREENER_USER" -- \
    "$source_dir/.venv/bin/python" \
    "$source_dir/scripts/verify-installed-signing-contract.py"
  baked_sha="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" rev-parse HEAD)"
  echo "bake complete: base + docker + uv + warm venv at $baked_sha"
  exit 0
fi

# --- Secrets -> protected files / env (values never touch logs) --------------
read_secret() {
  gcloud secrets versions access latest \
    --project="$SCREENER_GCP_PROJECT" --secret="$1"
}

mnemonic="$(read_secret "$SCREENER_MNEMONIC_SECRET")"
api_token="$(read_secret "$SCREENER_API_TOKEN_SECRET")"

# SCREENER_SOURCE_REVIEW_API_KEY_FILE is intentionally absent: the updater
# materializes the OpenRouter key and upserts that line on every run.
tmp="$(mktemp)"
cat >"$tmp" <<EOF
# Written by scripts/bootstrap-screener.sh at first boot — updater-managed
# afterwards (update-screener.sh upserts individual keys). Do not commit.
SCREENER_PLATFORM_API_URL=$SCREENER_PLATFORM_API_URL
NETUID=$NETUID
SCREENER_HOTKEY=$SCREENER_HOTKEY
SCREENER_POLL_SECONDS=30
SCREENER_QUEUE_LIMIT=20
# Per-stage caps — kept in lockstep with the pet VM role
# (infra ansible/roles/screener_worker/defaults) so pet and fleet return the
# SAME verdict for the same submission. build_timeout matches the platform
# first 45 minutes of the screening lease; every stage is clamped to the
# remaining lease. General source review starts only after build and smoke pass,
# so these values are upper bounds rather than independent time reservations.
SCREENER_BUILD_TIMEOUT_SECONDS=2700
SCREENER_RUN_TIMEOUT_SECONDS=120
SCREENER_BUILD_MEMORY=2g
# Language-neutral image builds get a larger but still bounded compiler/linker
# envelope. The built harness keeps the validator-compatible runtime limits.
SCREENER_IMAGE_BUILD_MEMORY=8g
SCREENER_REMOTE_BUILD_MODE=off
# GCE MIG instances are whole-screen overflow capacity. They build and smoke
# locally immediately; they do not enqueue work back onto the Hetzner node and
# wait before doing the same build themselves.
SCREENER_REMOTE_BUILD_TIMEOUT_SECONDS=1500
SCREENER_PIDS_LIMIT=512
SCREENER_DOCKER_HOST=$rootless_host
DOCKER_HOST=$rootless_host
SCREENER_REQUIRE_ROOTLESS_DOCKER=1
SCREENER_EXECUTOR_USER=ditto-builder
SCREENER_EXECUTOR_GROUP=ditto-builder
SCREENER_EXECUTOR_HOME=/var/lib/ditto-screener-docker
# MUST stay >= the platform upload cap (DITTO_MAX_TARBALL_SIZE_BYTES, 20 MiB).
SCREENER_MAX_TARBALL_BYTES=20971520
SCREENER_READINESS_PORT=$SCREENER_READINESS_PORT
SCREENER_MNEMONIC=$mnemonic
SCREENER_API_TOKEN=$api_token
EOF
install -o root -g "$SCREENER_GROUP" -m 0640 "$tmp" "$env_file"
rm -f "$tmp"
unset mnemonic api_token

# --- Hand off to the exact-commit updater ------------------------------------
target_sha="$(runuser -u "$SCREENER_USER" -- git -C "$checkout" rev-parse HEAD)"
test "$target_sha" = "$SCREENER_EXPECTED_SHA"

SCREENER_EXPECTED_SHA="$target_sha" \
  SCREENER_GCP_PROJECT="$SCREENER_GCP_PROJECT" \
  SCREENER_REPOSITORY_URL="$SCREENER_REPOSITORY_URL" \
  SCREENER_DEPLOY_LOCK_HELD=1 \
  bash "$source_dir/scripts/update-screener.sh"

touch "$MARKER"
echo "bootstrap complete: $(hostname) at $target_sha"
