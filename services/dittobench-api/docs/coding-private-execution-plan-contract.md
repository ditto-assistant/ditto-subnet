# Private coding execution-plan contract

`internal/codingexecution` freezes the task-static authority that the future
private supervisor phase runner needs but current leases do not yet deliver.
It is a contract and projector only: it has no catalog reader, lease endpoint,
artifact credential, workspace, harness, listener, worker, scorer, or weight
path.

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

The shared vector is synthetic and uses no repository, patch, memory, usable
capability, provider credential, signing key, or private catalog record. No
Platform model or endpoint consumes these plans yet. The next PR must commit
the authoring plan digest in the private catalog, return only the runner plan
through the authoring lease, and return the protected plan/resource projection
only after an accepted freeze through the grading lease.

No production composition root imports this package. Coding contract v1 stays
permanently `weight_eligible=false`.

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
