#!/bin/sh
# Manual, isolated integration test. Requires Linux Docker, a locally built
# sandbox image, and a preloaded BusyBox image; it creates privileged DinD
# containers without external network access. Do not run against a live daemon.
set -eu
image=${1:?usage: test-sandbox-firewall.sh SANDBOX_IMAGE [BUSYBOX_IMAGE]}
busybox=${2:-busybox:1.37}
tmp=$(mktemp -d)
name="ditto-firewall-test-$$"
cleanup() { docker rm -f "$name" >/dev/null 2>&1 || :; rm -rf "$tmp"; }
trap cleanup EXIT HUP INT TERM
docker image inspect "$image" "$busybox" >/dev/null

for backend in nft legacy; do
  if [ "$backend" = legacy ]; then legacy=1; else legacy=; fi
  docker run -d --name "$name" --privileged --network none -e DOCKER_TLS_CERTDIR= -e DOCKER_IPTABLES_LEGACY="$legacy" "$image" >/dev/null
  n=0
  until docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1; do
    n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi
    sleep 1
  done
  [ "$(docker exec "$name" cat /run/ditto-sandbox-firewall-backend)" = "$backend" ]
  # Initial install and the worker's first reconcile have both completed.
  # Chain declarations in iptables-restore must replace, not append, rules.
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; [ "$(iptables -w 5 -S DITTO-SANDBOX-EGRESS | grep -c "^-A ")" -eq 6 ] && [ "$(iptables -w 5 -t nat -S DITTO-OPENROUTER-SHIM | grep -c "^-A ")" -eq 1 ]'
  docker save "$busybox" | docker exec -i "$name" docker load >/dev/null
  docker exec "$name" docker image inspect "$busybox" >/dev/null
  docker exec "$name" docker run --rm --pull=never --network ditto-sandbox "$busybox" true
  gateway=$(docker exec "$name" docker network inspect --format '{{(index .IPAM.Config 0).Gateway}}' ditto-sandbox)
  docker exec -d "$name" socat TCP-LISTEN:11436,reuseaddr,fork EXEC:/bin/cat
  docker exec -d "$name" socat TCP-LISTEN:19999,reuseaddr,fork EXEC:/bin/cat
  sleep 1
  docker exec "$name" docker run --rm --pull=never --network ditto-sandbox "$busybox" nc -z -w 2 "$gateway" 11436
  if docker exec "$name" docker run --rm --pull=never --network ditto-sandbox "$busybox" nc -z -w 2 "$gateway" 19999; then
    printf '%s: non-allowlisted port was reachable\n' "$backend" >&2; exit 1
  fi
  # A second nested bridge supplies a synthetic routed peer. Docker normally
  # drops direct cross-bridge packets in raw PREROUTING before DOCKER-USER;
  # remove only that test peer's rule so the control exercises our chain.
  docker exec "$name" docker network create test-peer >/dev/null
  docker exec "$name" docker run -d --name test-peer --pull=never --network test-peer "$busybox" nc -lk -s 0.0.0.0 -p 19999 -e /bin/cat >/dev/null
  docker exec "$name" docker run -d --name test-source --pull=never --network ditto-sandbox "$busybox" sleep 300 >/dev/null
  peer=$(docker exec "$name" docker inspect -f '{{range .NetworkSettings.Networks}}{{.IPAddress}}{{end}}' test-peer)
  peer_network=$(docker exec "$name" docker network inspect -f '{{.Id}}' test-peer | cut -c1-12)
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -t raw -D PREROUTING -d "$1/32" ! -i "br-$2" -j DROP; iptables -w 5 -I FORWARD 2 -j ACCEPT' _ "$peer" "$peer_network"
  if docker exec "$name" docker exec test-source nc -z -w 2 "$peer" 19999; then
    printf '%s: forwarded peer was reachable with hooks installed\n' "$backend" >&2; exit 1
  fi
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -D DOCKER-USER -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS; iptables -w 5 -D DOCKER-USER -i dtj+ -j DITTO-SANDBOX-EGRESS'
  docker exec "$name" docker exec test-source nc -z -w 2 "$peer" 19999
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -I DOCKER-USER 1 -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS; iptables -w 5 -I DOCKER-USER 1 -i dtj+ -j DITTO-SANDBOX-EGRESS; iptables -w 5 -D FORWARD 2'
  docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -I FORWARD 1 -j ACCEPT'
  if docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck; then
    printf '%s: health missed displaced forward hook\n' "$backend" >&2; exit 1
  fi
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -D FORWARD 1'
  docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -t nat -I PREROUTING 1 -j ACCEPT'
  if docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck; then
    printf '%s: health missed displaced NAT hooks\n' "$backend" >&2; exit 1
  fi
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -t nat -D PREROUTING 1'
  docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck
  docker exec "$name" sh -c 'set -e; case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -I INPUT 1 -j ACCEPT'
  if docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck; then
    printf '%s: health missed displaced hook\n' "$backend" >&2; exit 1
  fi
  printf '%s: allowed and denied ports and hook drift checked\n' "$backend"
  docker exec "$name" sh -c 'kill -KILL "$(cat /run/ditto-sandbox-firewall-worker)"' || :
  n=0
  while [ "$(docker inspect -f '{{.State.Running}}' "$name")" = true ]; do
    n=$((n + 1)); if [ "$n" -ge 30 ]; then docker logs "$name"; exit 1; fi; sleep 1
  done
  docker logs "$name" 2>&1 | grep -q 'sandbox firewall maintenance worker exited; stopping daemon'
  docker rm -f "$name" >/dev/null
