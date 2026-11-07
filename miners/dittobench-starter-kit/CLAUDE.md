# Miner starter kit

Load `/mine` before editing, scoring, or uploading this harness.

`cargo run -- evaluate` is not on-chain scoring. From the repository root,
`uv run ditto practice --run-size small|medium|full` is the real rehearsal
(the live scoring bench, 17, observed tools). `full` is the on-chain envelope and is required
before upload. It still uses local `.env` inference.

Bench 17 (dueling) is live. A rehearsal at 16+ builds a local generator slice
from the public default generator only, and prints the composite, the per-column
means and the priced cost factor (v17: $0.50 reference per 350 cases, 0.3
floor).

Read `.agents/skills/mine/SKILL.md`.

Before upload, replay the public Bench v13 gates (shadow):
`uv run ditto practice --gates`. A would-zero note is a
served-path defect; fix it, never tune it away.
