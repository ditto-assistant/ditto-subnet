# Benchmark and scoring index

## Pipeline map

| Stage | Canonical paths |
|---|---|
| Platform leasing and result intake | `apps/platform/ditto/api_server/endpoints/validator.py`, `apps/platform/ditto/db/queries/` |
| Validator orchestration and signing | `ditto/validator/` |
| DittoBench service and version | `services/dittobench-api/cmd/dittobench-api/` |
| Scoring implementation | `services/dittobench-api/internal/scorer/`, `services/dittobench-api/internal/efficiency/` |
| Untrusted runtime | `services/dittobench-api/internal/sandbox/`, `Dockerfile.sandbox-docker` |
| Inference relay | `services/dittobench-api/cmd/provider-relay/`, scorer broker files |
| Deterministic datagen | `research/dittobench-datagen/` |
| Miner reference harness | `miners/dittobench-starter-kit/` |
| Shadow coding practice/runtime | `research/dittobench-coding-datagen/` |
| Shadow coding-agent harness | `miners/dittobench-coding-starter-kit/` |
| Shadow coding runner/freezer core | `services/dittobench-api/internal/codingrunner/` |
| Shadow pristine coding grader | `services/dittobench-api/internal/codinggrader/` |
| Shadow coding sandbox executor | `services/dittobench-api/internal/codingexecutor/` |
| Shadow private catalog selector | `apps/platform/ditto/coding_selection.py` |
| Shadow private catalog loader | `apps/platform/ditto/api_server/coding_private_catalog.py` |
| Shadow private task inputs | `CodingPrivateCatalogRecord` in `apps/platform/ditto/api_models/coding_selection.py` |
| Shadow selection assignment ledger | `apps/platform/ditto/db/queries/coding_assignments.py` |
| Shadow finalized run issuer | `apps/platform/ditto/db/queries/coding_issuance.py` |
| Shadow single-run reconciler | `apps/platform/ditto/db/queries/coding_reconciliation.py` |
| Shadow k=3 ticket-set issuer | `apps/platform/ditto/db/queries/coding_ticket_sets.py` |
| Shadow task lease core | `apps/platform/ditto/db/queries/coding_task_leases.py` |
| Shadow artifact capabilities | `apps/platform/ditto/api_server/coding_artifact_capabilities.py` |
| Shadow verified artifact fetcher | `services/dittobench-api/internal/codingartifacts/` |
| Shadow artifact delivery contract | `packages/dittobench-coding-contract/testdata/coding_artifact_capability_v1.json`, `apps/platform/ditto/api_models/coding_artifacts.py`, `services/dittobench-api/internal/codingartifacts/delivery.go` |
| Shadow authoring-lease delivery | `apps/platform/ditto/api_server/endpoints/validator_coding_delivery.py`, `ditto/validator/platform.py` |
| Shadow authoring-freeze ledger | `apps/platform/ditto/api_server/endpoints/validator_coding_freezes.py`, `apps/platform/ditto/db/queries/coding_evaluations.py` |
| Shadow grading-lease delivery | `apps/platform/ditto/api_server/endpoints/validator_coding_grading.py`, `ditto/validator/platform.py` |
| Shadow result submission | `apps/platform/ditto/api_server/endpoints/validator_coding_evaluation.py`, `ditto/validator/platform.py` |
| Shadow attempt coordinator | `ditto/validator/coding_attempt.py`, `docs/coding-shadow-attempt-coordinator.md` |
| Shadow terminal evidence builder | `ditto/validator/coding_terminal.py`, `docs/coding-shadow-terminal-evidence.md` |
| Shadow failure classifier | `ditto/validator/coding_failure.py`, `docs/coding-shadow-failure-classification.md` |
| Screening wire protocol | `packages/ditto-screening-protocol/` |
| Third-party adapters | `services/dittobench-api/integrations/` |

## Start with these contracts

- `services/dittobench-api/PROTOCOL.md`
- `services/dittobench-api/docs/seed-and-scoring.md`
- `services/dittobench-api/docs/calibration-trust.md`
- `docs/UNTRUSTED-EXECUTION-RUNBOOK.md`
- `docs/VALIDATOR.md`
- `research/dittobench-coding-datagen/docs/PRIVATE-EXECUTION-PROTOCOL.md`
- `services/dittobench-api/docs/coding-sandbox-shadow.md`

## High-value lookups

```bash
rg -n 'BenchVersion|v8|Composite|Score|capabilit' \
  services/dittobench-api/internal services/dittobench-api/cmd ditto/validator
rg -n 'seed|baseline|run_size|efficiency' \
  services/dittobench-api/internal research/dittobench-datagen
rg -n 'longmemeval|hermes|openclaw' services/dittobench-api/integrations
rg -n 'request_job|submit_score|set_weights' ditto/validator apps/platform/ditto
rg -n 'weight_eligible|workspace_capability|repair_score_micros' \
  research/dittobench-coding-datagen miners/dittobench-coding-starter-kit
```

## Validation ladder

```bash
cd services/dittobench-api && go test ./...
uv run pytest ditto/tests/validator ditto/tests/contract -q
uv run pytest ditto/tests/test_compose_stack.py ditto/tests/test_validator_compose.py -q
RELAY_API_KEY=validation-placeholder docker compose config --quiet
(cd miners/dittobench-coding-starter-kit && \
  cargo fmt --check && \
  cargo clippy --locked --all-targets --all-features -- -D warnings && \
  cargo test --locked --all-targets --all-features)
bash scripts/test-coding-starter-practice-e2e.sh
(cd miners/dittobench-coding-starter-kit && \
  docker build --tag dittobench-coding-starter-kit:validation .)
```

Run adapter-local tests in their own directory. For LongMemEval:

```bash
cd services/dittobench-api/integrations/longmemeval
python -m pytest -q
```

## Interpretation boundaries

- A green adapter run proves adapter plumbing, not a production scoring change.
- A built image proves construction, not deployed revision or runtime behavior.
- A green coding-practice E2E proves only the public shadow protocol; it does
  not activate a private corpus, production scorer, validator path, or weight.
- A heartbeat proves liveness, not correctness of the screened image or score version.
- Retain raw manifests, exact SHAs, model/provider identity, seeds, and scorer output with research conclusions.
