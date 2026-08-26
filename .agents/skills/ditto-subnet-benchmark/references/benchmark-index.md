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
| Screening wire protocol | `packages/ditto-screening-protocol/` |
| Screening worker policy/gates | `workers/screener/` |
| Third-party adapters | `services/dittobench-api/integrations/` |
| Local simulator (sessionless + phase-1) | `localstack/`, `.agents/skills/ditto-subnet-preview/references/localstack.md` |
| Foundry overlay / fault proxy | `ditto/preview/`, `preview/`, `.agents/skills/ditto-subnet-preview/references/cheatcodes.md` |
| New or stranded `bench_version` | `.agents/skills/ditto-subnet-bench-version-bump/SKILL.md` |
| LongMem confirmation rollout | `.agents/skills/longmem-confirmation-rollout/SKILL.md` |

## Start with these contracts

- `services/dittobench-api/PROTOCOL.md`
- `services/dittobench-api/docs/seed-and-scoring.md`
- `services/dittobench-api/docs/calibration-trust.md`
- `docs/UNTRUSTED-EXECUTION-RUNBOOK.md`
- `docs/VALIDATOR.md`
- Live overlapping `/run` and `case_concurrency`: [`bench-runtime.md`](bench-runtime.md)

## High-value lookups

```bash
rg -n 'BenchVersion|v8|Composite|Score|capabilit' \
  services/dittobench-api/internal services/dittobench-api/cmd ditto/validator
rg -n 'seed|baseline|run_size|efficiency' \
  services/dittobench-api/internal research/dittobench-datagen
rg -n 'longmemeval|hermes|openclaw' services/dittobench-api/integrations
rg -n 'request_job|submit_score|set_weights' ditto/validator apps/platform/ditto
```

## Validation ladder

```bash
cd services/dittobench-api && go test ./...
uv run pytest ditto/tests/validator ditto/tests/contract -q
uv run pytest ditto/tests/test_compose_stack.py ditto/tests/test_validator_compose.py -q
RELAY_API_KEY=validation-placeholder docker compose config --quiet
```

Run adapter-local tests in their own directory. For LongMemEval:

```bash
cd services/dittobench-api/integrations/longmemeval
python -m pytest -q
```

## Interpretation boundaries

- A green adapter run proves adapter plumbing, not a production scoring change.
- A built image proves construction, not deployed revision or runtime behavior.
- A heartbeat proves liveness, not correctness of the screened image or score version.
- Retain raw manifests, exact SHAs, model/provider identity, seeds, and scorer output with research conclusions.
