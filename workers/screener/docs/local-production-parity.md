# Local vs production: screening and grading parity

This page is for miners who see a local result that production does not
reproduce. Two cases come up:

- A local screen passes but production rejects or holds the submission.
- A local score on "the same seed" is higher than the live score.

All of the production screener lives in this monorepo, in `workers/screener/`,
and none of it lives anywhere else. What a local run cannot see is
**production data and settings**, not code. Those are: the live review posture,
your lineage history, other miners' unreleased source, and a small set of
private inputs. This page lists each one and shows how to get as close to
production as the repo allows.

## 1. The step-limit setting (`SOURCE_REVIEW_MAX_STEPS`)

No setting has the literal name `SOURCE_REVIEW_MAX_STEPS`. The real knob has
two layers:

| Layer | Name | Default | Allowed range |
|---|---|---|---|
| Worker env (bootstrap only) | `SCREENER_SOURCE_REVIEW_MAX_STEPS` | `200` | 1–240 (`ditto_screener/config.py`) |
| Platform review-settings revision (what production runs) | `source_review_max_steps` | `200` | 1–240 (`ScreenerReviewSettings`) |

A production worker fetches its review posture from Platform and **overwrites**
the env value, together with every other L1/L2/L3/L4 limit in the table below
(`ReviewSettings.apply_to` in `ditto_screener/review_settings.py`). The env
default of 200 is therefore not what production uses. The live posture is.

Each worker resolves its posture in this order: its own instance scope, then its
node scope (for example `subnet-screener-1`), then the global `*` scope. A
revision in `inherit` mode falls through to the next scope.

### Read the live values

```bash
curl -s https://dittobench.ai/api/v1/public/screener-review-settings | jq
```

The endpoint returns three things:

- the screening policy version the queue currently requires;
- the latest revision for every scope;
- each scope's role: `global`, `node_or_worker`, `integrity_double_check`, or
  `l2_report_canary`.

The response is an allowlist. The private policy-manifest rotation and the
operator's reason and actor are never included. Each field maps onto the
`SCREENER_*` env var named in the table below.

An operator support note (unverified when this page was written) reported
revision 142, scope `subnet-screener-1`, with these values:

- step limits: L1 240, L2 256, adjudicator 128;
- timeouts: 3600 s, 2700 s and 600 s;
- caps per attempt: 5M input tokens and $25.

Trust the endpoint over that note: revisions change.

### Every review knob

| Layer | Public field | Worker env var | Env default | Platform default |
|---|---|---|---|---|
| L1 model | `source_review_model` | `SCREENER_SOURCE_REVIEW_MODEL` | `openai/gpt-6-luna` | `openai/gpt-5.6-luna` |
| L1 steps | `source_review_max_steps` | `SCREENER_SOURCE_REVIEW_MAX_STEPS` | 200 | 200 |
| L1 timeout | `source_review_timeout_seconds` | `SCREENER_SOURCE_REVIEW_TIMEOUT_SECONDS` | 1800 | 3600 |
| L1 read budget | `source_review_max_read_bytes` | `SCREENER_SOURCE_REVIEW_MAX_READ_BYTES` | 8000000 | 8000000 |
| L1 output/turn | `source_review_max_completion_tokens` | `SCREENER_SOURCE_REVIEW_MAX_COMPLETION_TOKENS` | 8000 | 8000 |
| L1 effort | `source_review_reasoning_effort` | `SCREENER_SOURCE_REVIEW_REASONING_EFFORT` | high | high |
| L2/L3 mode | `mode` | `SCREENER_L2_REVIEW_MODE` | off | off |
| L2 model | `l2_model` / `l2_fallback_models` | `SCREENER_L2_REVIEW_MODEL` / `SCREENER_L2_FALLBACK_MODELS` | `openai/gpt-6-sol` / `z-ai/glm-5.2` | `openai/gpt-5.6-terra` |
| L3 critic | `l3_enabled` / `l3_model` | `SCREENER_L3_REVIEW_ENABLED` / `SCREENER_L3_REVIEW_MODEL` | true / `openai/gpt-6-sol` | true / `openai/gpt-5.6-sol` |
| L2 steps | `max_steps` | `SCREENER_L2_MAX_STEPS` | 256 | 32 |
| L2 timeout | `timeout_seconds` | `SCREENER_L2_TIMEOUT_SECONDS` | 1800 | 1200 |
| L2 input tokens | `max_input_tokens` | `SCREENER_L2_MAX_INPUT_TOKENS` | 5000000 | 425000 |
| L2 output tokens | `max_output_tokens` / `max_completion_tokens` | `SCREENER_L2_MAX_OUTPUT_TOKENS` / `SCREENER_L2_MAX_COMPLETION_TOKENS` | 1000000 / 16000 | 20000 / 2400 |
| L2 cost cap | `max_cost_usd` | `SCREENER_L2_MAX_COST_USD` | 25.00 | 6.00 |
| Hold thresholds | `concern_hold_count` / `clear_min_notes` | `SCREENER_REVIEW_CONCERN_HOLD_COUNT` / `SCREENER_REVIEW_CLEAR_MIN_NOTES` | 3 / 3 | 3 / 3 |
| L4 adjudicator | `adjudicator_mode` / `adjudicator_max_steps` / `adjudicator_timeout_seconds` | `SCREENER_ADJUDICATOR_MODE` / `_MAX_STEPS` / `_TIMEOUT_SECONDS` | off / 128 / 600 | off / 128 / 600 |

