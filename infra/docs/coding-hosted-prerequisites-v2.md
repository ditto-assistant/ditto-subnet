# Native-v2 host prerequisites

The `coding_hosted_prerequisites` role fixes the host values the hosted runtime
reads for candidate networking and identity, checks them against the live host,
and installs a manual egress proxy unit with an empty allowlist. It defaults
off. It creates no account, no Docker network, no nft rule and no credential,
and it never enables or starts a service. It is not a worker installation, a
connectivity profile, packet-enforcement evidence or canary approval.

## What the runtime needs

`services/dittobench-api/internal/codinghostedruntime/config.go` loads four
host-bound fields that "the operator must provision beforehand". The role
pins each one to a closed value; none is an operator input. The two ports are
role constants in `roles/coding_hosted_prerequisites/vars/main.yml`, read by the
record and unit templates; the helper and proxy take them from the rendered
record and unit.

| Field | Fixed value | Runtime check | Host prerequisite |
|---|---|---|---|
| `router_listen` | `<host>:18080` | Private, non-loopback IPv4 and canonical port 1024–65535 (`config.go:168-173`). The address is also the sandbox host gateway | The worker binds it during an attempt. The role proves the address is local, the exact listener is free and the port is outside the ephemeral range |
| `egress_proxy` | `http://<host>:18090` | `http`, private non-loopback IPv4, explicit port, no credentials, path, query or fragment (`config.go:174-181`) | Refusing proxy unit, installed but not started |
| `egress_network` | `ditto-coding-restricted` | Lowercase identifier (`config.go:200-205`); must be nonempty (`codingharness/sandbox.go:43`) | None. See below |
| `candidate_uid`, `candidate_gid` | `10001` | Nonzero (`codingexecutor/factory.go:42`); the Rust driver requires exactly 10001 (`codingexecutor/docker.go:167`) | Must map into the daemon's subordinate range. No host account |

The runtime never attaches containers to the configured network name. For each
harness start, `sandbox.LocalDocker` creates a fresh ICC-disabled
`ditto-job-<id>` bridge in the rootless daemon and removes it at stop; executor
phases run with `--network none`. The name only switches that behaviour on. So
the role **does not create a Docker network**: a standing network would be
unused daemon state. It rejects names beginning with `ditto-job-`.

Rootless Docker maps container ID 0 to the daemon account and IDs 1–65,536 into
its single subordinate range. Candidate 10001 is therefore host ID
`subuid_start + 10000` (and likewise for groups). The daemon role already
refuses any host account inside that range; this role reports the mapped IDs.

## One-time and per-attempt state

This role owns only static host state. Per-attempt authority stays elsewhere:

- The [connectivity profile](coding-hosted-connectivity-v2.md) grants the
  daemon's `candidate_tcp` access for one window. It must list `<host>:18080`.
  Bounded rollout also requires `<host>:18090`; in single-attempt mode listing
  the proxy is optional, and an unlisted proxy is simply unreachable.
- The worker binds the router; the runtime creates and removes each bridge; the
  Platform companion creates `state_root` for each attempt.
- An operator starts the proxy before the worker and stops it afterwards.

### One attempt at a time

The record has a single `router_listen`, and each running worker binds it, so
this host supports **one concurrent attempt** (`max_parallel` 1). A bounded
rollout approval may allow up to 4, but
`apps/platform/ditto/api_server/coding_bounded_rollout.py` keys each attempt's
runtime resource on `host.router_listen`, so attempts sharing this record run
one after another. One refusing proxy can serve those attempts in turn. Parallel
attempts need additional router ports, matching `candidate_tcp` entries and a
new record; that is a separately reviewed follow-up, not an input.

## Egress model

Candidate traffic leaves the per-run bridge through RootlessKit as the daemon
UID, which the deny guard blocks except for the connectivity window. Inference
and workspace tools reach the router at `host.docker.internal`, exempt through
`NO_PROXY`. Hosted-v2 candidates need no other destination, so the proxy has
an **empty allowlist**: `CONNECT` gets `403`, any other request `405`, and the
connection closes. It has no upstream connection code. Its unit runs as a
`DynamicUser`, allows only `AF_INET`, binds only TCP 18090, denies every IP peer
except `<host>/32`, and discards output rather than logging candidate data.
A non-empty allowlist would be a separate reviewed change, not an input.