done

# Stall the actual first restore and then inject a failure in the worker's
# next reconciliation. The daemon must not listen early or keep running after
# the failed reconciliation. Test shims and markers exist only in the container.
cat > "$tmp/iptables-restore" <<'EOF'
#!/bin/sh
if [ -f /run/test-pause-restore ]; then
  touch /run/test-restore-paused
  while [ ! -f /run/test-resume-restore ]; do /bin/sleep 0.1; done
fi
if [ -f /run/test-fail-restore ]; then exit 42; fi
exec /usr/sbin/iptables-restore "$@"
EOF
cat > "$tmp/sleep" <<'EOF'
#!/bin/sh
if [ "${1-}" = 21600 ]; then
  touch /run/test-reconcile-paused
  while [ ! -f /run/test-resume-reconcile ]; do /bin/sleep 0.1; done
  rm -f /run/test-resume-reconcile
  exit 0
fi
exec /bin/sleep "$@"
EOF
chmod 755 "$tmp/iptables-restore" "$tmp/sleep"
# Remove one hook while the worker is paused and verify the actual reconcile
# repairs it without reversing the required hook priority.
for backend in nft legacy; do
  if [ "$backend" = legacy ]; then legacy=1; else legacy=; fi
  docker run -d --name "$name" --privileged --network none -e DOCKER_TLS_CERTDIR= \
    -e DOCKER_IPTABLES_LEGACY="$legacy" -v "$tmp/sleep:/usr/local/bin/sleep:ro" "$image" >/dev/null
  n=0
  until docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1 && docker exec "$name" test -f /run/test-reconcile-paused; do
    n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi; sleep 1
  done
  # Losing either NAT hook must not duplicate the surviving hook.
  for bridge in ditto-sandbox0 'dtj+'; do
    docker exec "$name" sh -c 'case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -t nat -D PREROUTING -i "$1" -j DITTO-OPENROUTER-SHIM' _ "$bridge"
    docker exec "$name" sh -c 'rm /run/test-reconcile-paused; touch /run/test-resume-reconcile'
    n=0
    until docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1 && docker exec "$name" test -f /run/test-reconcile-paused; do
      n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi; sleep 1
    done
    docker exec "$name" sh -c 'case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; [ "$(iptables -w 5 -t nat -S PREROUTING | grep -c -- "-i dtj+ -j DITTO-OPENROUTER-SHIM")" -eq 1 ] && [ "$(iptables -w 5 -t nat -S PREROUTING | grep -c -- "-i ditto-sandbox0 -j DITTO-OPENROUTER-SHIM")" -eq 1 ]' || {
      echo "$backend: partial NAT hook repair duplicated a live hook" >&2
      exit 1
    }
  done
  docker exec "$name" sh -c 'case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -D INPUT -i ditto-sandbox0 -j DITTO-SANDBOX-EGRESS; iptables -w 5 -t nat -I PREROUTING 1 -j ACCEPT'
  if docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck; then echo 'health missed missing hook' >&2; exit 1; fi
  docker exec "$name" sh -c 'rm /run/test-reconcile-paused; touch /run/test-resume-reconcile'
  n=0
  until docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1 && docker exec "$name" test -f /run/test-reconcile-paused; do
    n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi; sleep 1
  done
  # A displaced pair leaves lower-priority copies after safe repair. Losing
  # either leading hook repeatedly must not duplicate the surviving hook.
  for bridge in ditto-sandbox0 'dtj+' ditto-sandbox0 'dtj+'; do
    docker exec "$name" sh -c 'case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; iptables -w 5 -t nat -D PREROUTING -i "$1" -j DITTO-OPENROUTER-SHIM' _ "$bridge"
    docker exec "$name" sh -c 'rm /run/test-reconcile-paused; touch /run/test-resume-reconcile'
    n=0
    until docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1 && docker exec "$name" test -f /run/test-reconcile-paused; do
      n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi; sleep 1
    done
    docker exec "$name" sh -c 'case "$(cat /run/ditto-sandbox-firewall-backend)" in legacy) export PATH="/usr/local/sbin/.iptables-legacy:$PATH";; esac; [ "$(iptables -w 5 -t nat -S PREROUTING | grep -c -- "-i dtj+ -j DITTO-OPENROUTER-SHIM")" -eq 2 ] && [ "$(iptables -w 5 -t nat -S PREROUTING | grep -c -- "-i ditto-sandbox0 -j DITTO-OPENROUTER-SHIM")" -eq 2 ]' || {
      echo "$backend: displaced NAT hook repair duplicated the surviving hook" >&2
      exit 1
    }
  done
  docker rm -f "$name" >/dev/null
