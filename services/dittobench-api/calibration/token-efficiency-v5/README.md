# DittoBench v5 token-waste calibration

This directory pins the reproducible reference-harness contract and the
reviewable calibration output used by the v5 relay-token waste penalty.

- `contract.json` binds the exact scorer, stock starter kit, screened image,
  source archive, provider profiles, and locked model identities.
- `summary.json` contains all 120 sanitized per-run observations plus the six
  provider/run-size distributions. It intentionally excludes benchmark
  prompts, expected answers, per-case results, and transcripts.
- `internal/efficiency/baselines_v5.json` is generated from the same audited
  reports and contains the nearest-rank p90 budgets used at runtime.

The repository skill owns the end-to-end workflow:

```sh
export DITTOBENCH_CALIBRATION_ROOT=/path/to/prepared-calibration-root
python3 skills/dittobench-token-calibration/scripts/calibrate.py status
python3 skills/dittobench-token-calibration/scripts/calibrate.py summary \
  --output calibration/token-efficiency-v5/summary.json
python3 skills/dittobench-token-calibration/scripts/calibrate.py baseline \
  --enable-scoring \
  --output internal/efficiency/baselines_v5.json
```

Every successful model call must carry provider-returned usage at the trusted
relay boundary. Caller-abandoned requests and native upstream retry attempts do
not add tokens to a successful observation; missing usage or exhausted retries
make the run ineligible rather than estimating or trusting miner telemetry.
Provider latency remains audit-only and is not part of the score.
