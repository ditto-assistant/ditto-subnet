#!/usr/bin/env bash
set -euo pipefail

# Install a dedicated rootless Docker daemon for untrusted screener builds.
# The screener controls this daemon through a narrow shared Unix-socket group,
# but the daemon itself runs as a separate empty host identity. A daemon escape
# therefore cannot read the worker checkout, platform token, signing material,
# or source-review credential. Never point the worker back at
# /var/run/docker.sock: membership in the rootful docker group is host-root.

SCREENER_USER="${SCREENER_USER:-deploy}"
SCREENER_GROUP="${SCREENER_GROUP:-ditto}"
SCREENER_ROOT="${SCREENER_ROOT:-/opt/ditto/screener}"
SCREENER_ROOTLESS_UNIT="${SCREENER_ROOTLESS_UNIT:-ditto-screener-docker}"
EXECUTOR_USER="${SCREENER_EXECUTOR_USER:-ditto-builder}"
EXECUTOR_GROUP="${SCREENER_EXECUTOR_GROUP:-ditto-builder}"
EXECUTOR_HOME="${SCREENER_EXECUTOR_HOME:-/var/lib/ditto-screener-docker}"

if [[ "${EUID}" -ne 0 ]]; then
  echo "install-rootless-docker.sh must run as root" >&2
  exit 1