## Inputs and refusals

- `coding_hosted_prerequisites_enabled: true`;
- `coding_hosted_prerequisites_confirmation`: exactly
  `CONVERGE NATIVE CODING HOST PREREQUISITES`;
- `coding_hosted_prerequisites_source_revision`: the reviewed lowercase
  40-character commit;
- `coding_hosted_prerequisites_host_address`: the host's primary IPv4 from the
  reviewed host apply, `10.33.0.2`–`10.33.0.253`, equal to the gathered default
  IPv4 address.

The play targets `role_coding_hosted` and requires hostname
`ditto-coding-hosted-v2`, Debian 13 and x86_64. It refuses check mode when
enabled. Before any write it also refuses:

- an inactive `ditto-coding-hosted-egress.service` deny guard;
- a live `ditto-coding-hosted-worker.service`, `ditto-coding-custody@*.service`
  or egress proxy (any state other than inactive or failed);
- a missing or unsafe daemon policy directory or `host-policy.py`;
- any installed file whose bytes, owner, mode or link count differ from this
  revision, or a receipt path that is not a root-owned `0444` file;
- a non-root check, a host policy rejection, an unmapped candidate identity, a
  non-local address, a busy listener or a port in the ephemeral range. The bind
  probe sets `SO_REUSEADDR` like Go `net.Listen`, so `TIME_WAIT` from an earlier
  attempt is free while any live listener is still busy;
- a `systemd-analyze unit-paths` list that differs from the reviewed systemd 257
  system list (`systemd.unit(5)`, "Unit File Load Path"), and, in any of those
  twelve directories, another `ditto-coding-hosted-egress-proxy.service` file,
  alias or mask; a `.d`, `.wants`, `.requires` or `.upholds` directory for the
  full name, each dash-truncated prefix (`ditto-coding-hosted-egress-.service`
  through `ditto-.service`) or the top-level `service`; another unit's
  dependency link to the proxy; or a same-name `.socket`, `.timer` or `.path`
  unit;
- a unit for which `systemd-analyze verify --recursive-errors=no` exits nonzero
  or prints anything.

The verify check is deliberately not filtered by unit name. With
`--recursive-errors=no`, systemd 257 reports only the named unit and its
drop-ins, not its dependencies: in a Debian 13 rehearsal, broken drop-ins for
`sysinit.target`, `system.slice` and `-.slice`, a missing dependency and an
invalid `system.conf` printed nothing and exited 0, while a top-level
`service.d` warning printed a line naming only the drop-in. Unrelated dependency
warnings therefore do not block convergence, and a name filter could hide a
warning that applies to this unit.

The helper and unit are checked from a scratch directory that is then removed,
so a failed check leaves no installed file.

## What it changes

| Path | Owner, mode | Purpose |
|---|---|---|
| `/usr/local/lib/ditto-coding-hosted/host-prerequisites.py` | root, `0444` | Read-only `check` helper |
| `/usr/local/lib/ditto-coding-hosted/egress-proxy.py` | root, `0444` | Refusing proxy |
| `/usr/local/lib/ditto-coding-hosted/host-prerequisites.json` | root, `0444` | Fixed host record (see below) |
| `/etc/systemd/system/ditto-coding-hosted-egress-proxy.service` | root, `0644` | Proxy unit without an `[Install]` section |
| `/usr/local/lib/ditto-coding-hosted/host-prerequisites-receipt.json` | root, `0444` | Convergence receipt: `source_revision`, `record_sha256`, `applied_at` (UTC) |

Nothing reads the host record yet, so today it is advisory. The hosted
attempt-config materializer
(`apps/platform/ditto/api_server/coding_hosted_attempt_config.py`, in a separate
change that has not landed) is its enforcing consumer: it refuses a record that
is missing, writable or not this closed shape, and takes every per-attempt
`host` value from it rather than from operator input.

Writes never replace existing bytes. Unit definitions are reloaded only when
this run installed a file. The receipt is written when a file was installed or
no receipt exists; it is not byte-compared, so a rerun from a newer revision with
identical files changes nothing and the receipt keeps the revision that
installed them. A run stopped between install and reload is refused on rerun
(`NeedDaemonReload` or not loaded): run `sudo systemctl daemon-reload` and rerun.

