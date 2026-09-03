# Protected Hippius canary helper proxies

## Status

This layer packages three distinct local clients for the phase-6 canary
operator. It does not implement or start the unwrap, authoring, or grading
backend services. The Ansible gate
`platform_coding_hippius_canary_helpers_enabled` is false by default, and the
disabled path removes only the explicitly managed proxy/config/operator entry
points.

No proxy is part of the Platform API or model-relay process environment. No
route, scheduler, worker, score, weight, or emission path invokes them.

## Local transport

Ansible installs the same audited standard-library proxy source as three
separate root-owned files:

```text
/opt/ditto-platform-canary/bin/hippius-canary-unwrap
/opt/ditto-platform-canary/bin/hippius-canary-authoring
/opt/ditto-platform-canary/bin/hippius-canary-grading
```

Each is group-executable mode `0550`. The operator verifies that the three
configured paths resolve to distinct device/inode identities. The executable
basename fixes its role; arguments and role selection from the environment are
rejected.

Each proxy loads one root-owned, group-readable mode-`0440` canonical v2 config
from `/etc/ditto-platform/coding/hippius-canary`. The config fixes:

- the role-specific absolute Unix socket;
- the socket-file GID and expected backend effective UID/GID separately;
- request and response byte limits; and
- a timeout no greater than the canary ticket deadline.

The socket must be mode `0660`, owned by the configured backend UID and the
Platform group. The proxy checks the socket inode before connecting and then
checks Linux `SO_PEERCRED` on the connected stream, so swapping a pathname to a
different process does not grant authority.

One request and response use unsigned 64-bit big-endian length frames around
canonical JSON. The role-specific request and response schemas are checked on
both sides of the exchange. Oversized, truncated, duplicated-field,
noncanonical, wrong-schema, wrong-peer, or unavailable responses fail closed.
The proxy prints no payload or backend detail on failure.

## Backend boundary

The proxies are clients, not key or execution services:

- the isolated unwrap-service layer implements the unwrap backend under its own
  UID/GID with an exact two-request authority and systemd socket activation;
- the authoring backend runs under a second UID and may adapt only the reviewed
  authoring supervisor boundary; and
- the grading backend runs under a third UID and may adapt only the reviewed
  pristine-grading boundary.

All three UIDs and primary GIDs must be non-root, mutually distinct, and
different from the Platform operator identity and socket-access group. Test
fixture programs are not backend implementations and are forbidden in a live
run. The existing Coding supervisor still requires complete lease, harness,
inference-grant, and durable-outbox authority; an execution backend must not
fabricate those fields from the reduced canary request.

## Deployment record and operator environment

After all ordinary Platform health checks pass, `scripts/update.sh` atomically
writes the exact deployed commit to mode-`0600`
`apps/platform/logs/deployed-source.sha`. A failed or partially verified deploy
does not advance that record. The canary operator independently compares this
file with its plan and tracked-clean checkout.

When the helper gate is enabled, Ansible writes a separate protected
`operator.env` containing the helper paths and the already dedicated
private-input reader settings. It is never sourced by PM2 and is not added to
`platform.env.j2` or the relay environment. The operator must explicitly source
the ordinary Platform `.env` and this one-shot environment before invoking the
confirmation-gated command.

Enablement also requires the private catalog and sealed-evidence custody gates,
an existing fresh plan/deployment record/manifest/publication receipt/curator
key, and three already-running correctly owned sockets. Ansible installs no
secret value and starts no backend service.

## Activation boundary

The separately reviewed unwrap layer adds a default-off service package but no
private key, prepared authority, host-variable enablement, Ansible converge,
socket activation, provider operation, or live unwrap. Authoring and grading
backend services remain absent.

Phase 6 remains incomplete until separately reviewed backend implementations
are installed, the default-off gate is explicitly enabled, exact merged source
is deployed, and the owner accepts one ready redacted receipt. Phase 7 remains
blocked until then.
