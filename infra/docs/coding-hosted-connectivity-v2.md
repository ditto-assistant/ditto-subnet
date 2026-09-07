# Native-v2 trusted worker connectivity

The separate `coding_hosted_connectivity` role installs a manual, one-attempt
worker unit and an expiring network policy. Its gate defaults false. Installation
does not start or enable the worker, change the running firewall, provision keys,
approve runtime images, or perform a private evaluation. No Terraform intent or
application deployment calls this role.

The [qualification daemon](coding-hosted-daemon-v2.md) and worker retain the same
dedicated UID and private mode-0600 Docker socket. Network authority is separated
by the socket's cgroup ancestry: the trusted worker runs in the root-owned,
nondelegated `system.slice/ditto-coding-hosted-worker.service`; the daemon and
RootlessKit remain below `user.slice/user-UID.slice/user@UID.service`. All accept
rules require the exact UID and cgroup authority, directly for initiation and
through a listener-bound conntrack mark for replies. This does not
isolate one compromised same-UID host process from another; host administrators,
the daemon and the trusted worker remain trusted. Candidate identities and
container boundaries must separately prevent gaining that host identity.

## Explicit profile

The operator supplies `coding_hosted_worker_python` beneath
`/opt/ditto-coding-hosted/`, an existing worker-owned private
`coding_hosted_worker_config` beneath `/var/lib/ditto-coding-hosted/`, and
`coding_hosted_connectivity_profile`. The Python package, Go worker and referenced
private runtime configuration must already be independently approved and
protected. The role neither fetches nor fabricates them. Example addresses and
timestamps below are synthetic placeholders, not production authority:

```json
{
  "schema": "dittobench-coding-hosted-connectivity-v2",
  "shadow_only": true,
  "weight_eligible": false,
  "issued_at_unix": 2000000000,
  "expires_at_unix": 2000000600,
  "trusted_tcp": [
    {"address": "10.20.0.7", "port": 5432},
    {"address": "1.1.1.1", "port": 443}
  ],
  "trusted_dns": [{"address": "127.0.0.53", "port": 53}],
  "trusted_loopback_tcp": true,
  "candidate_tcp": [{"address": "10.30.0.4", "port": 18080}]
}
```

The profile must be issued within five minutes of startup and expire within
24 hours of issuance. No DNS names, CIDRs, wildcards, IPv6, metadata/link-local,
multicast, unspecified addresses or duplicate endpoints are accepted. External
trusted TCP targets are at most 32 exact IPv4/port pairs. Up to two explicit DNS
targets permit TCP/UDP port 53, only for the worker. DNS resolution is not approval
to connect to another IP; address drift requires new operator-reviewed authority.

`trusted_loopback_tcp` explicitly permits the trusted worker to connect to TCP
listeners on `127.0.0.1`, including Docker's randomly assigned harness ports.
It is not granted to the candidate/daemon as an outbound initiation capability.
Review the host's local listeners as part of approving that flag. Other loopback
addresses require individual trusted TCP entries. No blanket established-flow
exception or candidate DNS exception exists.

`candidate_tcp` contains one or two reviewed private IPv4/port pairs, with ports
at least 1024: the native source router and, if needed, its restricted proxy.
These must match the actual runtime configuration and independently qualified
authenticated source-route/proxy policy. An arbitrary forwarding proxy is not
acceptable. The worker can reply from only those listeners; the daemon can reply
to trusted loopback harness connections. Those narrowly scoped reply rules still
require the same unexpired cgroup authority. Image pull access, public internet,
database, provider and Hippius authority are not granted to the daemon.

TCP handshake reply packets can carry request sockets rather than full sockets.
For replies, the input hook verifies the real listening socket's cgroup and
approved endpoint before assigning an otherwise-zero conntrack mark. Worker and
daemon reply marks differ and are derived from the exact profile; the output
hook requires that mark, the UID, reply direction/state, endpoint and an unexpired
UID lease. Existing nonzero marks are never overwritten. Audit other host rules
that write/copy conntrack marks; no competing mark writer may mint these values.
The input hook assigns metadata only and never bypasses another ingress policy.

