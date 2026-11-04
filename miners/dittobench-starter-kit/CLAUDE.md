# Miner starter kit

Load `/mine` before editing, scoring, or uploading this harness.

`cargo run -- evaluate` is not on-chain scoring. From the repository root,
`uv run ditto practice --run-size small|medium|full` is the real rehearsal
(the live scoring bench, 15, observed tools). `full` is the on-chain envelope and is required
before upload. It still uses local `.env` inference.

Bench 16 (dueling) is shadow, not live: `--bench-version 16` adds a local
generator slice built from the public default generator only, and prints the
composite plus per-column means.

Read `.agents/skills/mine/SKILL.md`.

Before upload, replay the public Bench v13 gates (shadow):
`uv run ditto practice --gates`. A would-zero note is a
served-path defect; fix it, never tune it away.
