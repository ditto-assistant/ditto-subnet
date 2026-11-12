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

Those default to the live scoring bench (15, read from the public rollout
endpoint) with a validator-visible `tool_endpoint`.
Hosted rehearsal and `evaluate` 1.0 scores do not predict leaderboard
`tool_mean`. Do not skip `full`. Before `full`, packaging, or upload, `/mine`
walks the served path against the operator review bar.

Before upload, replay the public Bench v13 gates (shadow):
`uv run ditto practice --gates`. A would-zero note is a
served-path defect; fix it, never tune it away. The rehearsal refuses a
`--bench-version` the local scorer build does not advertise (its
`/v1/capabilities` `supported_bench_versions`); 14 needs a scorer that
advertises it.

Bench 18 (the accuracy contract on the dueling bench) is the live scoring
contract: a 1,400 to 2,000-tool menu, multi-call tool encounters and a $1.00 per
350-case run budget, with the kit serving the Jev hybrid for an oversized menu. `uv run ditto
practice` at 16+ builds a local generator slice from the public default
generator only (miner generator columns stay private until their source is
released, so on-chain column means will differ) and prints the composite,
per-column means and the priced cost factor; `--no-slice` scores the datagen
half alone. v18 prices a full run against a $0.50 reference with a 0.3 floor,
so cost per run matters as much as accuracy.