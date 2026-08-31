#!/usr/bin/env bash
set -euo pipefail

# Install a rootless Docker daemon owned by an otherwise-empty host identity.
# This script never installs a scorer or validator, and it never reads a cloud,
# provider, Platform, or wallet credential. Its only output is the fixed socket
# URL after proving the daemon is rootless and carries the isolated label.

executor_user="${CODING_EXECUTOR_USER:-ditto-coding-executor}"
executor_group="${CODING_EXECUTOR_GROUP:-ditto-coding-executor}"
client_group="${CODING_EXECUTOR_CLIENT_GROUP:-ditto-coding-client}"
executor_home="${CODING_EXECUTOR_HOME:-/var/lib/ditto-coding-executor}"
runtime_dir="${CODING_EXECUTOR_RUNTIME_DIR:-/run/ditto-coding-executor}"
unit="${CODING_EXECUTOR_UNIT:-ditto-coding-executor-docker}"
guard_unit="${CODING_EXECUTOR_EGRESS_GUARD_UNIT:-ditto-coding-executor-egress-guard}"
daemon_memory_max="${CODING_EXECUTOR_DAEMON_MEMORY_MAX:-24G}"
daemon_tasks_max="${CODING_EXECUTOR_DAEMON_TASKS_MAX:-8192}"

if [[ "$EUID" -ne 0 ]]; then
  echo "install-rootless-docker.sh must run as root" >&2
  exit 1
fi
for value in "$executor_user" "$executor_group" "$client_group" "$unit" "$guard_unit"; do
  if [[ ! "$value" =~ ^[a-z_][a-z0-9_-]*$ ]]; then
    echo "invalid coding executor identity" >&2
    exit 1
  fi
done
if [[ "$executor_home" != /var/lib/ditto-coding-executor || "$runtime_dir" != /run/ditto-coding-executor ]]; then
  echo "coding executor paths are invalid" >&2
  exit 1
fi
if [[ "$daemon_memory_max" != 24G || "$daemon_tasks_max" != 8192 ]]; then
  echo "coding executor daemon bounds are invalid" >&2
  exit 1
fi
for command in docker dockerd-rootless.sh newuidmap newgidmap slirp4netns; do
  command -v "$command" >/dev/null || {
    echo "$command is required for the rootless coding executor" >&2
    exit 1
  }
done

getent group "$executor_group" >/dev/null || groupadd --system "$executor_group"
getent group "$client_group" >/dev/null || groupadd --system "$client_group"
if ! id "$executor_user" >/dev/null 2>&1; then
  useradd --create-home --home-dir "$executor_home" --shell /usr/sbin/nologin \
    --gid "$executor_group" "$executor_user"
fi
usermod -G "$executor_group" "$executor_user"

uid="$(id -u "$executor_user")"
user_runtime_dir="/run/user/$uid"
docker_host="unix://$runtime_dir/docker.sock"
daemon_root="$executor_home/docker"
daemon_exec_root="$user_runtime_dir/ditto-coding-executor-exec"
rootless_dockerd="$(command -v dockerd-rootless.sh)"

if ! grep -q "^${executor_user}:" /etc/subuid; then
  usermod --add-subuids 100000-165535 "$executor_user"
fi
if ! grep -q "^${executor_user}:" /etc/subgid; then
  usermod --add-subgids 100000-165535 "$executor_user"
fi

loginctl enable-linger "$executor_user"
systemctl start "user@${uid}.service"
user_systemctl=(
  runuser -u "$executor_user" -- env
  "HOME=$executor_home"
  "XDG_RUNTIME_DIR=$user_runtime_dir"
  "DBUS_SESSION_BUS_ADDRESS=unix:path=$user_runtime_dir/bus"
  systemctl --user
)
install -d -o "$executor_user" -g "$executor_group" -m 0700 "$executor_home"
install -d -o "$executor_user" -g "$executor_group" -m 0750 \
  "$daemon_root" "$daemon_root/data"
