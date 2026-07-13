# Miner guide (SN118)

SN118 is a best-artifact competition. Miners submit a Rust crate built on
[`ditto-harness`](https://github.com/ditto-assistant/ditto-harness); independent
validators build it in a sandbox, benchmark tool use and memory recall, and set
weights from the public score ledger. You are rewarded for improving the best
artifact, not for serving live inference.

## Contents

| Prepare | Submit | Understand |
| --- | --- | --- |
| [Requirements](#requirements) | [Install and configure](#install-and-configure) | [Prepare and verify](#prepare-and-verify-an-agent) |
| [Submit](#submit) | [Track status](#track-status) | [Scoring](#scoring) |
| [Emissions](#emissions) | [Duplicate protection](#duplicate-protection) | [Common questions](#common-questions) |

## Requirements

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- A funded Bittensor coldkey and a hotkey registered on netuid 118
- A `.tar.gz` agent crate based on the
  [`dittobench-starter-kit`](https://github.com/ditto-assistant/dittobench-starter-kit)

The coldkey pays the dynamic evaluation fee. The hotkey signs the artifact and
receives incentive. Never put wallet secrets in the submission.

## Install and configure

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
uv sync
cp .env.example .env
```

The CLI's `--network` option selects a locked platform API and subtensor pair so
they cannot drift. `--chain-endpoint` overrides only the chain endpoint, which is
useful for a hosted local subtensor.

## Prepare and verify an agent

The archive must contain one Rust crate at its root, including `Cargo.toml`,
`Cargo.lock`, and source files. It must be a gzip-compressed tarball, use safe
relative paths, avoid links and special files, and stay under the upload limit.

Run local preflight before paying:

```sh
uv run ditto verify --path agent.tar.gz
```

Preflight validates archive safety and structure without chain or API calls.
Some deeper manifest, dependency-allowlist, and schema checks currently report
`DEFERRED`; do not treat those as server acceptance. For closer screening parity,
build the tarball as Docker context, run the image, and check its `/health`
endpoint.

## Submit

```sh
uv run ditto --network finney upload \
  --path agent.tar.gz \
  --name my-agent \
  --coldkey default \
  --hotkey default
```

The CLI performs local preflight, checks eligibility and live pricing, requests
confirmation, pays on chain, signs the artifact digest with the hotkey, uploads
the archive, and prints the agent ID. Use `-y` only in automation after setting a
maximum acceptable fee.

Keep the payment proof (`block_hash`, `block_number`, `extrinsic_index`) if an
upload fails after payment. Each proof is single-use and bound to the signed
artifact digest.

## Track status

```sh
uv run ditto --network finney status <agent-id>
```

The normal pipeline is upload, automated screening, validator evaluation, and
ledger finalization. Screening includes an isolated build and health check. Up
to three independent validators score a submission; the platform finalizes the
median. Failed leases expire and can be retried, so one validator does not own
the result.

The platform exposes aggregate health, leaderboard, and signed score-ledger
data. A score signature binds validator hotkey, agent ID, run ID, composite, and
seed, making results independently verifiable.

## Scoring

DittoBench evaluates seeded tool-use and memory-recall cases against the locked
Qwen3-32B model (`Qwen/Qwen3-32B-TEE` through the fleet-standard Chutes relay).
Grading is deterministic and uses no judge model:

- Tool cases score tool selection, arguments, order, and unnecessary calls.
- Memory cases use answer-type-specific rules with distractor and forbidden-value
  checks.
- Efficiency and consistency multiply the aggregate score; a canary miss can
  disqualify the run.
- Reports include the dataset seed so a run can be reproduced and challenged.

Per-case and per-run budgets stop looping agents from consuming unbounded model
calls. Miners do not provide an LLM key for production scoring; use your own key
or local model only for practice.

## Emissions

Weights use a copy-resistant king-of-the-hill mechanism:

1. Each miner is represented by its highest eligible score.
2. A challenger dethrones the incumbent only after clearing the greater of a 5%
   relative margin and the configured statistical error band.
3. Ties and sub-margin gains keep the first-seen incumbent.
4. The champion receives 90% of weight; the next four distinct miners split the
   remaining 10%.

Weights are recomputed from the durable ledger each epoch, so a champion keeps
earning until dethroned. Every validator runs the deterministic fold in
`ditto/validator/weights.py`; Yuma consensus clips deviating vectors. Consensus
parameters can change, so the implementation and live chain are authoritative.

## Duplicate protection

An exact copy cannot clear the dethroning margin. Upload-time lexical and
score-time structural fingerprints also detect renamed, reformatted, or padded
near-duplicates. Cross-miner matches are held for human review rather than
automatically banned. Confirmed plagiarism can result in a hotkey-level ban.
Building on the public reference harness is expected; copying another miner's
work is not.

## Common questions

**How much does evaluation cost?** The fee is dynamic. The CLI fetches and shows
the exact TAO amount before confirmation.

**How long does scoring take?** Screening and a full benchmark both involve
container work. Expect minutes to hours depending on queue and build time.

**Can I submit more than once?** Yes. Every upload pays its own fee, and your
highest eligible agent represents your hotkey in the weight fold.

**What earns emissions?** A material, reproducible improvement over the current
champion. Small gains below the dethroning gate do not take the crown.

**Where do emissions come from?** Validators set weights, Yuma consensus combines
them, and standard Bittensor emission accrues to miner hotkeys.

## Code references

`ditto/miner_cli/` · `ditto/screener/` ·
`ditto/validator/{worker,dittobench,signing,weights}.py` ·
`ditto/api_models/{upload,validator,screener,agent_status}.py`
