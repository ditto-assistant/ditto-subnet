# Public telemetry: wandb + dashboard + public API

Status: **approach decided 2026-07-04** (Nick). This doc is the contract for what
SN118 publishes publicly and how. Implementation tracked per section below.

## Decisions

1. **wandb transparency = aggregate + per-category.** Publish per-agent
   composite, tool/memory means, and **per-category** means, plus
   leaderboard / weights / health. **Do not** publish raw per-case
   `expected`/`called` tools or the haystack contents — that is the benchmark's
   answer key and would let miners fit to specific cases.
2. **Dashboard = both.** A custom public "front door" (leaderboard + health) for
   humans, backed by a public read API + Prometheus; wandb linked alongside for
   researchers who want full per-epoch telemetry.
3. **Public read API = yes.** Add a rate-limited, no-auth read endpoint so the
   dashboard (and anyone) can read the leaderboard without a validator hotkey.
4. **The king's source becomes public after on-chain confirmation.** Source
   release is **king-only** and gated on on-chain weights: a submission's source
   becomes downloadable only once it has (1) held the KOTH crown and (2) had
   validators' revealed on-chain weights set on it after commit-reveal. The
   configured embargo (48 hours by default, operator-tunable anywhere from 6
   hours to 720 hours / 30 days) is measured from that on-chain confirmation —
   **not** from the third accepted validator score. A submission that never
   reigned is never released, however it scored. One that held the crown but is
   not yet chain-confirmed is withheld with no unlock time until commit-reveal
   confirms validators backed it. A three-validator score quorum on a single
   benchmark version remains a precondition — scores from different benchmark
   versions never combine into a quorum — but it starts no clock, and neither
   an operator clear nor a later re-score moves the embargo, which is anchored
   to the on-chain confirmation alone. Quarantined, held, and rejected
   submissions stay private regardless of rank.

## Anti-gaming posture (the load-bearing rule)

The **aggregate leaderboard** stays aggregate (best-per-payment-coldkey, no
per-run seed).
The **per-submission k=3 record** (`/submissions`, `/agent/{id}/scores`, added
2026-07-09) goes further for trust: it publishes which validators scored an
agent, each one's exact numbers plus signature, the finalized median, and the
raw dataset seed. The raw seed is safe to publish here because it is derived from an **on-chain
block** at job-ready (see `onchain_seed.py`), which is causally after the miner
committed their submission, so they could not have anticipated it, and it rotates
per submission, so a past seed can never help pre-overfit a future run. The
per-submission record also publishes `dataset_seed_block` + `dataset_seed_block_hash`
so anyone can recompute `derive_seed(block_hash, agent_id)` and confirm the seed
was **not platform-chosen** (removing the last platform-trust assumption; a null
block flags the rare CSPRNG fallback used when the chain was unavailable).
The one line that never moves: **per-case rows stay private** (`expected` /
`called` / `case_id` are the answer key). If we ever want research-grade
per-case release, do it on a delay (after that dataset generation is retired),
never live.

## Surface 1 — wandb (validator → public project)

The validator logs one wandb run per validator hotkey; each epoch/sweep appends.
New module `ditto/validator/telemetry.py` in ditto-subnet, called from the worker
after each sweep + weight set. Config (env): `WANDB_PROJECT`, `WANDB_ENTITY`,
`WANDB_API_KEY`, `WANDB_MODE` (`online`|`disabled`, default `disabled` so it is
opt-in). No secrets or keys are ever logged.

**Time-series scalars** (per epoch): `sweep_duration_s`, `queue_depth`,
`runs_started`, `runs_failed`, `runs_failed_frac`, `champion_composite`,
`positive_miner_count`, `openrouter_cost_delta`, `set_weights_latency_s`,
`set_weights_ok` (0/1), `chain_block`, and per-stage dittobench durations
(`build_s`, `seed_s`, `run_s`, `judge_s`).

**Tables** (snapshot per epoch):
- `scores` — one row per agent scored this sweep: `uid`/`miner` (short),
  `agent_id[:8]`, `composite`, `tool_mean`, `memory_mean`, one column per
  category mean (`cat.link_read`, `cat.web_search`, `cat.memory_lookup`,
  `cat.single-session-user`, `cat.temporal-reasoning`, `cat.multi-session`, …),
  `n`, `median_ms`, `seed`, `run_id`.
- `leaderboard` — best-per-payment-coldkey ledger (legacy hotkey fallback):
  `rank`, selected-generation `miner`, `composite`,
  `is_champion`, `ath` (all-time-high composite for that miner).
