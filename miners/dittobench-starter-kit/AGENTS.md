# Miner starter kit

This directory is the SN118 miner harness, not the validator.

Load `/mine` (`.agents/skills/mine/SKILL.md`) before editing, scoring, or
uploading. `cargo run -- evaluate` is a local name-only subset. Real rehearsal
is, from the repository root:

```bash
uv run ditto practice --run-size small    # smoke
uv run ditto practice --run-size medium   # development
uv run ditto practice --run-size full     # on-chain envelope; required before upload
```

Those default to live bench 12 with a validator-visible `tool_endpoint`.
Hosted rehearsal and `evaluate` 1.0 scores do not predict leaderboard
`tool_mean`. Do not skip `full`. Before `full`, packaging, or upload, `/mine`
walks the served path against the operator review bar.

Before upload, replay the public Bench v13 gates (shadow):
`uv run ditto practice --bench-version 13 --gates`. A would-zero note is a
served-path defect; fix it, never tune it away. The rehearsal refuses a
`--bench-version` the local scorer build does not advertise (its
`/v1/capabilities` `supported_bench_versions`); 13 needs a scorer that
advertises it.
