# Native-v2 private PostgreSQL path

This layer prepares the private network and guest admission path from the
Platform-owned Coding host to the existing Platform PostgreSQL VM. Both network
and guest gates default off. It creates no database user, password, grant, worker,
assignment or private evaluation, and performs no protected apply or convergence.
The [host foundation](coding-hosted-host-v2.md) remains independently default-off.

## Network boundary

`enable_coding_hosted_postgres` requires `enable_coding_hosted_host`. The root
passes the actual `module.pg_vm.internal_ip`, Platform VPC self-link and existing
PostgreSQL target tag into the native-host module. The optional `postgres_peer`
input defaults null and rejects disabled hosts, another project's/network's
self-link and addresses outside the current Platform `10.30.0.0/24` IPv4 subnet.

Two reviewed VPC peerings exchange private subnet routes. Custom routes, public
subnet routes and IPv6 exchange are disabled. Peering is not a port-specific
route or a firewall grant: private subnet routes are automatically exchanged by
[Google Cloud's peering model](https://docs.cloud.google.com/vpc/docs/vpc-peering).
The only new access permissions are separate firewall rules:

- Coding egress: its existing VM tag to the actual PostgreSQL `/32`, TCP 5432,
  at priority 800 before the existing priority-900 private/metadata deny.
- PostgreSQL ingress: the actual Coding VM's single `/32` to the existing
  PostgreSQL target tag, TCP 5432. No whole-subnet or cross-VPC source-tag grant.

Peering creation depends on both rules. The original private/metadata and other
egress denials remain intact; no reverse inbound exception is added on Coding.
Review effective inherited policies and both networks' rules before apply.
VPC rules cannot distinguish trusted worker and candidate processes sharing one
VM: separately qualify the default-deny host/cgroup/router boundary before
granting this route. The route does not approve a private execution environment.

The nonsecret `coding_hosted_postgres_access` output records the exact client IP,
client `/32`, PostgreSQL private IP and port. It explicitly reports
`database_login_ready=false`, `shadow_only=true` and `weight_eligible=false`.
An IP change after host replacement requires renewed review and guest convergence;
do not reuse an old host/IP approval or destroy retained host state automatically.

## Guest firewall and PostgreSQL admission

After separately reviewing the protected Terraform plan/apply and actual host
boundary, supply `coding_hosted_postgres_enabled: true` and the exact
`coding_hosted_postgres_client_ip` from that output to the existing
`gcp-platform-pg.yml` convergence. Do not guess an address or place passwords in
inventory, Git, receipts or command output. Existing protected password handling
and OS Login/IAP access remain unchanged.

The guard runs before the `base` role can change UFW, and again for direct
`postgres` role callers. It permits only `ditto-pg-platform` in its proper
inventory group and a canonical single usable native-subnet address. Reserved,
public, other-subnet, CIDR, IPv6, wrong-type and newline/injection inputs fail.

With the guest flag off, both existing allowlists render unchanged. When enabled:

- UFW gains only TCP 5432 from the exact host `/32`.
- HBA gains only `ditto_platform_prod`, the existing `ditto` Platform application
  principal, that same `/32`, and `scram-sha-256` authentication. Existing Platform
  subnet rules remain unchanged; the new rule grants neither dev-database nor
  all-user admission from Coding.

This uses the existing trusted Platform application principal and does not change
its SQL privileges. Its credential must never reach validators, miners or
candidate containers. Approval of that principal's use by the trusted native
service, credential custody, and authenticated/encrypted database-channel policy
remain operator responsibilities. Neither a private VPC route nor SCRAM alone
is a claim of certificate-verified TLS. No public PostgreSQL endpoint, PgBouncer,
new database, key distribution or password rotation is added by this layer.

HBA-only changes now notify a PostgreSQL reload, not a restart. Tuning changes
that require restart retain their existing restart handler. A full database
convergence can still change other approved settings; review the complete diff
and any resulting maintenance needs rather than treating it as HBA-only blindly.

## Qualification and rollback

After convergence, verify the actual routes, source IP, cloud and guest firewall
rules, effective HBA rows, credential/TLS behavior and a bounded native-service
database query. Prove rejection from other hosts and candidate containers and
against unintended ports, databases and users. Then continue approved runtime
installation, unchanged-private-suite qualification, custody/signing, Hippius
release publication and the private shadow canary. None of those live proofs is
provided by mocked plan or configuration-rendering tests.

Rollback is explicit and must preserve private state:

1. Stop/drain the exact native invocation and revoke its host network authority;
   reconcile any unfinished attempt/evidence before another run.
2. Review removal of only the PostgreSQL peerings and exceptions by setting the
   separate network flag false. Keep the host flag true if retained host data
   must remain; do not decommission the host as a network rollback shortcut.
3. Remove the exact native UFW rule through an authorized targeted operation.
   UFW convergence is additive: omitting a rule does not delete an existing one.
   Never reset the complete guest firewall.
4. Set the guest flag false and converge/reload the managed HBA configuration.
   A reload does not terminate existing connections. Inspect exact Coding-host
   backend identities and obtain approval before terminating any retained
   sessions; do not kill all PostgreSQL clients.

The module tests use a mocked provider and `command=plan` only. The Ansible
fixture forces a local connection, has no convergence roles or privilege
escalation, and checks native rendering plus 13 invalid addresses. CI syntax
checks the real playbook and runs that fixture with `--check`. These tests do not
read production database credentials or apply network/host changes.
