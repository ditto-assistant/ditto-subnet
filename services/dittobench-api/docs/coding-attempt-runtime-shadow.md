# Trusted shadow coding attempt runtime

`internal/codingattempt` is the in-process trusted adapter that composes the
reviewed artifact fetcher, coding runner/freezer, sandbox executor, and pristine
grader. It has separate authoring and grading entry points and no HTTP server,
Platform client, scheduler, or validator-worker registration.

## Phase boundary

Authoring accepts exactly:

```text
visible-bundle   / workspace-materializer
memory-bundle    / memory-seed-projector
resource-profile / resource-supervisor
```

It verifies ticket, deadline, case, profile, runner manifest, candidate limits,
resource-profile digest, delivery phase, artifact kind, audience, and object
digest before opening bytes. The visible bundle constructs a private
`codingrunner.Session`; the runtime asks the scoped-memory projector to verify
canonical bytes and build a deep-owned seed request, then the runtime closes the
raw reader before returning the session. Grader material is absent from the
authoring type.

`AuthoringSession.Freeze` requires an outer capability revoker. It attempts that
revocation before freezing the internal runner, caches one
presence-preserving deep copy of the freeze result, and permits transcript
streaming only after freeze. Repeated freeze and close calls return stable
results. The runtime never calls the injected revoker while holding its
lifecycle mutex; concurrent freeze or close attempts fail without destroying
the in-flight session. Closing before revocation/freeze cleans local state but
reports an integration error.

Grading accepts exactly:

```text
visible-bundle   / workspace-materializer
resource-profile / resource-supervisor
grader-bundle    / protected-grader
```

It additionally requires the freeze ID, authoring-evidence digest,
content-addressed frozen-submission key, and exact frozen patch SHA. It reopens
fresh visible bytes, never accepts memory, validates the canonical frozen patch,
and uses a lazy protected-bundle opener. Protected bytes are downloaded only
after the pristine replay and final-tree check succeed, then
`codinggrader.GradeWithProtectedOpener` runs with the sandbox-attested executor.
The delayed download inherits the signed grader lease deadline. Broken artifact
adapters that return no reader or return a reader together with an error fail
closed and release any returned reader.

## Resource authority

Both phases parse the signed `dittobench-coding-grader-resource-v1` artifact
under a 4 MiB limit. Duplicate fields, invalid Unicode/nesting, malformed
numbers, unsafe policies, digest drift, and disagreement with runner/grader
limits fail before candidate or grader bytes are used. Unknown fields remain
non-authoritative for rolling compatibility.

Artifact transport errors retain the fetcher's typed expired, unavailable, and
integrity domains while remaining URL-redacted. This lets the later worker map
retryable infrastructure without parsing error prose.

## Activation

This package is unwired. A later local gateway must connect it to the Python
validator coordinator, a source-bound capability publisher,
the harness lifecycle, ticket-scoped Luna relay, the now-available but unwired
durable evidence outbox, and a host sweeper. No production composition root imports it. It cannot
claim jobs, start miners, submit scores, rank agents, set weights, or make coding
contract v1 weight eligible.

## Validation

```bash
cd services/dittobench-api
go test -race ./internal/codingattempt ./internal/codingseed ./internal/codingartifacts \
  ./internal/codingrunner ./internal/codinggrader ./internal/codingexecutor \
  ./internal/codingcontract
go vet ./internal/codingattempt ./internal/codingseed ./internal/codingartifacts \
  ./internal/codingrunner ./internal/codinggrader ./internal/codingexecutor \
  ./internal/codingcontract
```
