# Shadow coding inference capability gateway

`internal/codinggateway` is the disabled validator-local composition layer for
one ticket-bound Luna capability. It joins the reviewed
`codingplatform.Client`, `codingrelayjournal.Store`, and `codingrelay.Relay`
without adding a listener, worker, scheduler, score, or deployment setting.

## Fresh activation

`Activate` accepts one already-exchanged Platform grant and an empty private
mode-`0700` journal directory that the integrator already created. It opens
and locks the journal, requires a trusted authorizer to prove the outbox
activation marker already committed, then durably commits the attempt binding
and constructs the secret-owning Platform client and relay before asking a
trusted publisher to mount the handler behind one opaque, source-bound base
URL. A journal with any prior binding is never republished.

The future attempt owner must reserve the durable evidence outbox and commit
its non-rerunnable `BeginTranscript` activation marker before calling
`Activate`. This package deliberately cannot manufacture that authority.

The miner receives only the base URL. It never receives the Platform bearer,
broker signing key, model-relay route, provider credential, journal path, or
grant-control fields. A caller-supplied HTTP transport is a trusted test or
integration dependency; production uses the hardened no-proxy, no-redirect
transport owned by `codingplatform.Client`.

## Revocation and evidence

Revocation is serialized and ordered:

1. revoke the source-bound outer route so no new miner request is admitted;
2. revoke the relay, waiting for an admitted dispatch to settle durably;
3. revoke the exact Platform grant generation;
4. permit deterministic model evidence finalization.

Every step is idempotent. A lost Platform revocation response is retried with
the same ticket, case, profile, grant, generation, policy digest, and deadline.
`Close` is refused before the complete revocation sequence and deletes no
journal evidence.

## Restart recovery

`Recover` accepts no bearer, broker private key, provider transport, or
publisher. It never recreates a miner-visible route. A journal containing only
completed settlements is durably revoked and can reproduce exact model
evidence. An incomplete dispatch marker means the provider outcome is unknown
and is terminally ambiguous: the local and Platform grants are revoked and the
request is never repeated.

The future gateway/worker remains responsible for mapping that typed outcome,
preparing the complete signed Platform publication envelope in the durable
evidence outbox, coordinating the harness and workspace capability, and
retaining both journal and outbox until terminal acknowledgement.

## Activation boundary

No production composition root imports this package. No deployment file
enables the coding model-relay gate, and no scheduler calls the gateway. Coding
contract v1 remains permanently `weight_eligible=false`.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codinggateway ./internal/codingplatform \
  ./internal/codingrelay ./internal/codingrelayjournal
go vet ./internal/codinggateway ./internal/codingplatform \
  ./internal/codingrelay ./internal/codingrelayjournal
```