## Startup, expiry and stop

After separate installation/qualification approval, use
`playbooks/gcp-coding-hosted-connectivity.yml` with the explicit role gate. The role
refuses an active worker, verifies the existing host identity/deny-guard/manager,
and installs the root-only profile at `/etc/ditto-coding-hosted/connectivity.json`.
It does not install credentials or overwrite the private runtime configuration.

Only an explicitly approved `systemctl start ditto-coding-hosted-worker.service`
invocation can execute the worker. There is no install target, boot start,
scheduler, automatic restart or fresh-attempt retry. Network installation runs
as a root `ExecStartPre` in that exact nondelegated worker cgroup, which must have
root-owned, nonwritable-to-worker membership controls. The guard first restores
the existing UID deny policy, validates the root-only config, checks the nft
transaction, rechecks the cgroup inode, then atomically installs scoped rules.
Configuration/kernel/commit failures attempt to restore deny and fail startup.

The dedicated table is reused, never the global ruleset. A distinct
`scoped_output` chain runs after connection tracking; the original qualification
chain is not repurposed with a different hook priority. Cgroup and reply-lease sets have
kernel timeouts, and every accept also checks the absolute Unix expiry, so
installation delay cannot extend the window. Expiry denies existing traffic as
well as new connections. It is not a promise that a timed-out evaluation can be
replayed or that containers have stopped.

`ExecStopPost` restores the original deny-all UID policy, including failed starts.
The unit allows 35 minutes for the runtime's existing conservative cancellation
drain and never deletes state. If nft restoration itself fails, network authority
still expires; record and reconcile that infrastructure failure rather than
claiming an immediate rollback. Reboot starts only the original deny guard and
qualification daemon. The worker is not enabled. Unit stdout/stderr are discarded
to avoid accidental private logs; exact database/evidence state, not logs, is
the completion authority.

## Qualification still required

This role does not supply the missing private PostgreSQL VPC route, rootless
restricted network/proxy, installed custody services, production credentials,
private release registration, native images or canary approval. Do not expose
PostgreSQL publicly, add a metadata exception or allow daemon image-registry
egress to bypass those gaps. Import approved digest-verified images through the
trusted host path before their use.

Before any private attempt, record actual cgroup placement and nondelegation,
effective nft rules, candidate source authentication, resource and cleanup
behavior, and kernel-level traffic tests. Prove worker-only allowed destinations,
candidate-only router access, blocked candidate DNS/metadata/public/other-private
destinations, IPv6 denial, loopback harness request/reply, expiry of an established
connection, startup-failure rollback and service-stop rollback. Pure policy tests
and successful nft syntax checks do not provide those proofs. The local development
host refused isolated user/network namespace creation; no live enforcement proof
is claimed here.

Infrastructure CI additionally runs `scripts/test-coding-hosted-connectivity-kernel.py`
as root inside a new network namespace, refusing the host network namespace. It
uses synthetic listeners and fresh test-owned cgroups, drops test clients to an
unprivileged UID, and tests actual packet decisions, DNS/IPv6 denial, scoped
replies, established-connection expiry and the original deny rollback. It removes
only its own empty cgroups and child processes. This tests kernel rule behavior,
not deployed RootlessKit placement, the real proxy, systemd runtime startup,
private connectivity or production host qualification.

The implementation uses the documented [nft socket cgroup ancestry, time and
connection-tracking expressions](https://netfilter.org/projects/nftables/manpage.html)
and the kernel's [cgroup-v2 delegation boundary](https://docs.kernel.org/admin-guide/cgroup-v2.html).
Unsupported kernel/nft combinations must refuse installation, not fall back to
UID-wide allowances. Shadow-only operation and `weight_eligible=false` remain
mandatory; competitive activation still requires separate approval.