The env and Platform defaults differ. If you run a local screen without the
live posture, you are not running production's limits.

## 2. Running a production-parity screen locally

### a. Static checks (what `ditto verify` covers)

```bash
uv run ditto verify --path agent.tar.gz
```

This runs the archive contract only: size, gzip, tar, Dockerfile contract and
source limits. It does no build, no health smoke and no model review. A clean
`verify` result is **not** a screen. It is the most likely reason a "local
screen script" passes cleanly.

### b. Build and health gate

The production build runs as a rootless Docker build, followed by a hardened
`/health` smoke as UID 65532 with a read-only root filesystem. That gate is
`BuildGate` in `ditto_screener/gate.py`. To run it against your own artifact:

1. Install rootless Docker (`workers/screener/scripts/install-rootless-docker.sh`).
2. Run the integration path in `workers/screener/README.md` ("Local
   development").

### c. L1 → L2 → L3 source review with the live posture

The offline replay runs the real production Luna reviewer (L1), then the
analyst (L2) and the critic (L3), on one artifact. `--production-settings`
loads the live posture from the public endpoint above. That includes the
models, step caps, timeouts, token and cost budgets, and hold thresholds. The
values override the matching flags and are recorded in the results file under
`production_posture`.

```bash
# 1. Analyzer image (production pins ditto-screener-l2-analyzer:active).
docker build -f workers/screener/deploy/l2-analyzer.Dockerfile \
  -t ditto-screener-l2-analyzer:local workers/screener

# 2. Lay out your artifact by its digest, outside the repo.
SHA=$(shasum -a 256 agent.tar.gz | cut -d' ' -f1)
mkdir -p ~/screen/artifacts/$SHA && cp agent.tar.gz ~/screen/artifacts/$SHA/
cat > ~/screen/manifest.json <<EOF
{"items": [{"agent_id": "local", "attempt_id": "$(uuidgen)",
  "artifact_sha256": "$SHA", "expected_disposition": "safe",
  "expected_resolution_basis": null}]}
EOF

# 3. OpenRouter key in an owner-only file (the replay calls OpenRouter).
install -m 0600 /dev/null ~/screen/key && printf '%s' "$OPENROUTER_API_KEY" > ~/screen/key

# 4. Replay under the live posture.
uv run --project workers/screener python workers/screener/scripts/run_l2_calibration.py \
  --production-settings \
  --manifest ~/screen/manifest.json --artifact-root ~/screen/artifacts \
  --api-key-file ~/screen/key --analyzer-image ditto-screener-l2-analyzer:local \
  --results-file ~/screen/out.json --concurrency 1 --run-l1
```

To replay against a saved snapshot instead of the live endpoint, use
`--production-settings settings.json`. To use a node scope's posture, add
`--production-scope subnet-screener-1`.

Open `out.json` and the audit file for the disposition, the resolution basis,
any budget stop, and any error code. A run that ends with a budget stop or
`inconclusive` is a production hold, not a pass.

The replay loads the built-in policy profile, at the screening policy version
of your checkout (`SCREENING_POLICY_VERSION`, currently 14). Compare it with
`required_policy_version` from the endpoint. If they differ, check out the
release that matches before trusting a local pass.

### d. What a local run still cannot reproduce

These inputs exist only in production. Any one of them can make production hold
or reject what a local run passes.

1. **Diff-only vs full-scope review.** Production fetches a *lineage packet*
   for each resubmission. The packet is built from one of three references:
   your previous version, an earlier upload from the same coldkey, or a
   declared released base. It contains the diff from that reference, every
   prior operator ruling, and the parent's findings. The packet's `scope` is
   then set to `diff` or `full`.

   With `diff` scope, the reviewer spends its budget on the changed files and
   their callers, and re-checks every prior reject or concern against the
   current code. Production forces `full` scope when any of these hold:
   - the Dockerfile, build manifest or entrypoint changed;
   - more than 2500 changed lines, or too many changed files;
   - an opaque or unread file changed;
   - **the parent was reviewed under a different policy version**;
   - there is no review history.

   A local replay has no lineage packet. That cuts both ways:
   - **Local can be narrower than production.** A diff-scoped local check (for
     example, reviewing only the files you changed against the starter kit)
     passes code that a full-scope production review then reads. Every
     resubmission whose parent was screened under policy v13 gets a full-scope
     review once v14 is the required version.
   - **Local can miss your history.** A local run on a fresh agent never sees
     the prior rejects that production feeds back in as leads.
2. **Copied or ported code.** Production checks every upload for distinctive
   content from another owner's *unreleased* source, including ports across
   languages (`apps/platform/ditto/api_server/unreleased_copy.py`). It also
   triages near-duplicate families (`copy_court_family.py`). Neither check can
   run locally, because it needs every other miner's source.

   A feature being live on someone else's agent does not clear your copy of it:
   - Their version may still be under review.
   - Their version was reviewed under its own lineage and policy.
   - If their source is unreleased, your port of it is the thing the copy check
     catches.
3. **Starter-kit trust.** Only exact `path + sha256` matches against the
   provenance manifests in `ditto_screener/data/` are treated as starter code.
   Today those are kit v1, v3, v4 and v5. Files from newer kits are reviewed as
   your own code. A local tool that exempts "starter files" under-reviews.
4. **Operator provenance markers.** Production can scan for a small set of
   operator-defined rare markers (`SCREENER_SOURCE_MARKER_PATTERNS`). Matches
   are review leads only, never a verdict on their own. The values are kept out
   of source control on purpose.
5. **L4 adjudicator.** When `adjudicator_mode` is `enforce`, production runs
   the L4 court (`ditto_screener/adjudicator.py`) after L2/L3. The offline
   replay does not run L4.
6. **Model nondeterminism.** The same artifact can land differently on two
   runs of the same reasoning model. A single local `safe` is evidence, not a
   guarantee. Run the replay more than once on a borderline change.

Every medium- or high-risk result is quarantined for operator review. None is
rejected automatically.

## 3. Infrastructure errors are not charged to the miner

| Fault | Where | Outcome |
|---|---|---|
| Review-model 429, 5xx or timeout (L1/L2/L3) | screener | `retryable_infra`: the agent waits in `SCREENING_FAILED` for an automatic or operator retry. It is never rejected or quarantined for the fault. |
| Platform artifact-fetch 408/429/5xx | validator | Retried with backoff, then handed back as `infrastructure` (SN-426, #3348) |
| Validator's own DittoBench scorer unreachable, 5xx, or restarted mid-run | validator | Handed back as `infrastructure` (codes `scorer_unreachable`, `scorer_submit_unavailable`, `scorer_poll_unavailable`). Before SN-445 this was charged as miner `scoring_error`. |
| Every inference call in a run failed (relay or provider unavailable) | scorer | `model_relay_unavailable`: infrastructure, retried |
| Fleet-wide provider outage | Platform | Circuit parks every issued ticket as `infrastructure` |
| One 429, 5xx or timeout on a single harness call, in a run that otherwise reached the model | relay + scorer | The relay retries a 429/5xx once, but only when it provably cost nothing upstream. Otherwise the harness gets a 502, and if it does not recover, that case scores low. **This is intended.** A harness should retry or degrade gracefully, and the score measures what it actually served. |

An `infrastructure` hand-back parks the ticket for an operator-issued retry.
It never consumes the miner's attempt. If you see a `scoring_error` that you
believe is ours, raise it with the agent id; operators triage it with the
`scoring-infra-triage` runbook.

## 4. Grading: local score vs live score

`uv run ditto practice --run-size full` is the on-chain-shaped dataset. It still
differs from live grading in these ways (details in `.agents/skills/mine/SKILL.md`):

- **Model and provider.** Locally, the chat model and provider come from your
  `.env`. Live grading uses the locked `openai/gpt-oss-20b` through the
  ticket-bound relay, inside your screened container.
- **Bench version.** Without `--bench-version`, `ditto practice` follows the
  live `active_version` (`/public/bench/rollout`). Pinning an older version,
  such as `--bench-version 14`, compares against a contract that no longer
  scores.
- **Cost factor.** From bench v16, priced inference cost multiplies the
  composite, so a quality-only local number on v14/v15 is higher than a
  cost-weighted live one.
- **Seeds.** For initial scoring, take the exact seed from
  `GET /public/agent/{id}/scores` or `/pipeline` and replay it:

  ```bash
  uv run ditto practice --run-size full --bench-version <v> --seed <dataset_seed>
  ```

  Continual retests and confirmation runs use **shared cohort seeds** anchored
  on the current champion. They are a different dataset from your agent's own
  `dataset_seed`, so "the same seed" does not apply to a retest. Those seeds are
  deliberately not published (#2595), because a live shared seed would let a
  challenger precompute the datasets. Public data shows per-validator retest
  composites, `voided_as_infrastructure`, and per-seed medians. It does not
  show the seed behind each retest. Publishing retired retest seeds, for
  example after the anchor rotates, is tracked under SN-305 and SN-416.
