# Default-off shadow coding worker

The shadow coding pipeline now has one complete composition path, but every
release keeps it disabled by default and every coding artifact remains
`weight_eligible=false`.

## Runtime order

The validator runs a separate `CodingShadowWorker` beside the ordinary
tool/memory scoring loop only when `VALIDATOR_CODING_SHADOW_ENABLED=true`.
For one stable worker instance it:

1. claims one Platform coding ticket from its configured exact run;
2. proves the private scorer handoff and fetches the non-executing authoring,
   screened-harness, and Luna-grant authority while the claim is transferable;
3. commits the Platform `start` boundary before invoking candidate code;
4. heartbeats the exact claim generation while work is active;
5. calls the private Go supervisor for authoring and pristine grading;
6. stores the exact signed authoring-freeze request in the Go outbox before
   Platform transmission and stores the exact verified acknowledgement after;
7. obtains the freeze-bound grading lease, runs the protected grader, and uses
   the same prepare -> publish -> acknowledge order for terminal evidence.

The Go host is constructed when `DITTOBENCH_CODING_SHADOW_ENABLED=true` or
`DITTOBENCH_CODING_CANARY_ENABLED=true`. It composes the phase-specific Docker
executor factory, artifact fetcher, scoped memory projector, durable outbox,
dormant screened-harness controller, direct-source registry, opaque workspace
and Luna routes, relay journal, attempt supervisor, publication service, and a
bounded outbox sweep loop. The public-canary handler is attached only when
`DITTOBENCH_CODING_CANARY_ENABLED=true` and
`DITTOBENCH_CODING_CERTIFICATION_ROOT` contains the pinned `certification/v1`
pack; a canary-enabled host without that pack fails closed. The default-off
canary worker claims a lease, exchanges a lease-bound inference grant, posts
the exchanged grant into the canary control plane, and always revokes the
grant. The canary path does not claim private tickets or set weights.
The relay-journal root has a durable directory-cardinality ceiling equal to the
host attempt bound; an unexpected entry or exhausted root fails closed instead
of allocating unbounded restart residue.

Authoring and grading do not share one prebuilt executor. Authoring receives
only the selected public environment-image digest and the validator-only
resource profile. The complete protected grader manifest is accepted only
after Platform returns the freeze-gated grading lease.

## Recovery

A started claim is never reassigned to another instance. On restart the same
instance asks the supervisor for durable state:

- `terminal_pending` reopens and republishes only the exact stored request;
- `authoring_pending` publishes the exact stored freeze, then continues from
  the acknowledged immutable patch;
- `authoring_published` reloads the stored request and acknowledgement and may
  enter pristine grading without rerunning candidate authoring;
- `released` is already complete;
- `none`, `ambiguous`, and `expired` never execute candidate code again.

Restored grading is admitted only when the phase runner independently observes
the acknowledged authoring publication in the durable outbox and the supplied
authoring evidence matches that outbox record. Process-local session loss by
itself grants no retry.
Terminal acknowledgement advances the outbox to `released`; observing that
durable state is also the only point where the supervisor may evict its
process-local session tombstone.

## Dedicated-executor client boundary

`CodingSupervisorRuntime` now has an explicit remote mode for the dedicated
executor transport. A trusted caller supplies a separately constructed TLS 1.3
client, the loaded validator keypair, private executor origin, and validator
hotkey. Every exact supervisor body is SHA-256 bound into a fresh, short-lived
`dittobench-coding-executor-control-v1` envelope; recovery additionally carries
the durable claim's agent UUID and artifact SHA instead of inventing missing
identity. The remote request sends only the signed envelope and JSON body—never
the local scorer bearer.

`CodingPublicationClient` now has the same explicit remote boundary. Its five
publication operations carry the real claim's agent UUID, artifact digest,
ticket, run, and deadline authority and sign their exact canonical JSON bytes.
The executor ingress independently requires the signed agent, artifact,
ticket, and run identities to match the publication command. Remote `open` is
also constrained to that outbox record and remote `pending` is a
non-enumerating readiness probe. Because a valid envelope cannot exist before
a claim, remote mode claims a still-unstarted ticket, performs this probe, and
only then requests leases or crosses the Platform start boundary. Local mode
retains its pre-claim loopback probe.

`CodingExecutorTLSConfig` and `create_coding_executor_http_client` validate
three distinct absolute credential files, reject symlinks, writable or
executable credentials, oversized inputs, and group/world-readable private
keys, then construct a no-proxy, no-redirect TLS 1.3-only client. This client is
deliberately not parsed from environment or constructed by validator startup
yet. Both remote clients are therefore dormant. A later atomic PR must inject
one protected mTLS client and the same private executor origin into supervisor
and publication construction together; it must not make only one half remote.

## Activation checklist

Activation is deliberately a coordinated operator action, not a code default.
All of the following must be configured together:

- Platform: an explicit Ansible
  `platform_coding_shadow_enabled: true`, which renders
  `DITTO_CODING_SHADOW_ENABLED=true`, the relay's separate coding gate, the
  canonical locked policy file, and exact HTTPS exchange, proxy, and
  revocation URLs. The reconciler and k=3 ticket-set admin gates remain
  separate false-by-default controls;
- scorer: `DITTOBENCH_CODING_SHADOW_ENABLED=true`, an euid-owned mode-0700
  private root, the canonical locked policy file, the source-bound port/base
  URL, and the reviewed runtime-image repository with every selected immutable
  environment/grader digest preloaded on the dedicated daemon;
- sandbox: a dedicated rootless daemon with the isolated-daemon label, the
  capability-only egress network, and an explicit allowlisting proxy;
- validator: `VALIDATOR_CODING_SHADOW_ENABLED=true`, one exact
  `VALIDATOR_CODING_SHADOW_RUN_ID`, one stable instance ID, and the existing
  private scorer control token.

The default-zero dedicated GCP executor cohort documented in
`infra/docs/coding-executor-hosts.md` is the physical isolation foundation for
the future k=3 canary. It creates neither a daemon nor a worker. A later
reviewed role must install and prove the rootless isolated daemon before any of
the activation settings above are eligible for operator use.

That dedicated rootless-daemon role is itself false by default and has no
scorer or worker consumer. It creates an empty daemon identity and an empty
socket-client group only; enabling a future coding worker remains a separate
reviewed operator action.

Setting only a subset fails closed: no ticket is claimed, or startup rejects
the incomplete runtime. The committed Compose values keep both worker gates
false. This PR does not deploy the Platform transport configuration, change a
benchmark version, combine coding with ordinary scoring, alter emissions, or
enable a leaderboard weight.

## Validation

```bash
uv run pytest -q ditto/tests/validator/test_coding_worker.py \
  ditto/tests/validator/test_coding_attempt.py \
  ditto/tests/validator/test_coding_supervisor.py \
  ditto/tests/validator/test_coding_publication.py

cd services/dittobench-api
go test -race ./internal/codinghost ./internal/codingpublication \
  ./internal/codingsupervisor ./internal/codingphase \
  ./internal/codingattempt ./internal/codingexecutor
go vet ./...
```
