# Miner starter kit

Load `/mine` before editing, scoring, or uploading this harness.

`cargo run -- evaluate` is not on-chain scoring. From the repository root,
`uv run ditto practice --run-size small|medium|full` is the real rehearsal
(the live scoring bench, 18, observed tools). `full` is the on-chain envelope and is required
before upload. It still uses local `.env` inference.

Bench 18 (the accuracy contract on the dueling bench) is live. Its 1,400 to
2,000-tool menu does not fit the locked model's context, so the kit serves the
Jev hybrid for an oversized menu (`src/hybrid.rs`). A rehearsal at 16+ builds a
local generator slice from the public default generator only, and prints the
composite, the per-column means, the priced cost factor ($0.50 reference per 350
cases, 0.3 floor) and the v18 run budget ($1.00 per 350 cases).

Read `.agents/skills/mine/SKILL.md`.

Before upload, replay the public Bench v13 gates (shadow):
`uv run ditto practice --gates`. A would-zero note is a
served-path defect; fix it, never tune it away.