```text
ansible-playbook -i infra/ansible/inventory/gcp.yml \
  infra/ansible/playbooks/gcp-coding-hosted-prerequisites.yml \
  --limit ditto-coding-hosted-v2 \
  -e '{"coding_hosted_prerequisites_enabled":true,
       "coding_hosted_prerequisites_confirmation":"CONVERGE NATIVE CODING HOST PREREQUISITES",
       "coding_hosted_prerequisites_source_revision":"<merged-source-sha>",
       "coding_hosted_prerequisites_host_address":"<reviewed-primary-ipv4>"}'
```

Around one separately approved attempt:

```text
sudo systemctl start ditto-coding-hosted-egress-proxy.service
# start custody and the worker as their own documents describe
sudo systemctl stop ditto-coding-hosted-egress-proxy.service   # after the worker has stopped
```

## Verification

After convergence the role requires the unit to be loaded from the installed
file with no drop-ins, `UnitFileState=static`, `ActiveState=inactive`, no
reverse start dependency (`WantedBy`, `RequiredBy`, `UpheldBy`, `BoundBy`,
`TriggeredBy`, `OnFailureOf`, `OnSuccessOf`; a vendor-path `.wants` link leaves
`UnitFileState=static`), and each file to match its rendered bytes. It prints the values with
`services_started=false`, `shadow_only=true` and `weight_eligible=false`. To
recheck later, with the worker and proxy stopped:

```text
systemctl show ditto-coding-hosted-egress-proxy.service \
  -p UnitFileState -p ActiveState -p DropInPaths -p FragmentPath
sudo /usr/bin/python3 -I /usr/local/lib/ditto-coding-hosted/host-prerequisites.py check \
  < /usr/local/lib/ditto-coding-hosted/host-prerequisites.json
cat /usr/local/lib/ditto-coding-hosted/host-prerequisites-receipt.json
```

These are configuration checks. They do not prove candidate packet denial,
proxy reachability, bridge isolation or cleanup on the real host; the
network-enforcement evidence stays a separate qualification record.

The proxy sets `SO_REUSEADDR`, so it restarts at once after refusals leave its
port in `TIME_WAIT`; Linux still refuses a second listener on the same address
because `SO_REUSEPORT` stays off. After each refusal it half-closes and discards
at most 64 KiB of unread request data for up to one second, so the connection
ends with FIN rather than a reset that could drop the `403` or `405`.

## Rollback and removal

There is no account, Docker network, firewall rule or data to remove. With the
worker, every custody instance and the proxy stopped:

```text
sudo systemctl stop ditto-coding-hosted-egress-proxy.service
sudo rm /etc/systemd/system/ditto-coding-hosted-egress-proxy.service
sudo systemctl daemon-reload
sudo rm /usr/local/lib/ditto-coding-hosted/egress-proxy.py \
  /usr/local/lib/ditto-coding-hosted/host-prerequisites.py \
  /usr/local/lib/ditto-coding-hosted/host-prerequisites.json \
  /usr/local/lib/ditto-coding-hosted/host-prerequisites-receipt.json
```

Reissue any connectivity profile that names the proxy. A new host address or a
changed role revision needs this removal and a fresh reviewed convergence; the
role refuses to rewrite the old files in place.

## Validation

```bash
uv run pytest -q ditto/tests/test_coding_hosted_prerequisites.py
(cd services/dittobench-api && go test ./internal/codinghostedruntime -run Prerequisites)
cd infra/ansible
uvx --from ansible-core==2.21.2 ansible-playbook --syntax-check \
  -i inventory/validator-static.yml playbooks/gcp-coding-hosted-prerequisites.yml
uvx --from ansible-core==2.21.2 ansible-playbook --check \
  -i localhost, tests/coding-hosted-prerequisites.yml
```

The Go test renders the role's record with the role's port constants and passes
it through the real runtime loader; the DittoBench workflow runs on changes to
both files. The fixture runs the real input guard against synthetic facts. None of
this touches a host, daemon, firewall or provider. Shadow-only operation and
`weight_eligible=false` remain mandatory.