- `weights` — `miner`, `uid`, `weight` (normalized), `role` (champion|tail),
  plus scalars `koth_margin`, `champion_share`.
- `integrity` — counters: `held_for_copy_review`, `dedup_rejected`,
  `banned_hotkey_rejected`, `seed_rotations`.

## Surface 2 — public read API (platform)

New router `endpoints/public.py`, mounted at `/api/v1/public`, **no auth**,
rate-limited, `Cache-Control: public, max-age=30`. Read-only, aggregate-only.

- `GET /api/v1/public/leaderboard` → `{ generated_at, count,
  active_bench_version, desired_bench_version, selection_mode, entries: [
  { rank, agent_id, agent_name, miner_hotkey, registered, emission_eligible,
    composite, official_composite, effective_composite, efficiency_bonus,
    aggregate_method, finalized, score_count, tool_mean, memory_mean,
    first_seen, n,
    median_ms, bench_version, dataset_sha256, models, per_category,
    submission_family: { member_count, selection_rule, members: [
      { agent_id, agent_name, agent_version, miner_hotkey,
        canonical_composite, submitted_at, representative } ] },
    integrity, tokens } ], emissions: { champion_agent_id, recipients,
    raw_leader_decision, margin, dethrone_z, champion_share, rank_shares,
    tail_size } }`.
  Entries are best-per-payment-coldkey and ranked by **`official_composite`,
  not `composite`**. Sorting the board yourself by `composite` reproduces `rank`
  only while every entry is still on the canonical median
  (`aggregate_method == 'canonical_median'`); once any agent has completed
  continual cohort waves the two orderings diverge, and `official_composite` is
  the one that ranks the board and drives the weight fold. Ties break on
  `first_seen`, then `agent_id`. Provisional (pre-quorum) rows are ranked among
  themselves and always trail the finalized board.
  Different names and hotkeys under one coldkey compete for one position; the
  best eligible generation wins and its hotkey remains the weight destination.
  `submission_family` keeps the other finalized, full-benchmark generations
  visible without exposing the coldkey itself or assigning them independent
  ranks. The same family projection appears on `/agent/{id}/pipeline`, so a
  direct link to a non-representative score explains which submission represents
  it on the board.
  During a
  benchmark rollout the default response is the exact authoritative hybrid pool
  validators fold: v3 for an agent at 3/3, otherwise that agent's active-version
  fallback. `?bench_version=2` provides a historical single-version view and
  intentionally returns `emissions: null`. In the default view, `emissions` is a
  public-safe, read-only projection of the validator's frozen first-seen KOTH
  fold over finalized authoritative entries: the fixed 0.007 composite-point
  incumbent hysteresis, the
  statistical band, and the 65% / 14% / 10% / 7% / 4% ranked distribution. It is
  `null` when no eligible entry exists. Validators still compute and submit their
  own authoritative weight vectors. The provenance block (`models` =
  generator/judge/judge_audit/harness, `bench_version`, `dataset_sha256`,
  `per_category` means, `median_ms`, `n`) is the **transparency payload**: it
  lets anyone see *what model produced a run and how it was scored* and pins the
  exact scored artifact (`dataset_sha256`) for a dispute re-score. All of it is
  advisory (not signed) and lifted from the safe subset of `scores.details` —
  extracted defensively so a malformed blob can never break the endpoint.
  `integrity` (paraphrase applied/attempted/fallback, NoLiMa lexical-gap
  rewrites + overlap before→after, capped tool cases, seeding waves) and
  `tokens` (LLM spend to generate+judge) publish the benchmark's **anti-overfit
  posture** so the community can audit *how gaming is resisted*, not just the
  scores.
  `registered` is a live chain decoration, not platform ownership: `false`
  preserves the immutable submission and score while excluding that hotkey from
  active weights and emissions; `null` means the chain snapshot was unavailable.
  Each entry also carries `artifact_release`, the submission-specific source
  state described below. Source visibility depends **entirely** on KOTH rank:
  only an agent that has worn the crown and been confirmed on-chain is ever
  released.
  The optional chain lookup has a short deadline and a bounded in-process
  snapshot cache, so Pylon latency or failure cannot fail the public
  leaderboard. A failed refresh keeps serving the last successful mapping and
  sets the response-level `registration_stale` flag rather than blanking the
  column: the values stay real, just not re-confirmed as of that response. Only
  when there is no recent good read at all does `registered` go `null`.
  The dashboard presents `null` explicitly as unknown and requires
  the KOTH projection before showing champion or recipient treatment.
  **Never** included: `seed` (anti-overfit), `per_case` `expected`/`called` (the
  answer key), sha256/signature/validator_hotkey (integrity-internal). The full
  submitted vector stays validator-side; only the explainable projection is public.
