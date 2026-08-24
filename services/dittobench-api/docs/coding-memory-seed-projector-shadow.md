# Scoped coding-memory seed projector

`internal/codingseed` converts one verified, task-scoped memory artifact into
the existing miner-facing `codingcontract.SeedRequest`. It is shadow-only and
has no endpoint, worker, model call, memory ranking, score, or weight path.

## Artifact boundary

The immutable `memory-bundle` object is exactly the canonical known-field JSON
projection:

```json
{"memories": [...]}
```

with sorted object keys and one trailing newline. It cannot carry ticket, case,
profile, owner, source URL, policy label, embedding, curator metadata, or a full
seed request. Ticket, case, profile capability, digest, and deadline come only
from the trusted authoring lease binding.

The projector independently checks:

- a canonical nonzero ticket UUID and bounded case/profile identities;
- an active deadline no more than two hours ahead;
- an exact lowercase raw-object SHA-256;
- the exact canonical JSON projection (sorted object keys and one trailing newline);
- the shared 4 MiB JSON/wire bound, Unicode, nesting, duplicate-field, trailing
  content, and strict known-field artifact shape;
- a present memory array (`[]` is valid V0; `null` is not);
- every visible-memory field, nullable field, order, unique ID, supersession,
  content, epoch, and confidence bound through `SeedRequest.Validate()`;
- the complete canonical seed request still fits the 4 MiB harness request
  envelope.

The projector never sorts, filters, ranks, embeds, repairs, or interprets stale
memory. V2/V3/V4 cases intentionally require the miner to decide relevance,
freshness, conflict, and user-override priority.

Platform and the Go artifact fetcher now enforce the same 4 MiB maximum. The
previous 64 MiB transport ceiling could certify an artifact that neither the
shared canonical contract nor the starter kit's HTTP body limit could accept.

## Delivery boundary

`Projector.Deliver` uses a narrow seed-only port. It sends the exact deep-owned
request once and validates case, profile, digest, and count in the response.
Either `idempotent_replay=false` or `true` is valid: after an ambiguous lost
response, the same operational seed may already be installed. Retry policy and
health/run orchestration remain with the future gateway. Certification keeps
its stricter fresh-then-replay proof.

`codingattempt.BeginAuthoring` projects and closes the raw memory reader before
returning an `AuthoringSession`. The session exposes only an opaque projection
whose `Request()` accessor deep-clones every nullable pointer and supersession
slice; JSON diagnostics fail closed and textual logs contain no memory content.
Freeze or close clears the session-owned projection after harness seeding.

## Activation

The default-off coding host invokes this layer before `/coding/run` and clears
the projection during terminal cleanup. It never publishes memory through
diagnostics and remains unreachable while the scorer gate is false. Coding
contract v1 remains permanently `weight_eligible=false`.

## Validation

```bash
cd services/dittobench-api
go test -race ./internal/codingseed ./internal/codingattempt \
  ./internal/codingartifacts ./internal/codingcertifier ./internal/codingcontract
go vet ./internal/codingseed ./internal/codingattempt \
  ./internal/codingartifacts ./internal/codingcertifier ./internal/codingcontract
go test ./...
```
