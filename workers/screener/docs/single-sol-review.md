# Report-only single-Sol review

This experiment tests whether one persistent GPT-5.6 Sol tool-using review can
replace the production Luna/Terra/Sol review chain. It is calibration-only: it
does not lease Platform work, change signed screening verdicts, clear or reject
submissions, enable a release setting, or write to Backroom.

The reviewer uses the existing policy-versioned source-review tools and strict
result schema with a larger envelope: 240 model/tool steps, 16 MB of delivered
source, 32,000 completion tokens per turn, high reasoning, and a one-hour
deadline. Each case is SHA-256-bound to an immutable `safe` or `violation` gold
label. Results include classification metrics, latency, provider/model identity,
token usage, reported cost, typed notes, and finding metadata.

## Context compaction

The agent loop keeps the stable system prompt and artifact inventory plus the
three most recent assistant turns. Older turns are replaced with a host-built
checkpoint even if the model has not recorded a note yet. The checkpoint
contains:

- the complete bounded typed-notes ledger;
- bounded inspection provenance (tool and path, never source contents);
- served-path coverage obligations that remain open; and
- consumed step and read-byte budgets.

Dropped tool output is not converted into evidence. The checkpoint explicitly
requires the reviewer to re-read exact source before citing it. This prevents a
long exploratory sequence from growing without bound while keeping clearance
and finding evidence tied to the artifact.

## Run

```bash
uv run --project workers/screener \
  python workers/screener/scripts/run_single_sol_calibration.py \
  --manifest /private/gold/manifest.json \
  --artifact-root /private/gold/artifacts \
  --api-key-file /private/keys/openrouter \
  --results-file /private/results/single-sol.json
```

The manifest uses the report-only calibration shape:

```json
{
  "revision": "immutable-gold-v1",
  "items": [
    {
      "agent_id": "exact-agent-uuid",
      "archive": "relative/path/agent.tar.gz",
      "artifact_sha256": "64-lowercase-hex-characters",
      "expected_disposition": "safe"
    }
  ]
}
```

Use `--artifact-sha256` for a bounded retry and `--concurrency 1` (the default)
for attributable latency and cost. A replacement decision requires a measured
corpus comparison against the current chain: false clears, false holds, missed
rejections, inconclusive rate, evidence validity, latency, and cost. Unit tests
or a reader replay are not production-readiness evidence.
