# Ditto Subnet (SN118) — Miner FAQ & Pipeline Guide

**Draft, as of 2026-07-07. Not yet published — internal review copy.**

Everything a miner needs to know about how SN118 works end to end: what you
submit, how it flows through the pipeline, how it's scored, how emissions are
decided, and what will get you flagged or banned.

> **Where things run today:** the full pipeline is live on the **dev localnet**
> (netuid 3) with a single team validator. Testnet and mainnet (finney, SN118)
> migration are on the roadmap. Where a detail is dev-only or subject to change
> before launch, it's called out.

---

## 1. What is this subnet?

**SN118 is a best-artifact competition, not a live-inference subnet.** Miners
submit a **Rust agent-memory harness** — a crate that depends on the
[`ditto-harness`](https://github.com/ditto-assistant/ditto-harness) reference
library and overrides its extension traits. Validators build and run each
submission in an isolated sandbox, benchmark it on seeded tool-use and memory
tasks, and the best harness takes ~all emissions.

Two properties follow from "best artifact":

1. **You are paid for beating the state of the art**, not for uptime or serving
   queries. Scores jump when someone ships a genuinely better harness, then
   plateau.
2. **Your artifact is downloadable by others**, so copying is the central
   threat the incentive mechanism is designed against (see §7 and §8).

### The moving parts (four repos + chain)

| Component | Repo | What it does |
| --- | --- | --- |
| Miner CLI (`ditto`) | `ditto-subnet` | Bundles, pre-flights, pays for, and uploads your submission |
| Platform API | `ditto-platform` | Intake, on-chain payment verification, object storage, the screener/validator queues, the public signed score ledger, the anti-copy gate, leaderboard/dashboard |
| Screener worker | `ditto-subnet` | Cheap automated gate: does your tarball `docker build` and serve `/health`? Promotes `uploaded → evaluating` |
| Validator worker | `ditto-subnet` | Pulls the queue, scores via DittoBench, signs and submits scores, computes the weight vector, sets weights on chain |
| DittoBench | `dittobench-api` (Go) | The scoring engine: sandboxed `docker build`, seeded tool + memory cases, deterministic judge-free grading → `ScoreReport` |
| Reference harness | `ditto-harness` (Rust) | The library your crate builds against (pinned build dep) |
| Bittensor chain | — | Weights → Yuma consensus → emissions to the winner |

The pipeline in one line:

```
you (ditto upload) → platform API (payment verified, stored)
  → screener (build + serve gate) → validator (DittoBench score, signed)
  → public score ledger → deterministic KOTH weight fold → chain → emissions
```

The platform's OpenAPI schema is the contract between all of these; the
validator and screener are **stateless** (no DB) — all state you can query
lives behind the platform API.

---

## 2. What do I submit?

**One gzipped tarball containing a whole buildable crate**, with:

- **A `Dockerfile` at the tarball root** (`Dockerfile` or `./Dockerfile`). The
  screener and scorer build your submission with `docker build` using the
  tarball itself as the build context — no Dockerfile at root = automatic
  screening failure (`"no Dockerfile at tarball root"`).
- **An HTTP service that serves `GET /health`** with a 2xx once up (container
  port 8080 by default). The screener runs your image and polls `/health`;
  never healthy within the timeout = screening failure.
- A dependency on the pinned **`ditto-harness`** library with your trait
  overrides — that's the thing actually being benchmarked.

Constraints enforced today:

| Constraint | Value | Enforced where |
| --- | --- | --- |
| Max tarball size | **20 MiB** (`DITTO_MAX_TARBALL_SIZE_BYTES`) | Platform upload cap (rejected pre-payment at `/upload/check`); the screener enforces the same cap on download |
| Valid gzip + tar | must open cleanly | CLI pre-flight (`gzip_valid`, `tar_opens`) |
| Build memory | 2 GB (`--memory 2g`) | screener + scorer sandbox |
| Build timeout | 20 min (screener default) | screener |
| Serve/health timeout | 120 s | screener |
| Pids limit | 512 | screener container |

Three further checks are **deferred stubs** today (they print `DEFERRED` in
`ditto verify` and don't gate): a manifest check, a dependency/import
allowlist, and a schema diff against the reference harness. Expect these to
become real gates once the harness interface is frozen — don't rely on their
absence.

Run `ditto verify --path agent.tar.gz` any time — it's purely local (no chain,
no API, no payment) and prints a per-check table with a final PASS/FAIL.

---

## 3. How do I upload? (the miner CLI)

Installed as the `ditto` console script. Global flags:

- `--network {finney|test|local}` (env `DITTO_NETWORK`, default `finney`) —
  picks a **locked pair** of API URL + subtensor network so they can't desync:
  `finney → https://api.ditto.subnet.ai`, `test → https://staging.api.ditto.subnet.ai`,
  `local → http://localhost:8000`. *(The finney/test API hostnames are
  placeholders until launch.)*
- `--chain-endpoint ws://…` (env `DITTO_SUBTENSOR_CHAIN_ENDPOINT`) — overrides
  only the chain target, keeping the `--network` API URL (used today to point
  at the hosted dev chain).
- `-v/--verbose` for debug logs.

### `ditto upload`

```sh
ditto --network <net> upload --path agent.tar.gz --name my-harness \
  --coldkey <wallet.name> --hotkey <wallet.hotkey> [-y]
```

What happens, in order:

1. **Local pre-flight** (same checks as `ditto verify`); aborts before any
   money moves if the tarball is structurally broken.
2. **Signature**: the CLI signs `"{hotkey_ss58}:{sha256}"` with your **hotkey**
   (sr25519) — this binds the upload to your hotkey and to the exact bytes.
3. **`POST /upload/check`** — the server pre-validates (registered hotkey,
   size, ban status, …) *before* you pay. Rejections come back as numeric
   `error_codes` + messages (e.g. `1101` = hotkey not registered on the
   subnet, `1103` = hotkey banned).
4. **`GET /upload/eval-pricing`** — returns the eval fee (`amount_rao`) and
   the Ditto-controlled SS58 receive address.
5. **Payment confirmation prompt** (`[y/N]`; skip with `-y`), then a
   `Balances.transfer_keep_alive` extrinsic **signed by your coldkey**, waited
   to finalization. Note: the fee comes from the **coldkey** balance; the
   hotkey only signs the upload.
6. **`POST /upload/agent`** — multipart upload of the tarball plus the payment
   proof (`block_hash`, `block_number`, `extrinsic_index`). The platform
   independently re-verifies the extrinsic **on chain** before accepting.
7. On success you get an **`agent_id`** (UUID) — your handle for status
   polling.

**If the upload fails after payment finalized**, the CLI prints your payment
proof and tells you to keep it for support — the fee is on chain either way.
Exit codes: `0` success, `1` any error, `2` you declined the payment prompt.

### Replay protection (why you can't reuse a payment)

The proof `(block_hash, extrinsic_index)` is the primary key of the platform's
`evaluation_payments` table. Each on-chain payment authorizes exactly one
upload; a consumed proof, a wrong-network extrinsic, or a sha256 that doesn't
match your signed value are all rejected.

### `ditto status`

```sh
ditto status <agent_id>                     # by id
ditto status --coldkey <ck> --hotkey <hk>   # latest agent for your hotkey
ditto status <agent_id> --json
```

Exit `3` means not found (404).

---

## 4. What happens to my agent after upload? (the lifecycle)

The canonical `AgentStatus` state machine:

```
uploaded → screening → screening_passed → evaluating → scored → live
                     ↘ screening_failed
(any point) → ath_pending_review   (plagiarism hold, human-reviewed)
(hotkey-level) → banned
```

| Status | Meaning |
| --- | --- |
| `uploaded` | Payment verified, tarball stored. Waiting for the screener. |
| `screening` / `screening_passed` / `screening_failed` | The build-gate result (§5). Failed = you're out; fix and resubmit (new fee). |
| `evaluating` | In the validator queue awaiting a DittoBench run. |
| `scored` | A signed score is in the public ledger. **You keep this status and your ledger entry durably** — weights are recomputed from the ledger every epoch, so a scored agent keeps earning without re-evaluation. |
| `live` | Reserved lifecycle state. |
| `ath_pending_review` | A cross-miner near-duplicate hold — a human reviews before you can take the crown (§8). Never auto-banned. |
| `banned` | Hotkey-level ban; upload rejected with code `1103` pre-payment. |

*(The screener is live on the dev validator host — `uploaded → evaluating`
promotion is fully automatic.)*

---

## 5. The screener: what's the cheap gate?

`python -m ditto.screener` polls the platform for `uploaded` agents (oldest
first, one at a time) and runs a **build + serve** gate — not a lint-only
check:

1. Downloads your tarball via a presigned URL and **re-verifies the sha256**
   against what you uploaded.
2. Checks `Dockerfile` at tarball root.
3. `docker build` with the tarball piped in as the build context (BuildKit;
   a `gh_token` build secret is provided so your crate can pull the private
   `ditto-harness` dep).
4. Runs the image (`--memory 2g --pids-limit 512`, port published on
   localhost) and polls `GET /health` every second until 2xx.
5. Posts a **signed verdict** (`{screener_hotkey}:{agent_id}:{passed}`,
   sr25519) to the platform, which flips your agent to `evaluating` or
   `screening_failed`.

Pass = **builds AND serves**. No LLM calls, no scoring — it exists so a broken
tarball never wastes an expensive DittoBench run. Failure details (build log
tail, "serve check failed", …) are logged server-side.

---

## 6. Scoring: how is my harness evaluated?

Each validator sweep (hourly by default), the validator:

1. Leases a scoring ticket (`POST /validator/job`: seed, dataset_sha256, run_size, deadline; at most 3 validators per agent).
2. Fetches a short-lived presigned tarball URL and **cross-checks the sha256**
   (queue vs. artifact vs. what the scorer fetches) — a mismatch refuses to
   score.
3. Submits to its co-located **DittoBench** engine with the tarball URL and
   `run_size=full` (no key: scoring is judge-free), then polls until done
   (timeout 40 min).
4. DittoBench does the real work: sandboxed `docker build` of your crate,
   **seeded** synthetic data generation, tool-use cases + memory cases run
   against your harness under the locked model, deterministic grading —
   producing a `ScoreReport`.
5. The validator **signs** the score and posts it to the platform ledger.

### The score

```
composite = 0.5 * tool_mean + 0.5 * memory_mean        # both in [0, 1]
```

- **Tool case:** deterministic trajectory + argument accuracy (0.4 name-F1 +
  0.4 arg-F1 + 0.2 order/extra-call discipline), scored on the trajectory the
  validator observed execute.
- **Memory case:** deterministic per-`answer_kind` grading (value, number,
  list, ordered list, duration, reversal, decline) with distractor and
  forbidden-value zeroing. No LLM judge anywhere.
- The report also carries `median_ms` (latency), `n` (cases), and the
  **`seed`** used for data generation.

**Why the seed matters to you:** every ledger entry records the dataset seed,
so any score is reproducible and challengeable — you don't have to trust the
scorer's word, you can re-run the exact benchmark. Seeded generation also
means you can't overfit to a fixed public test set.

**Cost caps:** your harness's own LLM calls run under per-case and per-run
budgets. **A harness that loops or spews unbounded LLM calls fails its
run** rather than burning the validator's budget — keep your harness's token
use disciplined.

One agent failing to score never stalls the sweep — it's logged, skipped, and
retried; other miners are unaffected.

---

## 7. Emissions: who gets paid, and how much?

The mechanism is **KOTH (king-of-the-hill) winner-take-most with an ATH gate**
— chosen specifically because it's the only shape that's structurally
copy-resistant for a downloadable artifact (see `docs/incentive-mechanism.md`
for the full option analysis).

The deterministic weight fold, exactly as implemented:

1. Take the public ledger (one entry per miner: their **highest-scoring**
   eligible agent). Drop non-positive composites.
2. Walk entries in **first-seen order** (upload time, then agent_id). A
   challenger dethrones the current champion **only if**
   `challenger_composite > champion_composite × 1.05` (a **5% relative
   margin**, `VALIDATOR_KOTH_MARGIN`).
3. **Ties and sub-margin improvements keep the incumbent.** First to submit
   wins — an exact copy at best *ties* the champion and therefore never earns
   the crown.
4. The champion gets **90%** of the weight (`VALIDATOR_KOTH_CHAMPION_SHARE =
   0.9`). The **participation tail** — the next **4** distinct miners by
   composite (`VALIDATOR_KOTH_TAIL_SIZE`) — split the remaining **10%
   equally** (2.5% each).
5. The vector is normalized on chain; only ratios matter.

Practical consequences for miners:

- **A 4% improvement earns nothing; a 6% improvement earns everything.**
  Ship real improvements, not epsilon tweaks.
- **Being early matters.** `first_seen` (your upload timestamp, immutable) is
  the tie-break. If two miners land equivalent scores, the earlier upload
  holds the crown.
- **You keep earning after one epoch.** Weights are recomputed from the
  durable ledger every epoch, not from the live queue — a scored champion
  keeps its emission until actually dethroned.
- **The tail keeps you in the game** — top-5-ish miners all earn something,
  but 90% concentration means #1 is the only position worth optimizing for.
- These parameters (5%, 90/10, tail 4) are validated against real score
  distributions before launch and may be tuned — the *shape* (KOTH + ATH +
  first-seen) is locked.

### Why every validator computes the same weights (and why you can trust it)

The weight function is a **pure, deterministic, open-source fold** in this
repo (`ditto/validator/weights.py`) — no clock, no randomness, no I/O. Every
validator pulls the same public ledger and computes the identical vector;
Bittensor's **Yuma consensus** clips any validator that deviates. You trust
the signatures and the function, not any single operator or the (closed)
platform API. The platform **never** computes champions or weights — that
logic lives only validator-side, by design.

*(Today there is a single team validator on the dev chain. The target design
is k=3: each submission independently scored by 3 validators, finalized as the
**median of 3** — robust to one liar or outlier — with all ~10 validators
folding weights from the ledger.)*

---

## 8. Anti-copy: what happens if someone (or I) resubmit a near-duplicate?

Copying is treated as the existential risk, and there are three stacked
defenses:

1. **The mechanism itself** (§7): a verbatim copy ties, never beats the 5%
   margin, and loses the first-seen tie-break — it earns nothing even if
   undetected.
2. **Exact + heuristic checks at upload:** cross-miner exact sha256 match and
   size/score heuristics.
3. **Two-channel content fingerprinting** (live since 2026-07-05), both
   MinHash sketches compared by Jaccard similarity + containment:
   - **Lexical channel** (platform, at upload): per-file line shingles with
     intra-line whitespace stripped. Survives re-indenting, reformatting,
     file renames, and junk-file padding (containment catches a copy hiding
     inside a bigger tarball).
   - **Structural/AST channel** (DittoBench, at score time): a sketch of the
     Rust **parse-tree shape** with identifiers and literals discarded —
     additionally survives **identifier renaming**. Travels on the score
     report as unsigned advisory metadata.

A flagged cross-miner near-duplicate is held in **`ath_pending_review`** for
**human review — never auto-banned**. Confirmed plagiarism can lead to a
hotkey-level ban (`banned_hotkeys`), which blocks future uploads before
payment.

What this means for honest miners:

- **Forking `ditto-harness` and building on the reference is the point** —
  that's not plagiarism, that's the game.
- Renaming variables / reformatting / padding the current champion **will be
  caught** and won't pay even if it weren't.
- Thresholds are currently conservative, so false holds are possible; a hold
  is reviewed by a human, and legitimate independent work gets cleared.
- Deep semantic rewrites are acknowledged as out of scope for the fingerprint
  — but they still have to *beat the champion by >5%* to earn anything.

---

## 9. Transparency: what can I see and verify?

**The score ledger is public and self-verifying.** `GET /api/v1/scoring/scores`
returns, per miner: `miner_hotkey`, `agent_id`, `composite`, `first_seen`,
`sha256`, `size_bytes`, `run_id`, `seed`, `validator_hotkey`, `signature` —
ordered exactly the way the weight fold consumes it.

**Verifying a score signature yourself:** rebuild the message

```
"{validator_hotkey}:{agent_id}:{run_id}:{composite!r}:{seed}"
```

(`composite!r` = Python's shortest round-trip float repr) and sr25519-verify
the hex `signature` against the validator's SS58 hotkey. Because the signature
binds the agent id, composite, and seed, the platform cannot fabricate or
alter a score, and a captured signature can't be replayed onto another agent.
The same pattern covers screener verdicts (`{hotkey}:{agent_id}:{passed}`) and
your own upload (`{hotkey}:{sha256}`).

**Public dashboard + leaderboard:** `GET /api/v1/public/leaderboard` and
`/public/health` (no auth, aggregate-only), with a dashboard served at the
platform root (dev: `https://platform-api-dev.heyditto.ai/`).

**W&B telemetry** (`heyditto/ditto-sn118`): the validator publishes
aggregate-only sweep stats — per-agent composite, tool/memory means,
per-category means, leaderboard, and the weight vector with champion/tail
roles. It deliberately **never** publishes the per-case answer key
(expected/called tool lists), your tarball contents, or any secret; agents
appear under an opaque `run_id` handle rather than the real `agent_id`.

---

## 10. Quick FAQ

**Q: What do I need before my first upload?**
A Bittensor **coldkey with funds** (eval fee + registration burn) and a
**hotkey registered on the subnet** (burned registration). The upload fee is
paid by the coldkey; the hotkey signs the submission and receives incentive.

**Q: How much is the eval fee?**
Dynamic — the CLI fetches it live from `GET /upload/eval-pricing` and shows
you the exact TAO amount before asking for confirmation. It exists to make
spam submissions costly; every upload pays it, including resubmissions.

**Q: How long until I'm scored?**
Screening is minutes (a docker build + health check). The validator sweeps
hourly by default; a full DittoBench run can take tens of minutes (40-minute
scoring timeout). So: same-day, typically within a couple of hours once the
pipeline is unattended.

**Q: My upload failed after I paid. Did I lose the fee?**
The CLI prints your payment proof (`block_hash`, `block_number`,
`extrinsic_index`) — keep it and contact the team. The proof is single-use
and tied to your signed sha256, so nobody else can consume it.

**Q: Can I test without paying?**
`ditto verify --path …` runs every local check free. Beyond that, run the
screener's exact gate yourself: `docker build` with your tarball as context,
run the image, curl `/health`. If those pass, screening will pass.

**Q: Do I need my own OpenRouter/LLM key?**
Not for scoring: grading is deterministic (no judge) and on-chain runs score
your harness against the locked open-weight model served by the validator's
gateway, so no key exists anywhere in a scored run. You only need a key (or
local Ollama) for your own local practice.

**Q: What model is used and can I see per-case results?**
Every harness runs against the locked open-weight model (Qwen3-32B,
`Qwen/Qwen3-32B-TEE` on the fleet-standard Chutes gateway). Per-case details
(`case_id`, category, scores, latency, notes) exist on the `ScoreReport`, but
only aggregates are published. The rubric that matters:
**0.5 × tool + 0.5 × memory**.

**Q: Someone copied my harness — what protects me?**
Your **first-seen timestamp** (immutable upload time), the 5% dethrone margin
(a copy only ties you), and the two-channel fingerprint that catches
renamed/reformatted/padded copies and holds them for human review. §7–8.

**Q: Can I submit multiple agents?**
Yes — each upload pays its own fee, and the ledger keeps one entry per miner:
your **highest-scoring** agent represents you in the weight fold.

**Q: What gets me banned?**
Hotkey-level bans are owner-issued (e.g. confirmed plagiarism after human
review). Banned hotkeys are rejected at `/upload/check` (code `1103`) before
any payment.

**Q: Is commit-reveal on?**
Off on the dev chain (weights apply directly). **Production will re-enable
commit-reveal**, which changes nothing for miners except weight visibility
timing.

**Q: Where do emissions actually come from?**
Standard Bittensor: validators set weights → Yuma consensus → the subnet's
per-block emission is split per the consensus weights, accruing as alpha to
your hotkey. This is proven live on the dev chain (champion incentive = 1.0,
alpha accruing).

---

## 11. Current status & known caveats (2026-07-07)

- **Live today (dev localnet, netuid 3):** the full pipeline runs unattended,
  non-mock, end to end — miner upload → screener (auto build-gate) → validator
  → real DittoBench scoring → signed ledger → KOTH weights → on-chain
  emissions. A real agent has produced a signed composite (0.522 at
  `run_size=small`) that drove an accepted on-chain `set_weights`; the
  full-size (`run_size=full`) proof is in flight.
- **Single validator** (the subnet owner's UID). k=3 median-of-3
  multi-validator scoring is the next architectural step; the current
  `/validator/job` / `/agent/{id}/artifact` / `/agent/{id}/score` endpoints
  are Phase-1 names that will migrate to lease-based `request-evaluation` /
  `submit-score`.
- **Sandbox egress allowlist not yet in place** (cost caps are); deferred tar
  checks (manifest / dependency allowlist / schema diff) will become real
  gates once the harness interface freezes.
- **Fingerprint thresholds are conservative placeholders** pending tuning
  against a real submission corpus; holds are human-reviewed.
- **Networks:** `finney`/`test` API URLs in the CLI are placeholders until the
  production hosts are provisioned; everything currently runs against the dev
  chain (`--network local --chain-endpoint ws://…`).

### Sources (this repo unless noted)

`README.md` · `PROJECT.md` (locked decisions D1–D4) ·
`docs/incentive-mechanism.md` · `docs/STATE-OF-THE-SUBNET.md` ·
`docs/PRODUCTION-HANDOFF.md` · `docs/NEXT-STEPS.md` ·
`docs/dev-e2e-handoff.md` · `ditto/miner_cli/` · `ditto/screener/` ·
`ditto/validator/{worker,dittobench,signing,weights}.py` ·
`ditto/api_models/{upload,validator,screener,agent_status}.py`
