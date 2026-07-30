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
  submission. Production locks every harness to one consensus model, so model
  choice is not a miner lever: on the current contract (**Bench v7**) that is
  **`openai/gpt-oss-20b`**, served through the platform-owned OpenRouter
  inference boundary with reasoning pinned on. Tune your prompting and
  reasoning budget for the active benchmark model; `GET
  /api/v1/public/bench/config` reports the authoritative contract. Your local
  practice key and model are not included in the submitted crate.
- The validator self-checks its `tool_endpoint` before scoring. A platform
  listener failure aborts and retries the run; if the listener is healthy and
  your harness never calls it, the run completes with the corresponding
  low/zero tool score and transcript evidence. New harnesses do not need a
  synthetic `preflight:` handler. The prefix remains reserved and older
  handlers continue to be accepted for compatibility. See the scoring engine's
  `PROTOCOL.md` ("Tool endpoint reachability").
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
earlier submission starts looking like someone else's work to the screener. An
owner-link attestation is the self-serve fix, and it replaces asking in Discord.

Use it when you have already rotated keys and a new submission is being
copy-flagged against work you submitted from the other hotkey. A normal upload
does not need one.

For a short copy-paste runbook, including the exact command for signing both
sides with coldkeys, see [Link hotkeys after rotating wallets](OWNER-LINKS.md).

### Both ends sign, and either key can prove an end

The link is symmetric: it says *these two hotkeys are the same operator*. There
is no "from" and no "to". **Both** ends must sign, because a one-sided link
would let anyone name a hotkey they do not control and then resubmit that
miner's work under cover of the link. Both wallets therefore have to be on the
machine you run this from.

Each end proves itself with **either** of two keys, chosen per side:

- **`hotkey`** (the default) signs with the hotkey being linked. It proves
  control of the exact key named in the link.
- **`coldkey`** signs with the coldkey that owns that hotkey. The platform
  reaches the claim through the coldkey→hotkey binding it already knows from
  your payment records.

Both are accepted. They differ in key strength, not validity.

### Signing with a coldkey does not move any TAO

Say it plainly, because the coldkey is the key that *can* move funds: **signing
an attestation is not a transfer.** The CLI hands your key a short text string
and stores the signature. It does not build an extrinsic, it does not submit
one, and the chain never sees it. Your balance, your stake, and your
delegations are untouched. The only thing a coldkey proof costs you is typing
the keyfile password so the key can be decrypted long enough to sign.

### Which key to use

Pick per side, based on what you still hold:

| Situation | How to prove that side |
| --- | --- |
| You hold the hotkey's key | `hotkey` (the default) |
| You lost the old hotkey's key but still hold the coldkey that owned it | `coldkey` |
| You rotated coldkeys but kept the hotkey | `hotkey` |
| You hold both | `hotkey` — it is the more direct proof |

A `coldkey` proof requires that hotkey to have a **payment record** on the
platform binding it to that coldkey; that binding is what makes the proof mean
anything, and it is learned from on-chain payment proofs rather than from your
attestation. A hotkey that never paid for an evaluation has no such record, so
sign that side with the hotkey itself.

### Mint the link

```sh
uv run ditto --network finney attest \
  --coldkey miner \
  --hotkey default \
  --other-coldkey old-miner \
  --other-hotkey-name default
```

`--coldkey` / `--hotkey` name **this** side's wallet; `--other-coldkey` /
`--other-hotkey-name` name the **other** side's. Which is which does not matter
to the result — the CLI sorts the two hotkeys into a canonical order and signs
each half for the side it lands on. Add `--key-kind coldkey` or
`--other-key-kind coldkey` to prove a side with its coldkey instead; both
default to `hotkey`.

After each successful upload, the CLI remembers only the local wallet names,
public hotkey address, network, and stable agent name (never key material). Like
the main Ditto CLI, this is user-scoped config under the home directory:
`$XDG_CONFIG_HOME/ditto/config.json`, or `~/.config/ditto/config.json` when XDG
is unset. The file is written atomically with mode `0600`; operators and tests
may override it with `DITTO_CLI_CONFIG_PATH`.