install -d -o "$executor_user" -g "$client_group" -m 0750 "$runtime_dir"
install -o "$executor_user" -g "$executor_group" -m 0640 \
  /usr/local/lib/ditto-coding-executor/rootless-daemon.json \
  "$daemon_root/daemon.json"

systemctl daemon-reload
systemctl enable --now "$guard_unit.service"

user_unit_dir="$executor_home/.config/systemd/user"
unit_file="$user_unit_dir/${unit}.service"
install -d -o "$executor_user" -g "$executor_group" -m 0700 \
  "$executor_home/.config" "$executor_home/.config/systemd" "$user_unit_dir"
tmp_unit="$(mktemp)"
trap 'rm -f "$tmp_unit"' EXIT
cat >"$tmp_unit" <<EOF
[Unit]
Description=Dedicated rootless Docker daemon for Ditto shadow coding
Documentation=https://docs.docker.com/engine/security/rootless/

[Service]
Type=notify
NotifyAccess=all
Environment=HOME=${executor_home}
Environment=XDG_RUNTIME_DIR=${user_runtime_dir}
Environment=DBUS_SESSION_BUS_ADDRESS=unix:path=${user_runtime_dir}/bus
Environment=DOCKER_HOST=${docker_host}
ExecStartPre=/usr/bin/systemctl is-active --quiet ${guard_unit}.service
ExecStart=${rootless_dockerd} --host=${docker_host} --group=${client_group} --data-root=${daemon_root}/data --exec-root=${daemon_exec_root} --pidfile=${user_runtime_dir}/ditto-coding-executor.pid --config-file=${daemon_root}/daemon.json
ExecStartPost=/bin/chgrp ${client_group} ${runtime_dir}/docker.sock
ExecStartPost=/bin/chmod 0660 ${runtime_dir}/docker.sock
Restart=always
RestartSec=5
TimeoutStartSec=120
TimeoutStopSec=60
KillMode=mixed
Delegate=yes
TasksMax=${daemon_tasks_max}
MemoryMax=${daemon_memory_max}
LimitNOFILE=1048576

[Install]
WantedBy=default.target
EOF
install -o "$executor_user" -g "$executor_group" -m 0644 "$tmp_unit" "$unit_file"
rm -f "$tmp_unit"
trap - EXIT

# A dedicated coding host never needs rootful Docker. Stop it after the guard
# and rootless unit definitions exist, then recreate the runtime socket root
# because stopping legacy services can remove /run state.
systemctl disable --now docker.service docker.socket >/dev/null 2>&1 || true
"${user_systemctl[@]}" disable --now "$unit" >/dev/null 2>&1 || true
rm -f "/etc/systemd/system/${unit}.service"
systemctl daemon-reload
install -d -o "$executor_user" -g "$client_group" -m 0750 "$runtime_dir"

"${user_systemctl[@]}" daemon-reload
"${user_systemctl[@]}" enable --now "$unit"

for _attempt in $(seq 1 30); do
  if runuser -u "$executor_user" -- env DOCKER_HOST="$docker_host" \
    docker info --format '{{json .SecurityOptions}} {{json .Labels}}' 2>/dev/null \
    | grep -q 'rootless' && \
    runuser -u "$executor_user" -- env DOCKER_HOST="$docker_host" \
      docker info --format '{{json .Labels}}' 2>/dev/null \
      | grep -q 'io.heyditto.dittobench.isolated=true'; then
    break
  fi
  sleep 1
done
runuser -u "$executor_user" -- env DOCKER_HOST="$docker_host" \
  docker info --format '{{json .SecurityOptions}}' | grep -q 'rootless'
runuser -u "$executor_user" -- env DOCKER_HOST="$docker_host" \
  docker info --format '{{json .Labels}}' \
  | grep -q 'io.heyditto.dittobench.isolated=true'

gpasswd -d "$executor_user" docker >/dev/null 2>&1 || true
printf '%s\n' "$docker_host"
