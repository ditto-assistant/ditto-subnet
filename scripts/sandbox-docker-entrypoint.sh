#!/bin/sh
set -eu

# Keep dockerd rootless, but retain this small root supervisor so it can place
# the model relays inside RootlessKit's network namespace. Inner sandboxes map
# host.docker.internal to that namespace's bridge gateway; proxies started one
# namespace above it are unreachable.
su-exec rootless dockerd-entrypoint.sh "$@" &
dockerd_pid=$!

rootlesskit_child=""
attempt=0
while [ "$attempt" -lt 100 ]; do
  if ! kill -0 "$dockerd_pid" 2>/dev/null; then
    wait "$dockerd_pid"
  fi

  for child in $(cat "/proc/$dockerd_pid/task/$dockerd_pid/children" 2>/dev/null || true); do
    command=$(tr '\0' ' ' <"/proc/$child/cmdline" 2>/dev/null || true)
    case "$command" in
      /proc/self/exe*)
        rootlesskit_child=$child
        break
        ;;
    esac
  done
  [ -n "$rootlesskit_child" ] && break
  attempt=$((attempt + 1))
  sleep 0.1
done

if [ -z "$rootlesskit_child" ]; then
  echo >&2 "could not locate RootlessKit network namespace"
  kill "$dockerd_pid" 2>/dev/null || true
  wait "$dockerd_pid" || true
  exit 1
fi

nsenter -t "$rootlesskit_child" -n socat \
  TCP-LISTEN:11434,fork,reuseaddr TCP:host.docker.internal:11434 &
embed_proxy_pid=$!
nsenter -t "$rootlesskit_child" -n socat \
  TCP-LISTEN:11435,fork,reuseaddr TCP:host.docker.internal:11435 &
gateway_proxy_pid=$!

# shellcheck disable=SC2329 # invoked indirectly by the signal trap below
shutdown() {
  kill "$dockerd_pid" 2>/dev/null || true
}
trap shutdown INT TERM

set +e
wait "$dockerd_pid"
status=$?
set -e
kill "$embed_proxy_pid" "$gateway_proxy_pid" 2>/dev/null || true
wait "$embed_proxy_pid" "$gateway_proxy_pid" 2>/dev/null || true
exit "$status"
