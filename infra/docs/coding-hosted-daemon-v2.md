# Native-v2 qualification-only rootless daemon

The `coding_hosted` role targets only `role_coding_hosted` and requires the
dedicated `ditto-coding-hosted-v2` Debian amd64 hostname. It defaults to
`coding_hosted_daemon_enabled: false`; even a normal playbook run does not
install packages, change accounts, write policies or start services while off.
The Terraform host flag also remains false. This role is not automatically
called by application deployment or the legacy coding-executor playbook.

## Preconditions and operator boundary

An approved base image must contain Debian Python (including `python3-apt`),
systemd and working authenticated Debian repositories. There are two supported
paths: preinstalled approved Docker/rootless prerequisites, or explicit package
bootstrap using `coding_hosted_packages_enabled: true` alongside the daemon gate.
Both require the same exact operator-supplied `coding_hosted_docker_version` for
Engine, CLI and rootless-extras. Bootstrap additionally requires an exact
`coding_hosted_containerd_version` and independently reviewed
`coding_hosted_docker_key_sha256`; all three selections default empty.

Bootstrap runs only after the fresh-host/account/home checks. It refuses
existing container runtime packages and active containerd, masks Docker and
containerd units **before apt**, and uses `policy_rc_d: 101` for every apt action.
It installs Debian rootless prerequisites, verifies Docker's HTTPS signing key
against the supplied SHA-256, and configures a fixed Debian 13 amd64 signed
source. One preferences file puts exact-version records before its origin
record: APT uses the first matching specific record, so a later version fragment
cannot override an earlier package-specific origin record. This ordering follows
[APT's documented priority rules](https://manpages.debian.org/trixie/apt/apt_preferences.5.en.html)
and also gives Ansible's python-apt preflight the selected exact candidate,
avoiding its known
[explicit-version candidate bug](https://github.com/ansible/ansible/issues/82763).
The role requests exact versions and verifies every installed package afterward.
It disables dependency
auto-install by the Ansible module, unauthenticated packages, downgrades,
recommended extras and automatic removals. It verifies installed versions and
inactive services afterward. The package flag alone cannot activate the role.
See [Docker's Debian instructions](https://docs.docker.com/engine/install/debian/)
and [Ansible's service-start suppression](https://docs.ansible.com/projects/ansible/latest/collections/ansible/builtin/apt_module.html#parameter-policy_rc_d).

No floating convenience installer or container image is executed. Debian
dependency versions follow the approved base-image repository snapshot; this is
not a full transitive package lock or a base-image security attestation. Existing
APT trust/configuration and selected package provenance still require approval.
These are public OS dependencies, not an alternative store for private Coding
inputs or evidence. No cloud privilege or private-data capability is granted.

An extraordinary first-provisioning recovery can set
`coding_hosted_package_recovery_enabled: true` only with exact observed Docker
and containerd versions in the two recovery variables. The role accepts that
mode only while the main fresh-host guards still prove the native account and
all state directories absent, conflicting distribution packages absent, and all
rootful units inactive and masked. The installed set must contain exactly the
four expected Docker-origin packages at the approved observed versions. Only
then may apt downgrade them to the independently approved target versions; the
ordinary path keeps downgrade permission false. This is not an update mechanism
for a provisioned daemon and cannot reconcile images, containers or private data.

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
for the ready handshake). The fixed PATH includes `/usr/sbin` because Docker's
rootless launcher calls the protected host `/usr/sbin/sysctl` inside its user
and network namespace setup; the role verifies that executable before creating
the native account. The process receives a root-owned public daemon policy, no image
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
it. The separate default-off
[trusted connectivity role](coding-hosted-connectivity-v2.md) provides an
operator-started cgroup-scoped profile without changing this qualification role.
Its kernel enforcement, authenticated router and actual runtime still require
independent qualification. Offline image import and network-none grading are
later explicit operator steps, not actions performed by this role.

## Verification and remaining work

After starting the empty daemon, the role verifies its exact rootless security
marker and isolated ownership label, Linux amd64 architecture and exact daemon
data root, private socket/empty client directory,
cgroup support, and zero images/containers. Its redacted output says
`private_execution_ready=false`, `shadow_only=true`, `weight_eligible=false`.
It is not an approved runtime profile, live packet-denial certificate or canary.
Verification failure leaves resources intact for exact-resource reconciliation;
it must never trigger blind re-provisioning or private attempt replay.
If startup fails before a socket or daemon exists, verify the exact packages,
identity/ranges, protected policy/unit bytes, active deny guard, empty data and
client directories, and absence of unexpected processes before a separately
reviewed unit-only recovery. Do not rerun first provisioning against an existing
account or use a failed unit as evidence that the daemon never changed state.

Local tests exercise identity/range/metadata rejection, policy generation,
socket permissions, command environment and role/CI contracts. Ansible syntax
and a disabled local check-mode play prove the default-off path. No live host,
daemon, network namespace, private task or provider is exercised by those tests.
The next gates are real-host deny/resource/cleanup tests, exact-image import,
unchanged private base/reference qualification, approved trusted connectivity,
custody, signing/recovery, Hippius publication and one private shadow canary.
