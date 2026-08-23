# Shadow coding private runtime adapters

`internal/codingharness`, `internal/codingsource`,
`internal/codinggrantrevoke`, and `codingphase.GatewayActivator` implement the
private adapters required by the phase runner. Nothing constructs them in a
production worker yet.

## Dormant screened harness

`codingharness.Factory.Acquire` validates the complete ticket/run/agent/image
binding, requires the canonical `ditto-screen/<agent UUID>:latest` reference,
checks the short-lived HTTPS image capability, and asks the existing sandbox
verifier to download, hash, parse, and load the exact Docker-save archive. It
does not start a container.

The production sandbox adapter requires the dedicated rootless daemon, the
isolated-daemon label, a capability-only egress network, proxy enforcement,
read-only root filesystem, dropped capabilities, bounded CPU/memory/PIDs, and
no Docker socket or credential environment. `Activate` starts the image only
after the phase runner committed its non-rerunnable outbox marker. It then
registers the Docker-observed private source address and creates the strict
coding harness HTTP client. `Destroy` removes source admission before proving
the container stopped; an ambiguous stop remains retryable. Harness HTTP calls
use the minimum of the caller context, remaining ticket lifetime, and the
operation's reviewed bound; no hidden five-minute client timeout truncates a
larger signed run wall budget.

## Source-bound routes

`codingsource.Router` owns one explicitly supplied host-gateway listener. A
workspace or inference route is published only when every binding field
matches one currently active harness registration. The model-visible URL has a
cryptographically random path capability, while every request must also arrive
from the exact directly observed container source address. Forwarding headers
are ignored, query and escaped paths are rejected, and the outer prefix is
removed before the reviewed runner or relay handler receives the request.

Revocation removes admission first and waits for already-admitted requests.
The registration is one-to-one by instance and source address, expires with
the ticket, and fails closed on in-process clock rollback. URLs, tokens, source
addresses, image capabilities, and private bindings reject diagnostic JSON and
use redacted string/log representations.

## Grant revocation and relay activation

`codinggrantrevoke.Revoker` accepts only the distinct revocation bearer from
the Platform exchange. It binds ticket, case, profile, grant, generation,
policy digest, and deadline to the relay binding, performs one no-redirect
HTTPS request, validates the no-store terminal acknowledgement, and zeros the
bearer after success. A lost response is safely retried against Platform's
idempotent endpoint.

`codingphase.GatewayActivator` creates one deterministic private relay-journal
directory per ticket/grant generation beneath an existing euid-owned mode
`0700` parent. Creation is descriptor-relative and parent-fsynced, including
an `EEXIST` retry after an ambiguous prior fsync. The activator supplies the
source-bound inference publisher and revoker to `codinggateway`, and refuses a
journal capacity below
the complete effective request/retry budget. Any failure before gateway
ownership durably revokes the already-exchanged grant.

## Boundary

This PR does not register a listener, construct a Docker client, claim a
ticket, mount the supervisor, publish Platform evidence, score a miner, or set
a weight. Coding contract v1 remains shadow-only and permanently
`weight_eligible=false`. The next review owns the exclusive ticket claim and
durable publication handoff; the final review owns default-off worker wiring.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codingharness ./internal/codingsource \
  ./internal/codinggrantrevoke ./internal/codingphase ./internal/codinggateway \
  ./internal/codingplatform ./internal/sandbox
go vet ./internal/codingharness ./internal/codingsource \
  ./internal/codinggrantrevoke ./internal/codingphase ./internal/codinggateway \
  ./internal/codingplatform ./internal/sandbox
```
