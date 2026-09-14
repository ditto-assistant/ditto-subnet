# Miner starter kit

Load `/mine` before editing, scoring, or uploading this harness.

`cargo run -- evaluate` is not on-chain scoring. From the repository root,
`uv run ditto practice --run-size small|medium|full` is the real rehearsal
(bench 12, observed tools). `full` is the on-chain envelope and is required
before upload. It still uses local `.env` inference.

Read `.agents/skills/mine/SKILL.md`.

Before upload, replay the public Bench v13 gates (shadow):
`uv run ditto practice --bench-version 13 --gates`. A would-zero note is a
served-path defect; fix it, never tune it away.
