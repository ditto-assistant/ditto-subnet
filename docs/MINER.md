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
- [Link a rotated hotkey](#link-a-rotated-hotkey)
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

The CLI runs preflight and obtains a short-lived platform admission reservation
before it displays pricing or sends payment. This makes the owner cooldown and
concurrent submissions fail before funds move. It then asks for confirmation,
pays on chain, uploads the signed archive, and prints the agent ID. Use `-y`
only when automation is intended to accept the live fee without an interactive
confirmation.

The CLI saves a finalized payment proof locally before uploading and retries
short-lived gateway and service failures automatically. If every attempt fails,
run the same command again with the same local tarball, hotkey, and agent name;
the CLI detects the pending proof and does not submit another transfer. It
stores only the proof and a hash of the upload identity—never tar contents,
source paths, or an artifact URL.

For a payment made by an older CLI, or when moving recovery to another machine,
use the printed proof (`block_hash`, `block_number`, `extrinsic_index`):

```sh
uv run ditto --network finney upload \
  --path ../dittobench-starter-kit/dittobench-submission.tgz \
  --name my-agent \
  --coldkey default \
  --hotkey default \
  --payment-block-hash 0x... \
  --payment-block-number 123456 \
  --payment-extrinsic-index 7
```

All three recovery flags are required together. The proof is single-use and
bound to the authenticated artifact; an exact retry returns the original agent
ID if the first response was lost. Never run a normal upload merely to recover
from a post-payment error, because that would submit a second transfer.
Recovery still obtains a fresh admission reservation. If the platform reports a
cooldown, wait until its exact UTC retry time and rerun the same recovery
command; the existing proof remains reusable and no new transfer is sent.

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
  the incumbent only after clearing the greater of a fixed 0.007 composite-point
  hysteresis and the statistical error band. From Bench v6 onward, that whole
  band shrinks smoothly once the incumbent exceeds 0.60, keeping the crown
  contestable as scores approach the benchmark ceiling. A near-miss is settled
  by re-scoring both agents on shared seeds rather than dataset luck.
- Competitive weight is distributed 65% / 14% / 10% / 7% / 4% to the champion
  and next four distinct miners, respectively. The competitive vector receives
  100% of available miner emission — nothing is burned while eligible miners
  exist. With no eligible miners, 100% is burned.
- Those five slots are ordered by the score that settles a near-miss — the mean
  including shared-seed re-scores — not by the raw composite shown next to your
  entry. The two orders usually agree and occasionally do not, so the slot you
  land in may not match a rank computed from raw composites alone.
- **A new score does not reach the chain immediately. Budget two to three
  tempos — roughly 2.5 to 4.5 hours — from your first score to visible
  incentive.** See "When will I see incentive on chain?" below.

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

## Link a rotated hotkey

Copy screening compares each submission against earlier work from other miners.
The exemption that keeps it from flagging your *own* history is keyed on the
wallet that paid, so once you rotate to a new coldkey or hotkey, your own
earlier submission starts looking like someone else's work to the screener. A
rotation attestation is the self-serve fix, and it replaces asking in Discord.

Use it when you have already rotated keys and a new submission is being
copy-flagged against work you submitted from the old hotkey. A normal upload
does not need one.

Both keys sign. The old hotkey signs an attestation that the new hotkey
continues it; the new hotkey counter-signs an acceptance over the same nonce.
The second signature is what stops anyone from naming a hotkey they do not
control as their successor, so both wallets must be present on the machine you
run this from. The old hotkey does not have to still be registered on SN118 —
the link only ever reaches submissions that hotkey already made, and those are
immutable history.

```sh
uv run ditto --network finney attest \
  --old-coldkey old-miner \
  --old-hotkey-name default \
  --coldkey miner \
  --hotkey default
```

The CLI loads both wallets, mints a single-use nonce, signs both halves, prints
what the link does, asks for confirmation, and submits. Add `-y` to skip the
prompt in a script. `--netuid` defaults to 118 and is signed into both payloads,
so an attestation minted for one subnet cannot be replayed onto another.

Worked example. You mined from `old-miner`/`default` through submission v3,
rotated to a fresh coldkey `miner`/`default`, and your first upload from the new
hotkey came back held for copy review against your own v3:

```sh
uv run ditto --network finney attest \
  --old-coldkey old-miner \
  --old-hotkey-name default \
  --coldkey miner \
  --hotkey default
```

```
Hotkey rotation attestation
  Netuid:      118
  Old hotkey:  5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y
  New hotkey:  5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy
...
Submit this attestation? [y/N]: y
3f6b1c04-5a7e-4a2f-9c31-0d8b2e4f7a15
```

The UUID on stdout is the attestation ID; quote it if you open a review ticket.
The link takes effect from the moment it is recorded, so it applies to screening
that runs after it, not to a decision already made. If a submission is already
held, mint the attestation and then ask for the review to be re-run.

Read the scope literally:

- It exempts the new hotkey from plagiarism screening **against the old
  hotkey's earlier work only**. Screening against every other miner is
  unchanged, and the exemption runs one way: your old hotkey is not exempted
  against your new one.
- It does **not** grant an additional emission slot. Emission positions are one
  per distinct agent no matter how many keys you hold, and this link is not an
  input to that calculation. Rotating keys and attesting the link does not put
  two of your agents in the weight vector.
- It does **not** permit byte-identical or repacked resubmission. Re-uploading
  the same artifact under a new key is still held, with or without a link.
- Links are recorded, auditable, and revocable. They are visible to reviewers,
  and one can be revoked if a key is sold or compromised.

If the old key lives on a machine that should not reach the platform, use
`--print-only` to sign both halves and print the request body instead of
submitting it:

```sh
uv run ditto --network finney attest \
  --old-coldkey old-miner \
  --old-hotkey-name default \
  --coldkey miner \
  --hotkey default \
  --print-only > attestation.json
```

Move `attestation.json` to a networked machine and POST it to
`/api/v1/attestations/hotkey-rotation` yourself. Minted attestations expire, so
submit it the same day or mint a fresh one.

## Common questions

**How much does evaluation cost?** The fee is dynamic. The CLI fetches and shows
the exact TAO amount before confirmation.

**How long does scoring take?** Screening and a full benchmark both involve
container work. Expect minutes to hours depending on queue and build time.

**When will I see incentive on chain?** Budget **two to three tempos — roughly
2.5 to 4.5 hours** — from your first score. It is not one tempo, and the delay
is chain mechanics rather than anything queued on our side:

- Your score needs a quorum of three independent validators.
- Each validator rebuilds and submits its weight vector once per tempo
  (72 minutes on SN118), and validators run on independent phases — some will
  pick you up in their next cycle, others not until the one after.
- Weights are committed, then revealed one epoch later, so a submission is not
  visible on chain until the following epoch boundary.
- Incentive is only recomputed by consensus at an epoch boundary.
- Consensus clips at half the validator stake. **Until more than 50% of stake
  carries you, your incentive is exactly 0.00000 — not a small number that
  grows.** This is the part that surprises people: it looks like nothing is
  happening, then it flips to full value in a single block.

So a run of zeros followed by a sudden jump is the system working normally, not
a stuck payout. If you are still at zero more than four hours after your first
score, that is worth reporting.

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