- `GET /api/v1/public/activity?limit=&downloadable=true` → `{ generated_at, count, downloadable_count, entries: [
  { agent_id, miner_hotkey, name, status, submitted_at, screening_reason,
    duplicate_of, review_reason } ] }`.
  Recent uploads, newest first, including screening and evaluation stages so a
  miner can confirm progress before a score exists. Screening failures expose a
  stable failure category; anti-copy holds expose the matched agent and signal
  summary. Internal review and ban states are collapsed to `under_review` /
  `rejected`. `artifact_release` exposes only the derived king/on-chain/embargo
  release state;
  signed download URLs, source hashes, payments, and raw screener/build logs are
  never included in this listing.
  `downloadable=true` narrows the listing to submissions whose derived release
  state currently permits a public source download; it composes with status and
  search filters. `downloadable_count` reports that population after search but
  before status filtering so the dashboard can label the quick filter.
  Each waiting entry also carries `validator_queue_rank` and
  `validator_queue_gate` — the queue preview, described next.
- **Queue preview (`validator_queue_rank` + `validator_queue_gate`).** Both the
  preview and the allocator that issues tickets read one ordering,
  `ditto.db.queries.queue_order.queue_order_terms`: the allocator composes it
  with `LIMIT 1` and a row lock, the preview composes the identical SQL with no
  limit. There is no second implementation to keep in sync, which is the whole
  reason the module exists — the preview used to restate the order in Python
  and drifted from the fleet three separate times.
  It is an **ordering, not a prediction of the next assignment**, and the
  difference is structural rather than a shortcoming to be fixed later. Three
  of the allocator's rules have no fleet-wide truth value:
  - **one score per validator** — a validator that already scored a submission
    is excluded from it, so every validator sees a different queue;
  - **per-validator retry cooldowns and attempt budgets** — a row a validator
    failed on is invisible to *that* validator until its cooldown lapses;
  - **coverage tiebreak and artifact mode** — whether a validator has held a
    submission before, and whether it prefers screened images, are properties
    of the validator, not of the queue.
  A global list cannot carry any of them, so rank 1 means "first in the shared
  order", not "scored next". `validator_queue_gate` is the honest half the
  preview *can* answer: `previous_generation` (retired-era work the fleet
  serves only once the current era drains), `owner_serialized` (another
  submission from the same paid owner is using that owner's validator slot, so
  this row waits while any other owner has eligible work — rotating hotkeys does
  not buy a second slot), or `not_leasable` (excluded by the allocator's
  candidate filter, or every quorum slot already occupied). A non-null gate
  means the row is not next in line regardless of rank, and consumers must not
  present it as imminent.

  `owner_serialized` is the one gate that is not an absolute refusal. A
  validator that walks its entire candidate set and finds nothing else eligible
  may lease such a row rather than idle its slot, bounded by the operator's
  per-owner concurrent-submission limit. Whether that happens depends on the
  polling validator's own cooldowns and prior tickets, which is exactly the
  per-validator half no global preview can model — so the preview reports the
  ordinary answer and this note is the caveat.
- `GET /api/v1/public/agent/{agent_id}/pipeline` → versioned screening history,
  validator assignment progress, and `provisional_scores` as soon as the
  platform accepts them. Each score exposes only `composite`, the post-commit
  exact decimal `seed` (encoded as a string to preserve 64-bit browser precision),
  run size, benchmark/generator version, dataset hash, acceptance time,
  seed provenance, and version-pinned reproduction / hash-verification commands.
  It does **not** associate a provisional number with a validator identity and
  never returns signatures, leases, run ids, secrets, or scorer internals.
  Finalized top-five agents may also carry `confirmation_scores`: completed,
  append-only shared-seed retest results with composite, exact decimal seed,
  public validator hotkey, benchmark version, and acceptance time. These rows
  are explicitly separate from `provisional_scores` and never change the fixed
  three-score canonical median. Each validation attempt has an additive
  `purpose` (`canonical_quorum`, `continual_retest`, or the bounded rolling-
  deployment state `legacy_unclassified`) so the operations UI can report extra
  maintenance work without presenting it as a fourth quorum slot or guessing
  about an old-writer lease. Unclassified in-flight leases drain and are
  reissued with an explicit purpose; neither score endpoint consumes them.
  A bounded migration allowance lets leases from old replicas finish during
  rolling overlap; the authenticated new score endpoint classifies such a lease
  as it consumes it. Purpose-aware writers explicitly disable the allowance and
  bind a positive revision. A database-side guard converts old-writer reissues
  to unclassified, while revision-zero leases without the transition allowance
  cannot be scored.
  `score_count` and `final_composite` are counted against `score_bench_version`
  — the benchmark era the submission belongs to, which is `active_bench_version`
  for current-generation work and an older version for a submission whose
  generation has closed. Consumers must read the two version fields together: a
  count scoped to the active era alone answered 0 for every previous-generation
  row, which is indistinguishable from a submission no validator ever took up.
  Scores from different eras are never mixed into one count.
  `final_composite` remains null until three independent scores reach quorum and
  the submission is in a finalized public state; the leaderboard and emission
  eligibility rules are unchanged. On-chain provenance is claimed only when the
  block fields exist; otherwise the response labels the unpredictable CSPRNG
  fallback. Unknown historical generator pins fail closed with no command rather
  than silently using `latest`.
