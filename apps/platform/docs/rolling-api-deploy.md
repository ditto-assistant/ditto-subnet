# Rolling Platform API deployment

The production Python API has one primary PM2 process on port 8000 and one
warm standby on loopback port 8002. Caddy prefers the primary and uses passive
failure detection to send new requests to standby while a fork-mode PM2 reload
stops the primary port. The standby serves the same HTTP routes but does not
start the provider-route refresher or any other singleton background loop.

The standby's PostgreSQL pool is capped at 8 connections. The budget is
primary 30 + development 30 + two inference relays at 12 each + standby 8 =
92 of the database's current 100 connections, leaving three superuser-reserved
and five ordinary slots.

## Activation

1. Merge the application change. Its first automatic Platform deploy still
   uses one process because the Ansible-owned standby port is not yet in the
   host environment.
2. Apply the reviewed `gcp-platform-app.yml` Ansible plan through the protected
   production infrastructure path. It writes `DITTO_PLATFORM_STANDBY_PORT=8002`
   and reloads Caddy with primary-first failover. Until a subsequent application
   deploy starts the standby, the primary remains the live upstream.
3. Re-dispatch the Platform deployment for the exact released commit. The
   updater starts or reloads standby, requires its `/health` to return the
   target commit, then reloads primary and verifies both processes. If standby
   does not become healthy, primary is left running. If primary is already
   down, the updater refuses to restart the only serving standby.
4. Confirm both local `/health` endpoints report the same commit and DB/chain
   health, then check the public `/health` and dashboard data during one
   controlled primary reload. The public response must continue without a
   502. Inspect PM2 and Caddy logs if any request fails.

To back out the routing, set `platform_api_standby_port: 0`, apply the same
protected Ansible path, and redeploy the application. Caddy then routes only
to the primary. The old standby PM2 entry can be removed after Caddy no longer
references it.