fi
for identity in "$SCREENER_USER" "$SCREENER_GROUP" "$EXECUTOR_USER" "$EXECUTOR_GROUP"; do
  if [[ ! "$identity" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    echo "invalid screener executor identity: $identity" >&2
    exit 1
  fi
done
if [[ "$EXECUTOR_HOME" != /var/lib/ditto-screener-docker ]]; then
  echo "SCREENER_EXECUTOR_HOME must be /var/lib/ditto-screener-docker" >&2
  exit 1
fi
if [[ "$SCREENER_ROOT" != /opt/ditto/screener ]]; then
  echo "SCREENER_ROOT must be /opt/ditto/screener" >&2
  exit 1
fi

for command in docker dockerd-rootless.sh newuidmap newgidmap slirp4netns; do
  command -v "$command" >/dev/null || {
    echo "$command is required for the rootless screener executor" >&2
    exit 1
  }
done
rootless_dockerd="$(command -v dockerd-rootless.sh)"

getent group "$EXECUTOR_GROUP" >/dev/null \
  || groupadd --system "$EXECUTOR_GROUP"
if ! id "$EXECUTOR_USER" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$EXECUTOR_HOME" --shell /bin/bash \
    --gid "$EXECUTOR_GROUP" "$EXECUTOR_USER"
fi
usermod -aG "$EXECUTOR_GROUP" "$SCREENER_USER"

uid="$(id -u "$EXECUTOR_USER")"
runtime_dir="/run/ditto-screener-docker"
user_runtime_dir="/run/user/$uid"
docker_host="unix://$runtime_dir/docker.sock"
daemon_root="$EXECUTOR_HOME/docker"
daemon_exec_root="$user_runtime_dir/ditto-screener-docker-exec"

# RootlessKit maps subordinate ids into the daemon's user namespace. Allocate
# one range only when provisioning did not already assign one.
if ! grep -q "^${EXECUTOR_USER}:" /etc/subuid; then
  usermod --add-subuids 100000-165535 "$EXECUTOR_USER"
fi
if ! grep -q "^${EXECUTOR_USER}:" /etc/subgid; then
  usermod --add-subgids 100000-165535 "$EXECUTOR_USER"
fi

loginctl enable-linger "$EXECUTOR_USER"
systemctl start "user@${uid}.service"
install -d -o "$EXECUTOR_USER" -g "$EXECUTOR_GROUP" -m 0700 \
  "$EXECUTOR_HOME"
install -d -o "$EXECUTOR_USER" -g "$EXECUTOR_GROUP" -m 0750 \
  "$daemon_root" "$daemon_root/data"
install -o "$EXECUTOR_USER" -g "$EXECUTOR_GROUP" -m 0640 \
  "$(dirname "$0")/../deploy/rootless-daemon.json" "$daemon_root/daemon.json"

user_unit_dir="$EXECUTOR_HOME/.config/systemd/user"
unit_file="$user_unit_dir/${SCREENER_ROOTLESS_UNIT}.service"
install -d -o "$EXECUTOR_USER" -g "$EXECUTOR_GROUP" -m 0700 \
  "$EXECUTOR_HOME/.config" "$EXECUTOR_HOME/.config/systemd" "$user_unit_dir"
install -o root -g root -m 0755 \
  "$(dirname "$0")/../deploy/executor-egress-guard.sh" \
  /usr/local/sbin/ditto-screener-egress-guard
install -o root -g root -m 0644 \
  "$(dirname "$0")/../deploy/ditto-screener-egress-guard.service" \
  /etc/systemd/system/ditto-screener-egress-guard.service
tmp_unit="$(mktemp)"
trap 'rm -f "$tmp_unit"' EXIT
cat >"$tmp_unit" <<EOF
[Unit]
Description=Rootless Docker daemon for Ditto screener submissions
Documentation=https://docs.docker.com/engine/security/rootless/

[Service]
Type=notify
# dockerd runs inside RootlessKit, so the readiness notification is emitted by
# a child process rather than the systemd-tracked wrapper process.
NotifyAccess=all
Environment=HOME=${EXECUTOR_HOME}
Environment=XDG_RUNTIME_DIR=${user_runtime_dir}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=${user_runtime_dir}/bus
Environment=DOCKER_HOST=${docker_host}
ExecStartPre=/usr/bin/systemctl is-active --quiet ditto-screener-egress-guard.service
ExecStart=${rootless_dockerd} --host=${docker_host} --group=${EXECUTOR_GROUP} --data-root=${daemon_root}/data --exec-root=${daemon_exec_root} --pidfile=${user_runtime_dir}/ditto-screener-docker.pid --config-file=${daemon_root}/daemon.json
# RootlessKit maps the daemon's supplementary group into the subordinate GID
# range. Reset only the control socket to the real host group so the screener
# worker can reach it without granting access to the rootful Docker daemon.
ExecStartPost=/bin/chgrp ${EXECUTOR_GROUP} ${runtime_dir}/docker.sock
ExecStartPost=/bin/chmod 0660 ${runtime_dir}/docker.sock
Restart=always
RestartSec=5
TimeoutStartSec=120
TimeoutStopSec=60
KillMode=mixed
Delegate=yes
TasksMax=8192
MemoryMax=14G
LimitNOFILE=1048576
# Do not add mount-namespace or no-new-privileges hardening here: RootlessKit
# needs the setuid newuidmap/newgidmap helpers and its own mount namespace. The
# daemon remains confined to the dedicated host identity, egress policy, and
# rootless user namespace; untrusted containers retain their separate limits.

[Install]
WantedBy=default.target
EOF
install -o "$EXECUTOR_USER" -g "$EXECUTOR_GROUP" -m 0644 \
  "$tmp_unit" "$unit_file"
rm -f "$tmp_unit"
trap - EXIT

systemctl enable --now ditto-screener-egress-guard.service

# Docker documents that rootless daemons must run under the executor's user
# systemd manager, not a system-wide unit with User=. Remove the legacy unit
# before starting the supported user service.
systemctl disable --now "$SCREENER_ROOTLESS_UNIT" >/dev/null 2>&1 || true
rm -f "/etc/systemd/system/${SCREENER_ROOTLESS_UNIT}.service"
systemctl daemon-reload
# Stopping the legacy system unit removes its RuntimeDirectory. Recreate the
# shared socket directory only after that teardown has completed.
install -d -o "$EXECUTOR_USER" -g "$EXECUTOR_GROUP" -m 0750 "$runtime_dir"
user_systemctl=(
  runuser -u "$EXECUTOR_USER" -- env
  "HOME=$EXECUTOR_HOME"
  "XDG_RUNTIME_DIR=$user_runtime_dir"
  "DBUS_SESSION_BUS_ADDRESS=unix:path=$user_runtime_dir/bus"
  systemctl --user
)
"${user_systemctl[@]}" daemon-reload
"${user_systemctl[@]}" enable --now "$SCREENER_ROOTLESS_UNIT"

for _attempt in $(seq 1 30); do
  if runuser -u "$SCREENER_USER" -- env DOCKER_HOST="$docker_host" \
    docker info --format '{{json .SecurityOptions}}' 2>/dev/null \
      | grep -q 'rootless'; then
    break
  fi
  sleep 1
done
runuser -u "$SCREENER_USER" -- env DOCKER_HOST="$docker_host" \
  docker info --format '{{json .SecurityOptions}}' | grep -q 'rootless'

# Access to a rootful Docker socket is root-equivalent. Keep the system daemon
# temporarily for additive rollout/service dependency compatibility, but remove
# the screener identity from its control group.
gpasswd -d "$SCREENER_USER" docker >/dev/null 2>&1 || true
printf '%s\n' "$docker_host"