- `GET /api/v1/public/bench/transcript/{sha256}/telemetry` → an allowlisted
  metrics projection from the immutable transcript whose digest is already
  bound into an accepted validator score. The platform reads only the
  content-addressed transcript namespace and verifies the stored bytes against
  the requested SHA-256 before parsing them. It returns execution totals,
  latency percentiles, retry/timeout/cancellation counts, relay health, and
  per-question attempt timing. It never returns question text, prompts, model
  responses, tool payloads, raw errors, credentials, or host paths. Older
  transcripts without execution telemetry show a clear unavailable state rather
  than fabricated metrics.
- `GET /api/v1/public/submissions?limit=` → `{ generated_at, count, quorum,
  submissions: [ { agent_id, miner_hotkey, status, score_count,
  median_composite, dataset_seed, dataset_sha256, last_scored_at,
  artifact_release } ] }`.
  The index over the **k=3 transparency records**, most recently scored first.
  Only settled public scores (`scored` / `live`) appear; held-for-review and
  still-evaluating agents are excluded so a provisional or accused agent is never
  surfaced.
- `GET /api/v1/public/agent/{agent_id}/scores` → `{ agent_id, miner_hotkey,
  status, quorum, score_count, median_composite, dataset_seed, dataset_sha256,
  dataset_run_size, artifact_release, scores: [ { validator_hotkey, composite, tool_mean,
  memory_mean, median_ms, n, seed, run_id, signature, generated_at } ] }`.
  The full k=3 breakdown for one finalized agent: *which* validators scored it,
  each one's exact numbers + sr25519 signature (self-verifying against the
  published validator key), the median the platform finalized on, and the pinned
  dataset (seed + sha256) so anyone can reproduce and audit the number. 404 for
  an unknown or not-yet-public agent. This is the one surface that intentionally
  exposes `validator_hotkey` + raw `seed` (see the anti-gaming posture above); it
  still omits `per_case`.
- `GET /api/v1/public/agent/{agent_id}/artifact` → `{ agent_id, bench_version,
  sha256, finalized_at, download_url, expires_at }`.
  Returns a five-minute private-bucket URL only when the agent **has held the
  KOTH crown**, its on-chain weights have been confirmed after commit-reveal,
  the configured embargo has elapsed since that confirmation, one benchmark
  version has three independently inserted score rows, and the agent is
  currently in a cleared finalized state (`scored` or legacy `live`). Before the
  deadline it fails with 425. An agent that has **never been king fails closed
  with 404** — `"only the king's source is released"` — as do unknown,
  held-for-review, quarantined, and rejected agents; an ever-king agent still
  awaiting on-chain confirmation is withheld with no unlock time. The response
  is `private, no-store`. A fourth score, re-score, benchmark rollover,
  leaderboard change, or deregistration never resets the original clock.