done

# Pause a snapshot writer after it emits partial output. Health must continue
# reading the previous complete snapshot until the new file is renamed.
cat > "$tmp/iptables" <<'EOF'
#!/bin/sh
if [ "$*" = '-w 5 -S DITTO-SANDBOX-EGRESS' ] && [ -f /run/test-snapshot-pause ]; then
  rules=$(/usr/sbin/iptables "$@") || exit 1
  printf '%s\n' "$rules" | sed -n '1p'
  touch /run/test-snapshot-paused
  while [ ! -f /run/test-snapshot-resume ]; do /bin/sleep 0.1; done
  printf '%s\n' "$rules" | sed '1d'
  exit 0
fi
exec /usr/sbin/iptables "$@"
EOF
chmod 755 "$tmp/iptables"
docker run -d --name "$name" --privileged --network none -e DOCKER_TLS_CERTDIR= \
  -e DOCKER_IPTABLES_LEGACY= -v "$tmp/sleep:/usr/local/bin/sleep:ro" \
  -v "$tmp/iptables:/usr/local/sbin/iptables:ro" "$image" >/dev/null
n=0
until docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1 && docker exec "$name" test -f /run/test-reconcile-paused; do
  n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi; sleep 1
done
docker exec "$name" sh -c 'rm /run/test-reconcile-paused; touch /run/test-snapshot-pause /run/test-resume-reconcile'
n=0
until docker exec "$name" test -f /run/test-snapshot-paused; do n=$((n + 1)); [ "$n" -lt 90 ] || exit 1; sleep 1; done
if ! docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck; then
  printf 'health failed while a snapshot write was paused\n' >&2; exit 1
