# Miner guide (SN118)

SN118 is a best-artifact competition. Miners improve an agent-memory harness,
practice locally, and submit its complete Docker build context for independent
validators to score. A harness may use Rust, Python, TypeScript, Go, or any
other language that can serve the public HTTP contract. You are rewarded for
improving the best artifact, not for serving live inference.

> **Start in the
> [`miners/dittobench-starter-kit`](../miners/dittobench-starter-kit/README.md).**
> It is the supported Rust reference harness and local practice environment.
> You may use it directly or implement the same container/HTTP contract in
> another language. You do not need `ditto-subnet`, Python, a wallet, or TAO
> until you are ready to verify and submit to Finney.

## Contents

- [Build and practice locally](#build-and-practice-locally)
- [Prepare for mainnet](#prepare-for-mainnet)
- [Install the submission CLI](#install-the-submission-cli)
- [Verify and submit](#verify-and-submit)
- [Track your submission](#track-your-submission)
- [Scoring and emissions](#scoring-and-emissions)
- [What counts as cheating](#what-counts-as-cheating)
- [Link a rotated hotkey](#link-a-rotated-hotkey)
- [Claim a public handle](#claim-a-public-handle)
- [Set a profile picture](#set-a-profile-picture)
- [Sign in to the public miner console](#sign-in-to-the-public-miner-console)
- [Common questions](#common-questions)

## Build and practice locally

Follow the starter kit's [`SETUP.md`](../miners/dittobench-starter-kit/SETUP.md)
for the Rust reference implementation, model, and embedding setup. Its shortest
path is:

```sh
git clone https://github.com/ditto-assistant/ditto-subnet
cd ditto-subnet/miners/dittobench-starter-kit
cp .env.example .env

cargo run -- seed-user          # one-time local memory setup
cargo run -- mem-eval --k 10   # fast retrieval test; no chat-model call
cargo run -- evaluate           # fixed local benchmark for iteration
cargo run -- practice --n 20   # rotating cases, closer to production
```

For the production-shaped generator, scorer, tool observer, and optional
LongMemEval adapter in one command, run from the monorepo root:

```sh
uv run ditto practice --run-size small

# On-chain-shaped envelope (same generator and observed-tool scorer as
# validators; still local .env inference, not the screened image).
uv run ditto practice --run-size full --report /tmp/dittobench-report.json

# Add the separate cleaned LongMemEval-S score (500 questions by default).
uv run ditto practice --run-size small --longmem-eval

# Fast adapter smoke; this is labeled partial practice, not the official score.
uv run ditto practice --run-size small --longmem-eval --longmem-limit 5
```

The command starts and tears down the harness, live-bench (currently 11) scorer,
trusted local reader/judge proxies, and isolated LongMemEval stores. Default
`--run-size small` is a smoke profile, not the on-chain case count. Nothing is
exposed to the public internet. LongMemEval remains a separately labeled
offline score and is never folded into the Bench composite, KOTH rank,
confirmation, or payout.

Coding agents should load `/mine` (`.agents/skills/mine`) instead of treating
`cargo run -- evaluate` as on-chain scoring.

Edit and test this repository until you are ready to submit. If you implement
the contract in another language, use its normal build and test tools. Docker
is required by the submission contract because production screening builds the
provided context as an image and probes `GET /health` on port 8080.

The Rust reference packages its complete build context with:

```sh
cargo run -- submit
```

This creates `dittobench-submission.tgz`. It does **not** make an on-chain
submission or charge a fee. Do not package `.env` or any API or wallet secret.

## Prepare for mainnet

To submit, you need:

- Python 3.12+ and [`uv`](https://docs.astral.sh/uv/)
- a funded Bittensor coldkey
- a hotkey registered on Finney netuid 118
- enough TAO for the platform-controlled evaluation fee (currently 0.04 TAO,
  or 40,000,000 rao)

If the hotkey is not registered yet, you do not have to leave the CLI to fix
it. `ditto upload` runs its pre-check before any TAO moves, and when the only
rejection is code 1101 (`hotkey is not registered on netuid 118`) it reads the
live registration cost from chain, shows it next to your coldkey balance, and
offers to register:

```
  pre-check rejection 1101: hotkey is not registered on netuid 118

Registration preview
  Netuid:   118
  Hotkey:   5FHn...k2Qd
  Coldkey:  miner (5Grw...GKutQY)
  Cost:     0.0005 TAO  (500000 rao)
  Balance:  12.4021 TAO

Register this hotkey now? [y/N]:
```

Answering `y` recycles that amount, re-runs the pre-check, and continues into
the ordinary payment confirmation. The registration cost is never cached or
assumed: it is read at the prompt and read again immediately before the
extrinsic, and the CLI aborts rather than paying more if it rose in between.

The platform resolves registration from a cached metagraph snapshot rather
than from the chain directly, so a registration that is already final on chain
is not visible to `/upload/check` immediately. The CLI waits it out, re-checking
for about four minutes:

```
registered on netuid 118: uid 37
continuing upload...
waiting for the platform to observe the registration (it reads a cached
metagraph snapshot, so this lags the chain)...
  still not visible; re-checking (1/8)
platform observed the registration after 2/8 checks
```

If the wait runs out, the registration still succeeded and the TAO is already
spent — **do not register again**. Re-run the same upload command a few minutes
later; it detects the on-chain registration, recycles nothing, and waits for
the platform again.

`--yes` does **not** cover this prompt. It authorizes the platform-quoted
evaluation fee only, whereas the recycle amount is a separate chain-quoted cost
with no ceiling. For scripted use, pre-authorize it explicitly with
`--register`; use `--no-register` to keep the old behavior of failing the
pre-check. Registration is also still available on its own:

```sh
btcli subnets register --netuid 118 \
  --wallet-name <coldkey> --hotkey <hotkey> --network finney
```

Registering recycles TAO — it is burned, not transferred, and is not refunded
if the agent is never submitted or scores 0. It is also separate from, and
usually larger than, the evaluation fee.

The coldkey pays the fee. The hotkey signs the artifact and receives incentive.
The fee is configured in TAO through Backroom. TAO/USD pricing is recorded only
for internal revenue reporting; it neither determines the amount due nor takes
part in payment or admission validation. Never put wallet secrets in the build
context.

## Install the submission CLI

Return to the `ditto-subnet` root when you are ready to verify, upload, check
status, claim a handle, set a profile picture, or sign in to the public
console:

```sh
cd ../..
uv sync
uv run ditto -h
```

`upload`, `attest`, `name`, and `avatar` each sign one request with the local
wallet (`--coldkey` / `--hotkey`, or `--wallet.name` / `--wallet.hotkey`).
`ditto login` is the separate device grant for the
[dittobench.ai](https://dittobench.ai/#/reviews) miner console and hosted MCP.
It does not replace those one-shot commands.

Miner submission does not require the validator's `.env` or Docker Compose
stack.

## Verify and submit

From `ditto-subnet`, verify the tarball before paying:

```sh
uv run ditto verify \
  --path miners/dittobench-starter-kit/dittobench-submission.tgz
```

The archive must be a gzip-compressed tarball no larger than 20 MiB, with its
`Dockerfile` at the root. It must use safe relative paths and contain no links
or special files. Cargo metadata is part of the Rust reference implementation,
not a submission requirement.

Submit to Finney:

```sh
uv run ditto --network finney upload \
  --path miners/dittobench-starter-kit/dittobench-submission.tgz \
  --name my-agent \
  --coldkey default \
  --hotkey default
```

The CLI runs preflight and obtains a platform admission reservation before it
displays pricing or sends payment. An unpaid reservation gives that coldkey an
exclusive submission slot for 15 minutes, preventing concurrent attempts from
sending multiple transfers. If no payment is finalized, a new attempt may
replace the abandoned reservation after those 15 minutes. It then asks for
confirmation, pays on chain, uploads the signed archive, and prints the agent
ID. Use `-y` only when automation is intended to accept the live TAO fee without
an interactive confirmation.

The CLI saves a finalized payment proof locally before uploading and retries
short-lived gateway and service failures automatically. If every attempt fails,
run the same command again with the same local tarball, hotkey, and agent name;
the CLI detects the pending proof and does not submit another transfer. The
platform keeps that finalized proof recoverable for the same bound upload for
24 hours. This 24-hour window is how long the payment may be reused, not how
long the coldkey is blocked. The CLI stores only the proof and a hash of the
upload identity—never tar contents, source paths, or an artifact URL.

For a payment made by an older CLI, or when moving recovery to another machine,
use the printed proof (`block_hash`, `block_number`, `extrinsic_index`):

```sh
uv run ditto --network finney upload \
  --path miners/dittobench-starter-kit/dittobench-submission.tgz \
  --name my-agent \
  --coldkey default \
  --hotkey default \
  --payment-block-hash 0x... \
  --payment-block-number 123456 \
  --payment-extrinsic-index 7
```

All three recovery flags are required together. The proof may be presented
again during recovery, but it authorizes only its bound, authenticated upload;
an exact retry returns the original agent ID if the first response was lost.
Recovery obtains usable admission immediately during the 24-hour payment
window, even when the coldkey would otherwise be in a submission cooldown, and
does not send a new transfer.

If the operator changes the TAO fee while an older finalized receipt is still
recoverable, the platform first applies its bounded legacy-fee amnesty. Only an
unused receipt tied to a reservation that was active at the fee-change cutover
can qualify, and it must have finalized before that cutover. This is a
receipt-scoped exception, not a coldkey-wide fee waiver.

If a saved receipt is instead definitively rejected for an amount mismatch or
because its 24-hour recovery window expired, the CLI prints the rejected proof
and sends nothing. In an interactive terminal it asks whether to pay the
currently reserved TAO fee. For non-interactive recovery, opt in explicitly:

```sh
uv run ditto --network finney upload \
  --path miners/dittobench-starter-kit/dittobench-submission.tgz \
  --name my-agent \
  --coldkey default \
  --hotkey default \
  --pay-again
```

`--pay-again` is narrowly limited to those two definitive receipt failures.
The CLI runs a fresh pre-check before retiring the rejected local proof or
submitting a replacement transfer. It never overrides a signer, destination,
cooldown, archive, transport, or other validation failure, and `-y` alone does
not authorize abandoning a finalized receipt.

A separate owner submission cooldown can still apply after a genuinely
completed upload. It prevents the same coldkey from paying for a distinct new
submission until the platform's exact UTC retry time. Because the CLI runs the
pre-check first, that rejection happens before funds move. It does not delay
recovery of the already-paid upload described above.

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
  choice is not a miner lever: on the current contract (**Bench v9**) that is
  **`openai/gpt-oss-20b`**, served through the platform-owned OpenRouter
  inference boundary. Reasoning effort is an intentional v9 strategy: a harness
  may request `low`, `medium`, or `high`; omission defaults to `medium`. Tune
  your prompting and reasoning budget for the active benchmark model; `GET
  /api/v1/public/bench/config` reports the authoritative contract. Your local
  practice key and model are not included in the submitted build context.
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
  contestable as scores approach the benchmark ceiling. That smooth decay is
  measured against a perfect score rather than against what is actually left,
  so on a saturated benchmark it could still ask for more than the maximum: a
  0.997012 incumbent required 1.000176. The band is therefore also capped at
  half of the score you still have left to gain, which keeps the requirement
  strictly below the ceiling — a perfect run always takes the crown. The cap
  only ever lowers the bar, never raises it, and the leaderboard's "beat this
  to contend" figure already includes it. A near-miss is settled by re-scoring
  both agents on shared seeds rather than dataset luck.
- Competitive weight is distributed 65% / 14% / 10% / 7% / 4% to the champion
  and next four distinct miners, respectively. Evidence-tied occupied positions
  pool their shares. If at least two highest-scoring miners are evidence-tied and
  the dethrone threshold cannot be exceeded within the score range, they form an
  uncapped joint crown and split the full competitive pool equally, including
  ties beyond the normal top five. The competitive vector receives
  100% of available miner emission by default — nothing is burned while eligible
  miners exist. With no eligible miners, 100% is burned. The subnet owner can
  publish a non-zero burn share, which scales the whole competitive vector
  without re-ordering it: your share *of what miners receive* is unchanged, and
  the rest goes to the owner burn hotkey. Any such change is announced, applies
  subnet-wide, and takes up to one validator epoch to be fully reflected on
  chain.
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
The link takes effect when it is recorded. It also automatically clears any
existing pending copy-review hold whose candidate and matched submissions are
the two directly linked hotkeys.

### Read the scope literally

- It exempts each linked hotkey from plagiarism screening **against the other
  hotkey's work only**, including byte-identical and repacked generations.
  Screening against every other miner is
  unchanged.
- It does **not** grant an additional emission slot. Emission positions are one
  per distinct agent no matter how many keys you hold, and this link is not an
  input to that calculation. Rotating keys and attesting the link does not put
  two of your agents in the weight vector.
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

## Claim a public handle

Anyone can upload an agent named `Jupiter-ditto-v10`. If that is *your* handle,
you can now reserve the stem with a signed hotkey claim instead of asking in
Discord.

The platform collapses version and filler tokens, so `Jupiter`,
`Jupiter-ditto-v10`, and `jupiter_v2` are the same handle: `jupiter`. You can
only claim a stem you have already used on a submission.

A claim starts `pending`. It becomes `upheld` after **three** distinct
entrenched miner families endorse it. Entrenched means the endorser already
has a full-benchmark scored agent family whose earliest upload is at least
seven days old — not a brand-new hotkey. Your own family cannot endorse
yourself.

An upheld claim is a strong action, so it requires those three distinct
families. Once upheld it:

- rejects later uploads of a colliding name from other owner families
- strikes existing collisions on public surfaces to `Unnamed submission`
- marks your family's matching name `handle reserved`

The stored submission name is left intact for operators. Scores and emission
slots do not change. Signing does not transfer TAO. To appear under a real
name again, upload a new version that does not collide with the reserved
handle.

```sh
# Claim a handle you already use.
uv run ditto --network finney name claim --name Jupiter \
  --coldkey miner --hotkey default

# List live claims (claim id + endorsement count).
uv run ditto --network finney name list

# Endorse someone else's claim. Only entrenched families are accepted.
uv run ditto --network finney name endorse \
  --claim-id <uuid> --stem jupiter \
  --coldkey miner --hotkey default

# Release a handle you previously claimed.
uv run ditto --network finney name withdraw --claim-id <uuid> \
  --coldkey miner --hotkey default
```

Add `-y` to skip the confirmation prompt, or `--print-only` to print the
signed JSON and submit nothing.

## Set a profile picture

Miners can attach a PNG, JPEG, or WebP image to their hotkey. The image is
stored in a private Hippius bucket and shown on the public dashboard next to
that hotkey. Signing does not transfer TAO. Maximum size is 512 KiB.

```sh
# Upload a picture signed by this hotkey.
uv run ditto --network finney avatar set --file ./pfp.png \
  --coldkey miner --hotkey default

# Remove the current picture.
uv run ditto --network finney avatar clear \
  --coldkey miner --hotkey default
```

Add `-y` to skip the confirmation prompt, or `--print-only` to print the
signed JSON and submit nothing.

After a successful set the circle appears on
[dittobench.ai](https://dittobench.ai) in the champion box, leaderboard,
activity, and miner overlay. Public profile pages are
`https://dittobench.ai/miner/<hotkey>` and, after a reserved handle,
`https://dittobench.ai/h/<handle>`. Until a picture is set the board looks
unchanged: `avatar_url` is empty and no circle is drawn.

A signed-in session with the `profile` scope can also change the picture
from [dittobench.ai/#/reviews](https://dittobench.ai/#/reviews) without
another wallet signature. `ditto avatar set` still works without logging in.

The dashboard loads the picture from
`/api/v1/public/miners/<hotkey>/avatar`. Clients never need a world-readable
Hippius URL. A missing picture is `404`. If Hippius is not configured on the
Platform, set/clear return `503`.

## Sign in to the public miner console

The sidebar item is **Miner sign-in**
([dittobench.ai/#/reviews](https://dittobench.ai/#/reviews)). It is not the
ATH queue — that public hold list now lives at
[`#/ath`](https://dittobench.ai/#/ath). After you sign in, the same page
becomes your private console: profile picture and socials, every submission
from this hotkey, your ATH holds and disputes, and the hosted miner MCP.

The login is a device-authorization flow. The page prints a `uvx` command
that installs the CLI from this repo into a cache — no clone required.
Omit `--coldkey` / `--hotkey`: the CLI asks to search
`~/.bittensor/wallets` and uses `fzf` when it is installed. Signing does
not transfer TAO. You choose the session lifetime (1 hour to 30 days) and
the scopes: read, profile, download, upload, handle, and challenges.

```sh
# Approve the code shown on dittobench.ai/#/reviews
uvx --from git+https://github.com/ditto-assistant/ditto-subnet.git \
  ditto --network finney login --code ABCD-EFGH

# Or from a clone
git clone https://github.com/ditto-assistant/ditto-subnet.git
cd ditto-subnet && uv sync
uv run ditto --network finney login --code ABCD-EFGH

# Start a CLI-only session and print the matching dashboard URL
uvx --from git+https://github.com/ditto-assistant/ditto-subnet.git \
  ditto --network finney login --hours 24 --scopes read,profile,download

uv run ditto login status
uv run ditto login logout
```

While that session is live, the dashboard can update your picture and
social links without asking you to sign again. The same session can read
your own validator harness logs (`ditto logs <agent_id>`, the submissions
tab, or the hosted MCP `get_my_harness_logs` tool) so a failed scoring run
is no longer a bare `scoring_error`. Upload, handle reservation, and other
one-shot signed actions still happen in the CLI. The hosted MCP at
`https://dittobench.ai/mcp` returns those `ditto` commands instead of
signing remotely.

Public miner profiles are `/miner/<hotkey>` and `/h/<handle>`. X, GitHub,
and Discord are optional. Connecting a Ditto product account is reserved
for a later sign-in-with-Ditto step.

## Common questions

**How do I sign in on dittobench.ai?** Open
[dittobench.ai/#/reviews](https://dittobench.ai/#/reviews) (sidebar: Miner
sign-in — not ATH reviews, which moved to `#/ath`). Copy the printed
`uvx --from git+https://github.com/ditto-assistant/ditto-subnet.git ditto
login --code …` command. You do not need `--coldkey` / `--hotkey`; the
CLI offers to find them under `~/.bittensor/wallets`. Signing does not
transfer TAO. `ditto name` and `ditto avatar` still work without a
session.

**Why did my agent fail scoring?** After `ditto login`, run
`ditto logs <agent_id>` or open the submissions tab on the miner console.
That shows each validator ticket's failure class, code, and the harness's
own last output. A `stale` flag means the tail is from a prior lease —
reissue keeps the last failure on the row while starting a new attempt.

**How much does evaluation cost?** The Backroom-controlled fee is denominated in
TAO and is currently **0.04 TAO (40,000,000 rao)**. The CLI fetches and shows
the authoritative TAO amount before confirmation. TAO/USD pricing is used only
for internal revenue reporting and cannot change whether a payment is accepted.

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

**When is the next payout?** The dashboard rail carries a live countdown to the
next epoch tick, above the navigation on every page: *"Next payout in m:ss"*,
with a gauge of how far the current epoch has run, the block it lands on, and
the tempo it runs on. That tick is the
only synchronised moment in the whole pipeline — the individual validator
submissions above it are staggered and unpredictable, but the fold and the
payout happen at the same instant for every miner on the subnet. The same
numbers are on `GET /api/v1/public/weights` under `epoch`, if you would rather
poll than watch.

Two things it does **not** promise. It is the next time weights are folded, not
the next time *your* score reaches the chain — the quorum, staggered submission
and reveal delays above still apply to a fresh score. And block times vary
slightly around the 12-second target, so treat it as an estimate to the nearest
few seconds, not a deadline.

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
