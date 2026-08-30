# Private shadow coding attempt supervisor

`internal/codingsupervisor` defines the authenticated private control-plane
boundary between the Python validator coordinator and a future trusted Go
attempt backend. `ditto.validator.coding_supervisor.CodingSupervisorRuntime`
implements the existing `CodingAttemptRuntime` protocol against that boundary.

## Private wire

Every request fixes:

- one operation: `author`, `grade`, `abort_authoring`, `abort_grading`, or
  `recover`;
- a canonical operation UUID, ticket UUID, coding-run ID, and deadline;
- one bounded lease object when the operation requires it;
- the complete authoring outcome only for grading.

The handler accepts only fixed `POST` paths, unencoded `application/json`, no
query string, a single exact bearer, bounded duplicate-free JSON, and coherent
operation-specific nullability. It stores only the SHA-256 of the configured
control token. One ticket/run pair is single-flight; a concurrent duplicate is
rejected before the backend runs.

Responses bind the same operation and authority. Authoring must prove both
capability revocation and environment destruction and return content-addressed
transcript/patch references. Grading returns one to 100 task-evidence objects
and a destroyed environment. Recovery can expose only a bounded state and, for
a pending publication, the exact stage/request digest. Backend errors are
projected to generic codes and never include task, provider, lease, or secret
text.

The Python client sends the existing scorer control token only to
`VALIDATOR_DITTOBENCH_API_URL` over HTTPS, or over HTTP to loopback during
local development. It disables redirects per request, bounds response bytes,
requires `Cache-Control: no-store` and JSON, and independently validates the
operation, operation UUID, ticket, run, and typed outcome before returning
coordinator dataclasses.

## Current boundary

The handler has an injected `Backend` port. No composition root supplies that
backend or mounts `Handler`; no worker constructs `CodingSupervisorRuntime`.
This PR therefore cannot fetch leases, open artifact capabilities, start a
harness, invoke Luna, grade a repository, publish evidence, claim work, or
affect scores and weights.

The next review must implement the trusted backend from `codingattempt`,
`codinggateway`, `codingoutbox`, harness/publisher adapters, and the host
sweeper. Worker registration remains a separate disabled-shadow PR after that
backend is complete.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codingsupervisor
go vet ./internal/codingsupervisor

cd ../..
uv run ruff check ditto/validator/coding_supervisor.py \
  ditto/tests/validator/test_coding_supervisor.py
uv run mypy ditto/validator/coding_supervisor.py \
  ditto/tests/validator/test_coding_supervisor.py
uv run pytest -q ditto/tests/validator/test_coding_supervisor.py
```