fi
docker exec "$name" sh -c 'rm /run/test-snapshot-pause; touch /run/test-snapshot-resume'
n=0
until docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1 && docker exec "$name" test -f /run/test-reconcile-paused; do
  n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi; sleep 1
done
docker rm -f "$name" >/dev/null

docker run -d --name "$name" --privileged --network none -e DOCKER_TLS_CERTDIR= \
  -v "$tmp/iptables-restore:/usr/local/bin/iptables-restore:ro" -v "$tmp/sleep:/usr/local/bin/sleep:ro" \
  -e DOCKER_IPTABLES_LEGACY= --entrypoint sh "$image" -c 'touch /run/test-pause-restore; exec /usr/local/bin/sandbox-docker-entrypoint' >/dev/null
n=0
until docker exec "$name" test -f /run/test-restore-paused; do n=$((n + 1)); [ "$n" -lt 90 ] || exit 1; sleep 1; done
if docker exec "$name" docker info >/dev/null 2>&1 || docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1; then
  printf 'daemon listened or became healthy before policy installation\n' >&2; exit 1
fi
docker exec "$name" touch /run/test-resume-restore
n=0
until docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1 && docker exec "$name" test -f /run/test-reconcile-paused; do
  n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi; sleep 1
done
docker exec "$name" touch /run/test-fail-restore /run/test-resume-reconcile
n=0
while [ "$(docker inspect -f '{{.State.Running}}' "$name")" = true ]; do
  n=$((n + 1)); if [ "$n" -ge 30 ]; then docker logs "$name"; exit 1; fi; sleep 1
done
docker logs "$name" 2>&1 | grep -q 'sandbox firewall initialization or reconciliation failed; stopping daemon'
printf 'startup barrier and reconciliation failure checked\n'
docker rm -f "$name" >/dev/null

# Intercept only network-list calls: one transient error must recover, while
# persistent errors must stop dockerd after the bounded retry budget.
cat > "$tmp/docker" <<'EOF'
#!/bin/sh
if [ "${1-}" = network ] && [ "${2-}" = ls ]; then
  count=0
  [ ! -f /run/test-network-attempts ] || count=$(cat /run/test-network-attempts)
  count=$((count + 1))
  printf '%s\n' "$count" > /run/test-network-attempts
  if [ "$TEST_DOCKER_FAILURE" = always ] || [ "$count" -eq 1 ]; then
    printf 'injected network-list failure %s\n' "$count" >&2
    exit 42
  fi
fi
exec /usr/local/bin/docker "$@"
EOF
chmod 755 "$tmp/docker"
for mode in once always; do
  docker run -d --name "$name" --privileged --network none -e DOCKER_TLS_CERTDIR= \
    -e DOCKER_IPTABLES_LEGACY= -e TEST_DOCKER_FAILURE="$mode" \
    -v "$tmp/docker:/usr/local/sbin/docker:ro" "$image" >/dev/null
  n=0
  while :; do
    if [ "$mode" = once ] && docker exec "$name" /usr/local/sbin/dockerd --ditto-healthcheck >/dev/null 2>&1; then
      [ "$(docker exec "$name" cat /run/test-network-attempts)" -eq 2 ]
      break
    fi
    if [ "$(docker inspect -f '{{.State.Running}}' "$name")" = false ]; then
      [ "$mode" = always ] || { docker logs "$name"; exit 1; }
      docker logs "$name" 2>&1 | grep -q 'injected network-list failure 3'
      docker logs "$name" 2>&1 | grep -q 'sandbox firewall initialization or reconciliation failed; stopping daemon'
      break
    fi
    n=$((n + 1)); if [ "$n" -ge 90 ]; then docker logs "$name"; exit 1; fi; sleep 1
  done
  docker rm -f "$name" >/dev/null
done
printf 'transient and persistent Docker API failures checked\n'
