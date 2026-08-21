# Miner starter kit

Load `/mine` before editing or scoring this harness.

`cargo run -- evaluate` is not on-chain scoring. From the repository root,
`uv run ditto practice --run-size small|medium|full` is the real rehearsal
(bench 11, observed tools). `full` is the on-chain envelope and is required
before upload. It still uses local `.env` inference.

Read `.agents/skills/mine/SKILL.md`.