If a later upload reuses that agent name from a different wallet, an interactive
CLI offers to sign and submit this same direct owner link before payment.
Declining does not block the upload. Non-interactive uploads never sign
implicitly; they print the exact `ditto attest` command to run separately.

The CLI loads both wallets, mints a single-use nonce, signs both halves, prints
what the link does, asks for confirmation, and submits. Add `-y` to skip the
prompt in a script. `--netuid` defaults to 118 and is signed into both
payloads, so an attestation minted for one subnet cannot be replayed onto
another.

### Worked example

You mined from `old-miner`/`default` through submission v3, rotated to a fresh
coldkey `miner`/`default`, and your first upload from the new hotkey came back
held for copy review against your own v3. You still have both keyfiles:

```sh
uv run ditto --network finney attest \
  --coldkey miner \
  --hotkey default \
  --other-coldkey old-miner \
  --other-hotkey-name default
```

```
Owner-link attestation
  Netuid:     118
  Hotkey lo:  5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy
    proved by hotkey: 5DAAnrj7VHTznn2AWBemMuyBwZWs6FNFjdyVXUeYum3PTXFy
  Hotkey hi:  5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y
    proved by hotkey: 5FLSigC9HGRKVhB9FiEo4Y3koPsNmBmLJbpXg2mp1hXcS59Y

Signing does NOT transfer any TAO...
Submit this attestation? [y/N]: y
3f6b1c04-5a7e-4a2f-9c31-0d8b2e4f7a15
```

Same situation, except the old hotkey's keyfile is gone and you only have the
old coldkey. Prove that side with the coldkey:

```sh
uv run ditto --network finney attest \
  --coldkey miner \
  --hotkey default \
  --other-coldkey old-miner \
  --other-hotkey-name default \
  --other-key-kind coldkey
```

The old hotkey does not have to still be registered on SN118 — the link only
ever reaches submissions that hotkey already made, and those are immutable
history.

The UUID on stdout is the attestation ID; quote it if you open a review ticket.
The link takes effect from the moment it is recorded, so it applies to
screening that runs after it, not to a decision already made. If a submission
is already held, mint the attestation and then ask for the review to be re-run.

### Read the scope literally

- It exempts each linked hotkey from plagiarism screening **against the other
  hotkey's earlier work only**. Screening against every other miner is
  unchanged.
- It does **not** grant an additional emission slot. Emission positions are one
  per distinct agent no matter how many keys you hold, and this link is not an
  input to that calculation. Rotating keys and attesting the link does not put
  two of your agents in the weight vector.
- It does **not** permit byte-identical or repacked resubmission. Re-uploading
  the same artifact under a new key is still held, with or without a link.
- Links are recorded and auditable. If a key is sold or compromised, contact
  an operator and quote the attestation ID so the link can be revoked. The
  current miner CLI creates links; it does not provide self-service revocation.
- The **evidence grade** — `coldkey-coldkey`, `mixed`, or `hotkey-hotkey`,
  reported back when the link is recorded — describes which key kinds proved
  the two halves. It is reviewer context. It does **not** change whether the
  exemption applies; all three grades establish the link identically.

### Links are direct only

A link covers exactly the two hotkeys it names. It is **not transitive**:
attesting `A`–`B` and `B`–`C` does not link `A` and `C`, because those two
owners never signed anything with each other. If you have rotated more than
once, attest each pair you actually need — for a chain of three hotkeys where
old work under `A` collides with new work under `C`, mint `A`–`C` directly.

### Signing on an offline machine

If both wallets live on a machine that should not reach the platform, use
`--print-only` to sign both halves and print the request body instead of
submitting it. This is not split signing: both wallets must be available to the
same command invocation.

```sh
uv run ditto --network finney attest \
  --coldkey miner \
  --hotkey default \
  --other-coldkey old-miner \
  --other-hotkey-name default \
  --print-only > attestation.json
```

Move `attestation.json` to a networked machine and POST it yourself:

```sh
curl --fail-with-body \
  -H 'Content-Type: application/json' \
  --data-binary @attestation.json \
  https://platform-api.heyditto.ai/api/v1/attestations/owner-link
```

Minted attestations expire, so submit it the same day or mint a fresh one.

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
