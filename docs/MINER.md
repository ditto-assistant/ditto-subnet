# Miner guide (SN118)

SN118 is a best-artifact competition. Miners improve a Rust agent-memory
harness, practice locally, and submit the complete crate for independent
validators to score. You are rewarded for improving the best artifact, not for
serving live inference.

> **Start in the
> [`dittobench-starter-kit`](https://github.com/ditto-assistant/dittobench-starter-kit).**
> It is the harness you edit, the local practice environment, and the crate you
> package. You do not need `ditto-subnet`, Python, a wallet, or TAO until you are
> ready to verify and submit to Finney.

## Contents

- [Build and practice locally](#build-and-practice-locally)
- [Prepare for mainnet](#prepare-for-mainnet)
- [Install the submission CLI](#install-the-submission-cli)
- [Verify and submit](#verify-and-submit)
- [Track your submission](#track-your-submission)
- [Scoring and emissions](#scoring-and-emissions)
- [What counts as cheating](#what-counts-as-cheating)
- [Common questions](#common-questions)

## Build and practice locally

Follow the starter kit's
[`SETUP.md`](https://github.com/ditto-assistant/dittobench-starter-kit/blob/main/SETUP.md)
for Rust, model, and embedding setup. The shortest path is:

```sh
git clone https://github.com/ditto-assistant/dittobench-starter-kit
cd dittobench-starter-kit
cp .env.example .env

cargo run -- seed-user          # one-time local memory setup
cargo run -- mem-eval --k 10   # fast retrieval test; no chat-model call
cargo run -- evaluate           # fixed local benchmark for iteration
cargo run -- practice --n 20   # rotating cases, closer to production
```

Edit and test this repository until you are ready to submit. Docker is strongly
recommended for the final local check because production screening builds your
crate as an image and probes `GET /health` on port 8080.

Package the complete crate:

```sh
cargo run -- submit
```

This creates `dittobench-submission.tgz`. It does **not** make an on-chain
submission or charge a fee. Do not package `.env` or any API or wallet secret.

## Prepare for mainnet

To submit, you need:

- Python 3.11+ and [`uv`](https://docs.astral.sh/uv/)
- a funded Bittensor coldkey
- a hotkey registered on Finney netuid 118
- enough TAO for the dynamic evaluation fee

The coldkey pays the fee. The hotkey signs the artifact and receives incentive.
Never put wallet secrets in the crate.

## Install the submission CLI

Clone `ditto-subnet` only when you are ready to verify, upload, or check status:

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet
uv sync
```

Miner submission does not require the validator's `.env` or Docker Compose
stack.

## Verify and submit

From `ditto-subnet`, verify the tarball before paying:

```sh
uv run ditto verify \
  --path ../dittobench-starter-kit/dittobench-submission.tgz
```

The archive must be a gzip-compressed tarball no larger than 20 MiB, with the
crate and `Dockerfile` at its root. It must use safe relative paths and contain
no links or special files.

Submit to Finney:

```sh
uv run ditto --network finney upload \
  --path ../dittobench-starter-kit/dittobench-submission.tgz \
  --name my-agent \
  --coldkey default \
  --hotkey default
```

The CLI runs preflight, checks eligibility and live pricing, asks for
confirmation, pays on chain, signs the artifact digest, uploads the archive,
and prints the agent ID. Use `-y` only when automation is intended to accept the
live fee without an interactive confirmation.

Keep the payment proof (`block_hash`, `block_number`, `extrinsic_index`) if an
upload fails after payment. Each proof is single-use and bound to the signed
artifact digest.

## Track your submission

```sh
uv run ditto --network finney status <agent-id>
```

You can also follow the public submission pipeline and leaderboard at
[`platform-api.heyditto.ai`](https://platform-api.heyditto.ai/).

The normal pipeline is upload, automated build and health screening, evaluation
by up to three independent validators, and median-score finalization. Failed or
expired validator leases are retried, so one validator does not control the
result.

## Scoring and emissions

- DittoBench generates fresh tool-use and memory-recall cases for each
  submission. Production locks every harness to Qwen3-32B in a TEE; your local
  practice key and model are not included in the submitted crate.
- Every scored run starts with a reachability preflight: the validator sends
  one probe case whose `case_id` begins with `preflight:`, and your harness
  must answer it by POSTing one tool call (`search_web` with any args is
  sufficient) to the advertised `tool_endpoint`. Hard-code this on the
  `case_id` prefix — do not rely on your model deciding to call the tool. A
  run whose probe is never observed is retried instead of scored. See the
  scoring engine's `PROTOCOL.md` ("Reachability preflight").
- Grading is deterministic and judge-free. Tool and memory means contribute
  equally to the composite; bounded efficiency, consistency, and integrity
  checks can reduce it.
- Each miner competes with its highest eligible score. A challenger dethrones
  the incumbent only after clearing the greater of the 2% relative margin and
  the statistical error band; a near-miss is settled by re-scoring both agents
  on shared seeds rather than dataset luck.
- The champion receives 90% of competitive weight; the next four distinct
  miners split 10%. The competitive vector receives 20% of available miner
  emission and the remaining 80% is burned. With no eligible miners, 100% is
  burned.

Scores, signatures, and each run's graded transcript are published so results
can be independently checked: regenerate the dataset from the published seed,
re-run the public grader over the transcript, and the numbers must match the
signed composite.

## What counts as cheating

Your submission must be a general model-backed agent, not a program designed to
recognize or emulate the benchmark. Cheating includes benchmark-specific lookup
tables or static dispatch, embedded evaluator logic or answer fixtures,
fabricated tool trajectories, seed or state shortcuts, bypassing the locked
model/provider path, and instructions intended to manipulate screening.

Forking, replacing, or heavily optimizing the public starter harness is
allowed; copying another miner's work is not. Lexical and structural
fingerprints detect renamed, reformatted, or padded near-duplicates across
miners, and suspicious or matching submissions are quarantined for human review
rather than automatically banned. Confirmed plagiarism can result in a
hotkey-level ban.

## Common questions

**How much does evaluation cost?** The fee is dynamic. The CLI fetches and shows
the exact TAO amount before confirmation.

**How long does scoring take?** Screening and a full benchmark both involve
container work. Expect minutes to hours depending on queue and build time.

**Can I submit more than once?** Yes. Reuse the same hotkey and exact agent name
to version an agent (`v1`, `v2`, and so on); a different name starts a new
series. Every upload pays its own fee, and your highest eligible version
represents your hotkey, so a lower-scoring or failed update never replaces your
current best. The CLI saves the name after a successful upload and reuses it as
that hotkey's default.

**What earns emissions?** A material, reproducible improvement over the current
champion. Small gains below the dethroning gate do not take the crown.

**What happens if my hotkey is deregistered after I submit?** Your submission,
scores, and payment record are kept, but a hotkey absent from the SN118
metagraph cannot receive weight and is excluded from the weight fold.
Registering the same hotkey again restores eligibility automatically. A
different hotkey is a separate miner identity and requires a new signed, paid
upload.
