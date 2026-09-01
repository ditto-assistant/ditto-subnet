# Default-off shadow coding worker

The shadow coding pipeline now has one complete composition path, but every
release keeps it disabled by default and every coding artifact remains
`weight_eligible=false`.

## Runtime order

The validator runs a separate `CodingShadowWorker` beside the ordinary
tool/memory scoring loop only when `VALIDATOR_CODING_SHADOW_ENABLED=true`.
For one stable worker instance it:

1. claims one Platform coding ticket;
2. proves the private scorer handoff and fetches the non-executing authoring,
   screened-harness, and Luna-grant authority while the claim is transferable;
3. commits the Platform `start` boundary before invoking candidate code;
4. heartbeats the exact claim generation while work is active;
5. calls the private Go supervisor for authoring and pristine grading;
6. stores the exact signed authoring-freeze request in the Go outbox before
   Platform transmission and stores the exact verified acknowledgement after;
7. obtains the freeze-bound grading lease, runs the protected grader, and uses
   the same prepare -> publish -> acknowledge order for terminal evidence;
8. uploads and finalizes only the canonical evidence kinds allowed at each
   phase, and persists the redacted terminal release intent before its PUT;
9. releases local retention only after the exact terminal acknowledgement is
   finalized by Platform.

The private Go host can now stream every sealed outbox evidence kind to the
trusted Python validator by exact ticket, record, kind, digest, and size. That
stream has a trusted uploader which requests an exact Platform
capability, forwards the stream with the signed size/type/checksum/metadata
headers, rejects redirects or incomplete transfers, and asks Platform to
finalize the object. The worker calls it only inside the existing default-off
shadow lane. Authoring evidence is finalized before freeze publication;
terminal evidence is finalized before result publication; acknowledgement
bytes are finalized immediately after their exact Platform response is stored.
The authenticated outbox manifest supplies the frozen submission's exact byte
size, which is intentionally absent from the signed freeze request. Worker
integration uses that manifest and never reconstructs this value.

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

Before asking Platform for any claim, the worker enumerates one URL-free local
pending release. An already-finalized receipt is replayed and committed locally
without a new claim or S3 request. If Platform has not finalized it, ordinary
claim recovery resumes the same generation, refreshes the capability, and
uploads the same sealed acknowledgement bytes.

Restored grading is admitted only when the phase runner independently observes
the acknowledged authoring publication in the durable outbox and the supplied
authoring evidence matches that outbox record. Process-local session loss by
itself grants no retry.
Terminal acknowledgement remains `terminal_pending` while its exact bytes are
uploaded and finalized by Platform. Before finalization, the local outbox
persists the redacted upload identity without its bearer URL; after a crash it
can enumerate that identity and replay only an already-finalized Platform
receipt even if claim cleanup has completed. Only the explicit
finalization-bound local release advances the outbox to `released`; observing
that durable state is also the only point where the supervisor may evict its
process-local session tombstone.

The publication and storage clients are created only when the validator shadow
flag is enabled. Both ignore proxy environment variables; the local client uses
the ordinary bounded HTTP timeout, while presigned storage traffic has a fixed
five-minute transfer bound matching the capability contract.

## Activation checklist

Activation is deliberately a coordinated operator action, not a code default.
All of the following must be configured together:

- Platform: `DITTO_CODING_SHADOW_ENABLED=true`, the canonical locked policy
  file, and exact HTTPS exchange, proxy, and revocation URLs;
- scorer: `DITTOBENCH_CODING_SHADOW_ENABLED=true`, an euid-owned mode-0700
  private root, the canonical locked policy file, the source-bound port/base
  URL, and the reviewed runtime-image repository with every selected immutable
  environment/grader digest preloaded on the dedicated daemon;
- sandbox: a dedicated rootless daemon with the isolated-daemon label, the
  capability-only egress network, and an explicit allowlisting proxy;
- validator: `VALIDATOR_CODING_SHADOW_ENABLED=true`, one stable instance ID,
  and the existing private scorer control token.

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
