# Native-v2 qualification-only rootless daemon

The `coding_hosted` role targets only `role_coding_hosted` and requires the
dedicated `ditto-coding-hosted-v2` Debian amd64 hostname. It defaults to
`coding_hosted_daemon_enabled: false`; even a normal playbook run does not
install packages, change accounts, write policies or start services while off.
The Terraform host flag also remains false. This role is not automatically
called by application deployment or the legacy coding-executor playbook.

## Preconditions and operator boundary

An explicitly approved base image must already contain reviewed Docker Engine,
CLI and rootless-extras packages at the same exact operator-supplied
`coding_hosted_docker_version` (empty by default), plus Debian's Python, systemd,
dbus-user-session, uidmap, slirp4netns and nftables prerequisites. The role checks
package versions and protected executable files. It does not fetch a floating
installer, enable rootful Docker to install packages, grant cloud privileges, or
provide base-image/package attestation. Review the actual installed binaries,
dependencies and package provenance before applying the role.

An active rootful service/socket is refused, not stopped. After fresh-host checks,
the inactive rootful units are disabled and masked so reboot or package hooks
cannot start them. Any existing native
account or home directory is refused before policy writes, even if currently
quiet: stale user services must not be activated through lingering. This role
is first provisioning only, not an in-place update or runtime recovery mechanism.
After a partial failure it refuses a blind retry rather than recycling state.
Reconcile any retained
attempt, container, private state and evidence before separately approving a
stop/reconfiguration. No automatic prune, deletion, restart of active workloads,
or recursive permission repair is performed.

## Identity and daemon policy

The role creates an empty private home without copying skeleton user services.
The otherwise-empty `ditto-coding-hosted` user has a locked password, nologin
shell, no supplementary privileged group and no other members in its primary
group. The account allocator supplies subordinate IDs; the verifier requires
one 65,536-ID range in both maps, starting at or above 100,000, with no overlap
with other mappings or host account IDs. It does not reuse a hardcoded range,
reallocate an existing range, or fix an unsafe identity silently.

The Unix socket is `/run/ditto-coding-hosted/docker.sock`, owned by that identity
with mode `0600` beneath a `0700` directory recreated by tmpfiles on reboot.
There is no shared client group or TCP Docker listener. A future native worker
using this socket must use the same trusted host identity; this role does not
claim a same-UID isolation boundary between daemon and worker. Provider, database,
Hippius and custody provisioning remain separate reviewed work.

Docker runs as a **user service**, not a system service with `User=`. Its process
gets an explicit clean environment (including systemd's own `NOTIFY_SOCKET`
for the ready handshake), a root-owned public daemon policy, no image
or registry credential, no container log collection, no live-restore and no
automatic daemon restart. The per-user manager (not every host user) delegates
CPU, cpuset, IO, memory and PID controllers. Startup verification requires cgroup
v2/systemd plus memory, swap, CPU-quota and PID-limit support. See [Docker's
rootless guidance](https://docs.docker.com/engine/security/rootless/tips/).

## Qualification-only deny policy

A root system service atomically installs one dedicated nftables `inet` table.
Its output hook rejects **all IPv4 and IPv6 traffic from the daemon UID**,
including public/private addresses, loopback, DNS and metadata. There is no
allowlist override in this role. RootlessKit/slirp host egress must use that
identity; proving that on the actual host is still a qualification requirement.
Other host identities retain the host-foundation provisioning network policy.

The dedicated user manager starts only after this guard succeeds and is bound
to its service lifetime. Stopping the guard does not remove its deny rules.
Policy replacement is an nft transaction; no global ruleset flush is used.
Host root remains trusted: an active service is not proof against subsequent
root firewall changes. Live nft rules and packet-denial tests must be recorded
before approving a runtime. No synthetic test here is a kernel-level proof.

This also blocks future trusted-worker IP traffic under the same UID. Do not
enable a real worker, add a DNS exception or weaken socket checks to work around
it. A separately reviewed control/daemon identity and network design must allow
only the intended trusted connections without giving candidate egress that
authority. Offline image import and network-none grading qualification are
later explicit operator steps, not actions performed by this role.

## Verification and remaining work

After starting the empty daemon, the role verifies its exact rootless security
marker and isolated ownership label, private socket/empty client directory,
cgroup support, and zero images/containers. Its redacted output says
`private_execution_ready=false`, `shadow_only=true`, `weight_eligible=false`.
It is not an approved runtime profile, live packet-denial certificate or canary.
Verification failure leaves resources intact for exact-resource reconciliation;
it must never trigger blind re-provisioning or private attempt replay.

Local tests exercise identity/range/metadata rejection, policy generation,
socket permissions, command environment and role/CI contracts. Ansible syntax
and a disabled local check-mode play prove the default-off path. No live host,
daemon, network namespace, private task or provider is exercised by those tests.
The next gates are real-host deny/resource/cleanup tests, exact-image import,
unchanged private base/reference qualification, approved trusted connectivity,
custody, signing/recovery, Hippius publication and one private shadow canary.