- `GET /api/v1/public/agent/{agent_id}/dataset` → `{ agent_id, miner_hotkey,
  seed, run_size, dataset_sha256, bench_version, dataset_seed_block(+hash),
  artifact }`.
  The **finalized-dataset reveal** (task A): the FULL labeled DatasetArtifact
  (answer keys included) a finalized submission was scored against, regenerated
  from its published on-chain-derived seed, so anyone can **independently
  re-grade** its k=3 scores. The regenerated artifact's SHA-256 is re-verified
  against the hash pinned at scoring (502 on drift), so the revealed bytes
  provably are the scored dataset. Gated to finalized (scored/live) agents (404
  otherwise — a provisional agent's answers are never revealed); 503 when the
  generate service is unavailable. Safe despite the answer key: the seed is
  one-time and was unpredictable at submission (see the anti-gaming posture), so a
  past dataset's answers cannot help overfit a future run.
- `GET /api/v1/public/bench/{version}/corpus?limit=&offset=` → `{ bench_version,
  generated_at, count, total, limit, offset, entries: [ { agent_id, miner_hotkey,
  validator_hotkey, seed, run_id, composite, per_case } ] }`.
  The **retired-version corpus release** (task B): the FULL UNREDACTED per-case
  answer keys (from stored `scores.details`) for a benchmark version that has been
  superseded. Refused with 409 for the current (live) version or any unknown
  future version, so a live answer key is never exposed here. Once a version
  retires it is never scored again, so releasing its complete labeled corpus has
  zero anti-overfit cost and lets researchers study the benchmark in full.
- `GET /api/v1/public/audit?since_seq=&limit=` → `{ generated_at, count,
  genesis_hash, head_hash, entries: [ { seq, agent_id, validator_hotkey, event,
  payload, prev_hash, entry_hash, recorded_at } ] }`.
  The **append-only, hash-chained audit log** (task #51): every scoring event in
  order — each validator's signed `score` and each `agent_finalized` (quorum
  reached, the median + scoring validators). `entry_hash` is SHA-256 over the
  entry's canonical content (which embeds `prev_hash`); `prev_hash` links to the
  previous `entry_hash`, rooted at `genesis_hash` (64 zeros). A consumer replays
  from `since_seq=0`, re-requests with the last `seq` seen, and recomputes each
  hash to prove nothing was reordered, edited, or dropped. Unlike `scores` (which
  UPSERTs — a re-score overwrites the row), the log is insert-only: a re-score is
  its own immutable entry, so the full history survives. Each `score` entry
  carries the validator's sr25519 signature, so authenticity (who scored) and
  integrity (nothing tampered) are both checkable off the public feed. Never
  carries `per_case`.
  **Storage note:** the canonical chain lives in Postgres (`score_audit_log`)
  because the append must be *transactional with the score write* — durable iff
  the score is, which a separate bucket object cannot guarantee. The public feed
  above is the read surface; mirroring/anchoring entries into the results bucket
  (or periodically checkpointing `head_hash` there for an external timestamp) is
  an infra add-on on top of this verifiable core, not a correctness dependency.
- `GET /api/v1/public/weights` → a block-consistent native read of the public
  `SubtensorModule.Weights` matrix: validator UID/hotkey plus each non-zero raw
  destination UID/hotkey/u16 value. The response also names the subnet-owner
  hotkey so clients can separate the 80% burn route from the KOTH miner pool.
  Under commit-reveal these are necessarily the last **revealed** vectors and may
  lag encrypted active commitments; they are validator inputs to stake-weighted
  Yuma consensus, not a claim about final miner emissions. The dashboard calls a
  miner a validator's **top choice** when it has that validator's highest revealed
  miner weight, and counts **validator support** whenever it has any revealed
  weight. The term **champion** is reserved for the KOTH emissions projection.
  The chain read is cached and refreshed in the background (one in-flight read
  process-wide, no matter how many clients are polling), so a served response
  never waits on chain. `generated_at` is therefore when the matrix was read
  rather than when the response was served, and `age_seconds` is the gap. When a
  re-read fails, the last known good matrix is served with `stale: true` instead
  of a 503 — the block it pins is still real chain state, just older — and a
  reader should label it rather than treat the matrix as absent. A 503 is
  reserved for having never read the matrix, or having last read it so long ago
  that presenting it would misrepresent current chain state.
- `GET /api/v1/public/health` → subnet rollup **from what the platform records**:
  `miners`, `scored_miners`, `scored_agents`, `last_scored_at`, `scores_24h`,
  `avg_latency_ms`. Note: no `success_rate` — the platform only ever sees a
  *successful* score, so run started/failed counts and set-weights latency are
  validator-side telemetry (wandb), not fabricated here. Detailed ops stay on the
  existing Prometheus `/metrics`.
- `GET /api/v1/public/validators` → the latest signed software heartbeat from
  each reporting permitted validator: public hotkey, package/protocol version,
  current worker phase/work id, reliable first-seen time when known,
  report/receive times, availability, health, and coarse CPU/memory/disk/Docker
  aggregates. Active benchmarks refresh `running_benchmark` every two minutes.
  Protocol v1/v2 reporters remain valid and return `system_metrics: null`; this
  is “not reported,” never an outage. A missing hotkey means it has not proved
  heartbeat-capable software; this endpoint does not pretend to enumerate every
  on-chain permit holder.

  Slot capacity is published as two distinct numbers, because they answer two
  different questions. `configured_slots` (with `healthy_slots`) is what the
  validator *advertises*; `allowed_slots` is what the platform will actually
  fund on it right now — the operator's `max_concurrent_slots` cap narrowing the
  advertisement, clamped to one while that validator's disk ceiling is tripped.
  The response-level `slot_policy` carries the fleet-wide cap and disk ceiling
  so the difference is explainable without re-deriving the policy. Advertised
  capacity above `allowed_slots` never receives a ticket, and presenting it as
  available capacity overstates the fleet.

  Heartbeat protocol v7 signs a closed `capabilities` object and a fixed
  six-component `stack` identity (`ditto_subnet`, `dittobench_api`,
  `sandbox_docker`, `model_relay`, `pylon`, and `ollama`). One length-prefixed,
  canonical JSON token removes field-order and delimiter ambiguity. Protocols
  v1-v6 retain their existing signing bytes. The v7 schema and canonical bytes
  are frozen; any new signed capability requires a later protocol version and
  is never inferred for an older reporter.

  Assignment remains mixed-fleet compatible. Pre-v7 reporters retain legacy
  source eligibility. Validators advertising screened-image support plus source
  fallback prefer complete verified image tuples and may receive source-only
  submissions. Image-only validators receive only complete verified tuples. An
  incompatible unstarted lease is released when a validator becomes image-only;
  a fresh `running_benchmark` report preserves active work. Malformed,
  contradictory, or stale v7 identity fails closed instead of downgrading.

  The public response exposes only the typed allowlist. Component versions,
  digests, and provenance are self-reported compatibility telemetry: the
  validator signature proves who reported them, not independent host or image
  attestation. Signatures and arbitrary host/container fields remain private.

  Heartbeat protocol v15 adds `capabilities.scorer_benchmarks.probe`: the
  observation behind the scorer status rather than the conclusion drawn from it.
  It carries `outcome` (`served`, `served_degraded`, `http_error`, `unreadable`,
  `timeout`, `connect_error`, `not_probed`), the `observed_at` of the probe, the
  `http_status` when the scorer answered, a `reason` when a readable reply was
  partly rejected, `last_served_at`, and `consecutive_failures`. Without it a
  scorer that never answered and a scorer that answered with something unusable
  are identical on the wire: both report null identity fields. The field is
  additive and optional, so a pre-v15 reporter signs exactly the bytes it always
  did and reads `scorer_liveness: unreported`, never a claim that its scorer is
  fine. A validator whose probe reports no usable answer is published as
  `health: critical`, which is why `critical` joins the fleet health values.
  Reporting is deliberately not a precondition for acceptance: telemetry that
  can silence a validator is worse than telemetry that is missing.

  Each entry also carries `bench_serviceability` against the response's
  `active_bench_version`: `serving`, `scorer_unverified`, or
  `software_obsolete`. This is the gate ticket issuance already applies, asked
  one question earlier — a validator that cannot serve the benchmark the fleet
  is scoring is leased nothing, so anything other than `serving` is published as
  `health: critical` no matter how clean its host metrics are. The two failure
  values are kept apart because they call for different responses:
  `software_obsolete` is a heartbeat protocol that cannot describe the active
  benchmark at all and only an upgrade clears it, while `scorer_unverified` is
  current-enough software whose scorer is not advertising that benchmark and a
  fix can clear it. The verdict is capability alone: a quiet or offline
  validator that still advertises the active benchmark reads `serving`, so
  liveness is never laundered into a capability claim. The legacy benchmark
  version is exempt, exactly as it is exempt from the leasing gate.

  Heartbeat protocol v4 optionally signs a ticket-bound benchmark stage and
  aggregate `completed`/`total` check counts. The platform revalidates the live
  ticket, evaluating agent, freshness, stage allowlist, bounds, and monotonic
  high-water mark before projecting progress. Public responses derive only six
  progress fields: `agent_id`, `agent_name`, `stage`, `completed_checks`,
  `total_checks`, and `percent`. Percent is rounded to the nearest 5% and capped
  at 95% until the score is submitted; finalizing/signing may therefore show
  `95%` with all checks complete. Terminal, expired, stale, requeued, or
  mismatched tickets clear public active work. Older clients and v4 heartbeats
  that omit progress remain compatible and show an explicit unknown-progress
  state. A private agent-bound high-water mark survives idle, polling, and
  downgrade reports so a late same-ticket heartbeat cannot restart at a lower
  count; it is never part of a public response.

  Validator heartbeat progress never publishes case ids, case categories or order, prompts, answer
  keys, tool names, memory or generated-dataset contents, pre-disclosure dataset
  hashes, seeds, canaries, partial scores, per-case outcomes or latency, model
  output, harness/build logs, internal run/container ids, paths, IPs, or error
  bodies. The API constructs the public shape from an allowlist rather than
  forwarding validator JSON or a validator-supplied display string.
- `GET /api/v1/public/screeners` → the equivalent public-safe view for
  platform-operated screeners. Reports use the existing dedicated screener
  bearer-token and hotkey-signature boundary, not a validator mnemonic or
  validator permit. `policy_version` is included so operators can spot a stale
  gate alongside its package/protocol version.

  Fleet telemetry is an explicit allowlist. The signed ingestion contract
  accepts only five-point CPU/memory/disk percentages, one aggregate Docker
  state, bounded running/unhealthy counts, and a sample timestamp within the
  heartbeat freshness window. Payloads are capped at 4 KiB, stored revalidation
  drops malformed historical values, and the public projection omits the sample
  timestamp to reduce temporal resolution. Hostnames, IPs, cloud instance ids,
  filesystem paths, image digests, container names, secrets, env values, wallet
  paths, key material, signatures, source digests, and arbitrary keys are never
  returned. Five-minute freshness determines `available`; a separate 15-minute
  grace window reports a delayed heartbeat as `stale`, and only older reports
  become `offline`. A newly accepted heartbeat immediately recovers the row.
  Missing optional metrics determine only `health: unknown`. Values are sampled
  at most every two minutes and rounded to five-point buckets.

  Ticket-bound benchmark progress is optional decoration on an otherwise valid
  signed heartbeat. If the referenced lease has expired, disappeared, or no
  longer belongs to an evaluating agent, ingestion drops only the active-work
  context and still records liveness, version, state, and safe system metrics.
  It does not update the ticket, submission, benchmark, or any accepted score.
  Progress regression for a still-valid lease remains rejected. Telemetry
  failure therefore cannot abort scoring or weights.
  `waiting_for_relay` is a reversible running sub-stage: it preserves aggregate
  check counts while the scorer backs off between provider attempts, then may
  return to `running_benchmark` without being treated as a regression.

  Screener heartbeat protocol v2 may also sign a current-job start time and one
  coarse stage: `preparing`, `downloading`, `validating`, `building`, `starting`,
  `health_check`, or `submitting`. The public view adds the already public agent
  identity/name, and the dashboard derives elapsed time. Progress appears only
  while the heartbeat is fresh and its screener, agent, and live screening lease
  still match; idle, terminal, expired, and offline rows clear it. Protocol v1
  remains accepted and shows neutral screening state without granular progress.
  The signed pair uses the existing screener telemetry JSON column, so no
  migration or secret is required.

  Progress never accepts or returns source, build output, dependency or image
  metadata, Docker layers, policy modules or rules, fingerprints, prompts,
  challenges, verdict evidence, secrets, paths, or arbitrary display text.
  Heartbeats remain best-effort and do not gate claims, screening, or verdicts.
- Per-category means + run provenance: the scoring engine (dittobench-api) emits
  `models` + `per_category` (alongside `bench_version`, `dataset_sha256`,
  `lexical_gap`, `paraphrase`, `seeding_waves`, `tokens`) in `RunDetails`; the
  validator forwards the whole blob unsigned as `ScoreReport.details`, the
  platform persists it verbatim to `scores.details` (merged with `per_case`, not
  overwritten), and the public endpoint surfaces only the safe subset. Category
  means come straight from `details.per_category`, never re-derived from
  `per_case` at read time.

## Surface 3 — dashboard (custom front door)

Static SPA (no server-side secrets) pulling the public API above; wandb linked
for the deep dive. Sections:
- **Submission pipeline** — recent agent uploads, miner hotkey, public lifecycle
  stage, screening/review evidence, submission time, and accepted provisional
  composites with reproducibility commands, visible before scoring completes.
  The atomic operations snapshot also carries the frozen desired-version
  rollout cohort as `rollout_queue`, so inherited agents remain visible as
  queue work while their active-version scores continue to drive weights.
  Each submission shows its current source-release embargo and exposes a download
  control once available.
- **Leaderboard** — rank, miner, composite, category radar sparkline, weight %,
  trend arrow; champion highlighted.
- **Miner drill-down** — composite history, category radar, ATH badge, best run
  (aggregate only).
- **Weights** — current on-chain allocation (pie/bar), champion callout.
- **Fleet health** — validators by default, with a keyboard-accessible native
  “Show screeners” checkbox. One dense responsive table shows availability,
  heartbeat age, current work, version, first-seen time, and coarse system
  metrics without turning each machine into a repetitive card. Validator rows
  always retain the shortened public hotkey, full-hotkey title, and an accessible
  copy button with clipboard fallback and announced success/failure state.

### Optional Taostats validator names

`GET /api/v1/public/validator-names` exposes a cache snapshot containing only
`validator_hotkey` and `display_name` for hotkeys already present in the
platform's public validator fleet. The dashboard escapes names as untrusted text
and continues to show each short hotkey, so duplicate names remain unambiguous.
The name route is decoration only: validator availability, scheduling, scoring,
and weights never read it.

The cache refreshes in a background task with a 1.5-second default timeout, a
one-hour refresh interval, a five-minute minimum retry interval, and a bounded
24-hour stale-while-revalidate window. HTTP errors, timeouts, malformed JSON,
rate limits, an unknown hotkey, or an expired cache produce an empty fallback;
no public request performs Taostats I/O. The response parser accepts only the
documented address/hotkey and name fields, bounds record and name counts, and
drops control and bidirectional-spoofing characters. Configuration accepts only
HTTPS URLs on `api.taostats.io`.

As checked on 2026-07-14, Taostats documents
`/api/dtao/validator/available/v1` for subnet validators, and its current welcome
page says API keys are free. An unauthenticated SN118 request returns `401`, so
enrichment requires the optional `DITTO_TAOSTATS_API_KEY` secret alongside
`DITTO_TAOSTATS_VALIDATOR_NAMES_URL`; no paid plan is required. Its terms reserve
rate-limit and access changes. The public metagraph page contains rendered data,
but HTML scraping is deliberately unsupported. Both settings are disabled by
default, so deployments without a key use hotkeys without changing core
behavior or making failed anonymous calls.

Hosting: static build → object storage + CDN (matches the earlier static-serve
idea); no server needed since all data comes from the public API + wandb.

## Build order

1. ✅ Public API — `/api/v1/public/leaderboard` + `/api/v1/public/health`.
   `/weights` is a read-only native Subtensor overlay; validators still own weight
   submission. The leaderboard keeps the current champion/tail projection, the
   last revealed validator vectors, and eventual Yuma emissions explicitly
   separate so raw score rank cannot be confused with any of them.
2. ✅ wandb `telemetry.py` in the validator (ditto-subnet #27) — aggregate +
   per-category tables, opt-in and off by default.
3. ✅ Dashboard SPA (`dashboard/index.html`) against the public API + wandb link.
   Now renders a per-row model chip (harness/generator + `bench v{N}`) and a
   drawer "Benchmark run" section (cases, median latency, bench version,
   harness/judge/audit/generator models, dataset SHA-256, per-category
   breakdown).
4. ✅ Run provenance persisted end-to-end (2026-07-07): dittobench-api
   `RunDetails.{Models,PerCategory}` → `ScoreReport.details` → `scores.details`
   → public leaderboard `models`/`bench_version`/`dataset_sha256`/`per_category`.
   Verified live on localnet against a real Ollama-backed harness run. The
   `ScoreReport.details` field is unsigned and additive — the signed tuple
   (`run_id, seed, composite, tool_mean, memory_mean, median_ms, n`) is
   unchanged, so this never touches the score or the signature.
5. ✅ Per-submission k=3 transparency (2026-07-09): `/api/v1/public/submissions`
   + `/api/v1/public/agent/{id}/scores` publish which validators scored each
   finalized agent, all k scores + signatures, the finalized median, and the
   pinned dataset (raw seed + sha256). Reads the existing `scores` rows; no
   schema change. This is the "transparency is the trust mechanism" surface for
   the decentralized k=3 model. Dashboard drill-down to consume it is TODO.
7. ✅ Auditability opening (2026-07-09): on-chain seed derivation (verifiable,
   removes platform seed-trust), `/public/agent/{id}/dataset` finalized-dataset
   reveal (task A, independent re-grade), per-validator per-case breakdown (task
   C), and `/public/bench/{version}/corpus` retired-version full-corpus release
   (task B). Opening the generator itself is planned + approval-gated (see
   dittobench-api `docs/open-generator-plan.md`), NOT done.
6. ✅ Append-only hash-chained audit log (2026-07-09, task #51):
   `score_audit_log` table (migration `d3a9f5e17c24`) + `/api/v1/public/audit`.
   Every score submission appends one immutable, SHA-256-chained entry in the
   score-write transaction; quorum appends an `agent_finalized` entry. Replayable
   + verifiable off the public feed (`verify_audit_chain`); tamper (edit/reorder/
   drop) breaks the chain. Bucket mirror/anchor is an optional infra add-on.
