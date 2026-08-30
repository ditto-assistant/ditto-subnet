# Private coding execution-plan contract

`internal/codingexecution` freezes the task-static authority consumed by the
private supervisor phase runner. Platform now mirrors the contract,
verifies complete plan bundles in the private catalog, and delivers only the
correct phase projection. The Go package remains a contract and projector: it
has no catalog reader, artifact credential, workspace, harness, listener,
worker, scorer, or weight path.

## Phase split

The authoring runner plan contains:

- case, visible-bundle, and base-tree identity;
- disjoint editable, creatable, and deletable paths;
- fixed visible test and build command argv plus timeouts;
- the complete candidate runner limits.

Ticket, deadline, profile, memory, inference grant, provider authority, grader
bundle, protected test commands, expected hidden-test counts, and protected
resource limits are structurally absent. The runner plan is bound to the
model-visible runtime policy by the sorted union of paths and the exact ordered
test/build command IDs.

The grading plan contains the already versioned pristine-grader projection:
grader contract, image, bundle, test manifest, exact commands, expected group
counts, and execution order. Its separate resource profile contains candidate
and protected limits plus sandbox ceilings. The bundle validator requires:

```text
runner case / visible bundle / base tree
    == grader case / visible bundle / base tree

runner candidate limits == resource candidate limits
grader resource_profile_sha256 == canonical resource profile digest
grader_contract_sha256 == compiled grader-v1 contract digest
```

Canonical digests use the shared sorted-key UTF-8 JSON policy with one trailing
newline. Unknown fields remain non-authoritative and are excluded from the
known-field digest. Required known fields, nested commands, path safety,
collection presence, order, uniqueness, hard limits, command allowlists, and
cross-phase identity are fail-closed.

## Current boundary

The shared vectors are synthetic and use no repository, patch, memory, usable
capability, provider credential, signing key, or real private catalog record.
The runner-plan digest is committed through `task_commitment_sha256`; authoring
leases return only that runner plan. Protected grader/resource projections are
returned only after Platform accepts and revalidates an immutable freeze.

The default-off `internal/codinghost` composition now supplies each phase
projection to `internal/codingphase`, reserves its evidence outbox, and commits
the non-rerunnable activation marker before harness activation. Authoring and
grading executors are constructed separately so the protected plan remains
unavailable during authoring.

All committed feature gates are false. Coding contract v1 stays permanently
`weight_eligible=false`.

Validation:

```bash
cd services/dittobench-api
go test -race ./internal/codingexecution ./internal/codingrunner ./internal/codinggrader
go vet ./internal/codingexecution ./internal/codingrunner ./internal/codinggrader

cd ../..
uv run ruff check ditto/api_models/coding.py ditto/tests/api_models/test_coding.py
uv run mypy ditto/api_models/coding.py ditto/tests/api_models/test_coding.py
uv run pytest -q ditto/tests/api_models/test_coding.py
```
