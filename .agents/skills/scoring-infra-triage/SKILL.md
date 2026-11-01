---
name: scoring-infra-triage
description: Diagnose a suspected infrastructure failure behind a low or failed SN118 validator score or continual retest, decide whether it is our bug or the agent's, fix ours before any retry, choose the right retry tool, and answer the miner. Use for "my retest looks like an infra failure", score drops after a fleet change, OOM / seed-timeout / provider-failure reports, harness_oom vs host_memory_pressure, retry or replace a validator score, and deciding when an agent cannot be retried.
---

# Scoring infra triage

Find the failure before scheduling anything. A retry on an unfixed bug spends
another sample on the same fault, and a retry on the agent's own bug hides it.
Observability is the deliverable: if a step below could not see something, add
the read (last section) before deciding.

Load `$backroom-review` (authority rules), `$gcloud-ditto-readonly`,
`$wandb-ops` and, for runtime and concurrency, `$ditto-subnet-benchmark`. Use
the OAuth `sn118-backroom` MCP (`https://backroom.dittobench.ai/mcp`); call
`get_backroom_access` first. Do not message miners from this skill.

## 1. Pin every affected sample

Reports are truncated leads. Resolve exact agent UUID, validator hotkey,
ticket, purpose, seed, run ID and accepted time for **every** low sample.

- `get_continual_retest_diagnostic` — canonical vs official composite and
  sample counts. `retest_samples` lists each confirmation sample with its
  serving grant's `chat_requests`, `failed_requests` and `failure_codes` by
  relay `terminal_error_code`. If `official_sample_count` exceeds the canonical
  count, the gap is confirmation samples.
- `get_agent_scores` / `get_score_history` — canonical `scores` rows only.
- `get_validation_retry` — ticket `purpose`, `attempt_count`, `failure_reason`,
  `failure_detail`, `container_log_tail`, `infra_retry_grants`,
  `provider_outage`.
- `list_validator_assignments generation=all`, `list_lease_revocations
  agentId=...` — live and ended leases, operator evictions.
- Public fallback: `GET /api/v1/public/agent/<id>/pipeline` has
  `confirmation_scores` (per validator) and `inference_runs` (per grant:
  requests, prompt tokens, created/updated).

## 2. Evidence, in this order

1. **Backroom reads.** Per sample: the `retest_samples` failure codes;
   `get_inference_runtime_metrics`; `get_inference_failure_taxonomy` (live
   windows only, 60 s to 1 h, so it cannot see last night); `get_validator_fleet`
   (heartbeat and stack identity); `get_validator_capacity` and
   `get_validator_slot_settings` (slot and memory admission).
2. **Relay traces** for a past window: `list_inference_traces dt= hour=
   kind=chat lane=inference`, then `download_inference_trace` per segment
   covering the ticket. Aggregate locally by `grant.agent_id`,
   `grant.validator_hotkey`, `request.grant_id`, `outcome.status` and
   `upstream.phases[].status/body.error`. `peek_inference_trace` caps at 50
   records, so it can sample but not count.
3. **Prod DB** (`query_prod_db.sh`): `confirmation_scores`, `validator_tickets`,
   `inference_grants`, and `inference_requests` grouped by `status`,
   `terminal_error_code`, `upstream_provider`, `timed_out` for the grant;
   `provider_outage_circuits`. If gcloud reauth fails, say so and continue with
   the traces. Do not hunt another credential.
4. **Logs.** `read_platform_logs.sh grep '<agent_id>|<grant_id>'` and the relay
   files (`--file relay-1`) for `provider circuit opened`, settle errors and
   lease parking.
5. **W&B** (`$wandb-ops`). Validator runs carry sweep and weights summaries,
   not per-ticket inference, so W&B corroborates host or process loss. It
   cannot clear a relay fault.

Signatures:

| Evidence | Reading |
|---|---|
| Requests about 2x a peer run, the same or fewer tokens, fast zero-token failures | Upstream refusal. Read the phase body: `402` with `limit_source=openrouter_credits` is gateway credit exhaustion; `429`/`503` is backpressure. **Ours.** |
| `failure_detail=provider_outage_parked` | The circuit worked; the no-fault resume is automatic. |
| `host_memory_pressure`, or `harness_killed_137` once | **Ours** (validator host). A `137` repeated on a second validator is the harness's. |
| `harness_oom` | **The agent's**: it hit its published cap while the host had headroom. |
| `seed_deadline_exceeded` | Unattributed. Compare the agent's `/seed` time on healthy validators and peers on the same host. |
| Clean grant, normal request count, low composite | Probably real variance or the agent. Compare peers on the same seed. |

## 3. Systemic or agent-unique

Both tests, always.

- **Systemic.** Over the same window, validator and bench version, did other
  agents' samples drop or their grants fail? Scan the ranked board's
  confirmation samples against canonical medians since the last fleet change,
  and check whether the failure stops at a fleet event (credits restored,
  circuit closed, deploy). A fault that only one agent saw can still be
  fleet-wide if that agent was the only traffic. Check the per-minute traffic
  before calling it unique.
- **Agent-unique.** Memory near the cap, high case concurrency, long `/seed`,
  an unusual tool pattern, or a provider or model quirk only this agent hits.
  Compare the same agent on a healthy validator in the same hour.
- **Recent change.** Name the fleet PRs in the window (screened-image-only
  validators, OOM reclassification, memory admission, cohort size) and test
  each one against the evidence, not against the timing alone.

## 4. Fix before retry

Our bug: fix it in one monorepo PR (producer, consumer, tests), merge on the
repository bar, then verify release, deploy and live behaviour as separate
facts. Retry only after the fix is live, or after the outage has clearly ended
and cannot recur during the retry.

| Case | Tool |
|---|---|
| Accepted canonical `scores` row measured infra | `get_validator_score_replacement` → `replace_validator_score` with its snapshot and run ID. Blocked while the validator is busy: re-read later. |
| Slots exhausted by infra before quorum | `get_validation_retry` → `retry_validator_evaluation` (`targetValidatorHotkey` for one slot; `batch_retry_validator_evaluation` for many). An open circuit needs `acknowledgeProviderOutage`. |
| v9 contract mismatch on an accepted score | `list_v9_contract_retests` → `queue_validator_score_retests`. |
| LongMem confirmation bundle | `get_confirmation_bundle` → `authorize_confirmation_bundle_retest`. |
| Continual-retest `confirmation_scores` sample | `get_continual_retest_diagnostic` (copy `retest_samples` run_id and composite) → `void_infra_scores` with `dryRun` first, then `VOID INFRA SCORES`. It is gated on the grant's outage-class failures. The seed re-runs at the next `void_generation`; read it back with `list_infra_voids`. Check `get_provider_outages` first: the post-outage sweep may already have voided it (source `outage_sweep`). |
| Canonical score in a sweep's `canonical_flagged` | `get_provider_outages` lists it with the grant's outage counts and window; review with `replace_validator_score`. The sweep never replaces canonical scores. |
| Parked by `provider_outage_parked` | Nothing to do; the resume is automatic after the quiet window. |

Read the result back (`get_agent_scores`, `get_continual_retest_diagnostic`,
`get_validation_retry`) and report the new score with its run ID.

## 5. When not to retry

- The agent's own fault: `harness_oom`, a repeated `137`, a deterministic
  crash in its log tail, or a `/seed` slower than peers on healthy hosts.
- The evidence is mixed or a source is unreadable: say what is missing (§7).
- The bug is not yet fixed live, or the outage is still open.
- The run's grant does not meet the outage evidence gate (`insufficient_evidence`).

## 6. Miner reply

Use `$miner-comms` for wording. Facts first, then send.

- **Our bug.** Fix, verify live, retry, read back, then reply: what failed,
  that it was ours, what changed, and the new score.
- **Their bug.** Give the RCA and the exact fix: file or limit, the observed
  number against the cap, and how to rehearse it locally (`$mine`). No retry.
- **Blocked.** Say it was ours and what the repair is waiting on. Do not
  promise a score.

## 7. Missing observability → add a Backroom read

If a step needed traces, SSH or a hand-rolled script to answer something
Backroom should show, add the read or log field in the same session (Platform
endpoint, Backroom tool, `docs/mcp.md`, tests), then decide. Name the gap in
the report even when you worked around it.

## Report

Per sample: validator, ticket, timestamps, failure reason, failed calls by
code, signature. Then the systemic vs unique verdict with its counterexample,
the fix PR and its live verification, the retry actions with read-backs (or why
none), new observability PRs, and the 2–3 sentence miner reply.
