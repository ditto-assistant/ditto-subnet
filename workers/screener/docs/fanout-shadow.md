# Two-stage fan-out shadow pilot

The production pilot runs the normal authoritative screener unchanged, then
records a durable, lower-priority source-review experiment for the same artifact.
The fan-out record cannot sign or replace a verdict, update agent state, alter a
cache, change queue eligibility, quarantine, reject, ban, or retry a submission.
Skipped and incomplete work remain visible and never become a clean result.

Platform inserts the shadow row in the accepted baseline verdict transaction.
That insert performs no model or rental work, so the authoritative attempt can
release its admission slot immediately. The rental loop consumes the row later,
after build, runtime, source-review, and authoritative-finalization lanes. One
shadow job may run globally. Targon must retain at least one slot for baseline
work. The pilot never consumes the GCE overflow lane. If capacity is unavailable,
the shadow row ends incomplete rather than
holding or starving an authoritative job.

Each row binds:

- the baseline attempt and bounded baseline evidence;
- the source artifact SHA-256;
- the screening policy version;
- the exact policy-manifest profile, rotation id, and digest;
- the exact review-settings revision, scope, and checksum; and
- a trusted screener image built from the source SHA named by the operator.

The worker recomputes the built-in manifest digest inside that exact image before
making an inference request. Every specialist and the fresh stage-two adjudicator
receives the manifest identity and active module list in its policy prompt. The report calls
its coverage `source_review`: it does not claim to re-execute other manifest
modules such as the behavioral oracle.

Five independent specialists run with separate transcripts and full-archive
read-only tool access. The production pilot uses `partition=specialists`; bounded
file/hybrid partitions remain available for offline calibration. A capped hybrid
plan omitted files from normal submissions and spent its request allowance before
adjudication. The specialist protocol leaves room for corrections and adjudication
within the same 40-request limit. A fresh adjudicator receives every pass outcome and note,
including incomplete and no-finding passes, then must verify each candidate ID
against the original source. Its structured record labels each candidate
`supported`, `refuted`, or `unresolved` with source citations and counterevidence.
Omitted candidates stay unresolved. Support requires re-reading at least one exact
candidate citation; an unrelated finding cannot confirm another candidate.
Minority findings, disagreement, missing reads, and uncertainty are retained.
`critic_also_flagged` means at least one candidate received source-bound stage-two
support. It remains a shadow observation, not proof and not a vote.

## Pilot limits

The recommended global revision uses:

| Setting | Pilot value |
| --- | ---: |
| Shadow mode | `shadow` |
| Model | `z-ai/glm-5.3-flash` |
| Jobs globally | 1 |
| In-job concurrency | 2 |
| Requests per artifact | 40 |
| Pre-admitted token bound | 1,500,000 |
| Wall time per artifact | 900 seconds |
| Reserved cost per artifact | $3 |
| Rolling 24-hour Platform reservation | $20 |
| Targon slots kept for baseline | 1 |

The request ledger reserves the UTF-8 JSON byte count as an input-token upper
bound plus the full 8,000-token completion before each request. Concurrent passes
share one atomic ledger. A fully metered response settles its own token reservation
to actual token and reported dollar usage; in-flight and unknown-cost requests
retain their full bounds. Each response must fit its own pre-admitted envelope.
Platform retains the full $3 artifact reservation even after local settlement. The shadow uses
low reasoning effort and required tool calls to leave room for a structured result. An invalid structured
verdict gets at most two schema corrections within the same request/token budget; it is
never converted to a pass. Overlong display summaries are shortened to 240
characters while originals remain in `full_summaries` (up to 8,000 characters,
with explicit truncation metadata) and the adjudicator context. Risk, categories,
evidence and invariant decisions are never normalized. Validation errors and
field sizes are retained in the pass report.
The price envelope is $0.60/M input and $2.00/M output,
four times the highest listed non-batch route seen on 2026-09-14 for the exact
GLM model. See the [OpenRouter model page](https://openrouter.ai/z-ai/glm-5.3-flash)
and [Z.ai provider page](https://openrouter.ai/provider/z-ai). The production
lane is pinned to `https://router.heyditto.ai/v1` in passthrough mode. A live
2026-09-14 metering probe requested `z-ai/glm-5.3-flash` and returned the verified
native ID `glm-5.3-flash` with prompt tokens, completion tokens, total tokens, and
cost. Only those two exact request/response IDs are accepted. Any other model ID,
or missing token or cost field, stops further admission and makes the report
incomplete. A reported price above the envelope does the same.

Platform retains the full $3 reservation after every admitted job, including an
incomplete or unmetered job. The $20 rolling reservation ledger therefore admits
at most six shadow starts in any rolling 24 hours during this pilot, even when
reported or billed cost is lower. Further new submissions receive an explicit
`fanout-daily-budget-exhausted` skipped row while authoritative screening
continues. This makes pilot coverage bounded rather than promising an unbounded
second review for every submission; the reservation can be tuned after billed
spend and completion data are observed.

These are conservative application admission controls, not an upstream billing
guarantee. Before activation, create a dedicated Ditto Router key for this lane,
set its own $20 daily spend cap, and store it in a separate GCP secret. Set
`platform_targon_fanout_shadow_secret` to that secret resource. Never reuse or
constrain the authoritative source-review key. The dedicated key is the second
billing guard and its key-usage endpoint is the source for billed spend; Platform
continues to show reserved and response-reported cost separately.

The production secret resource is
`projects/ditto-app-dev/secrets/screener-fanout-shadow-router-key`. Its dedicated
Router key has a $20 daily cap and expires on 2026-10-14. The application role
default stays empty, and the global shadow setting stays off until activation.

## Activation

The code and global setting default off. The production host points at the
dedicated secret prepared for this pilot, but no job can launch while the global
setting is off. While it is off, Platform hashes the inactive fan-out block in
the pre-fan-out wire shape. Existing workers ignore the new response fields and
continue accepting their effective settings checksum during the rolling Platform
and native-worker deployment. An enabled revision binds every fan-out field and
therefore requires the upgraded worker. Activation happens only after the merged
release exists:

1. Confirm release deployment completed and a succeeded trusted screener image
   exists with `source_sha` equal to the merged release commit.
2. Verify the prepared dedicated Router key still has its $20 daily cap and
   2026-10-14 expiry, the GCP secret resolves, and only the existing screener
   bootstrap identity has access. Converge Platform so the host variable is live.
3. Upgrade the native screener release first. Confirm all four native workers
   heartbeat on the release that understands the fan-out settings fields and
   accepts the current effective settings checksum. A pre-fan-out worker ignores
   the new fields and cannot reproduce the checksum of an enabled successor
   revision, so do not enable shadow mode while any old worker remains.
4. Read the complete global and effective node-scoped screener-review revisions
   through Backroom. Production currently has a non-inheriting
   `subnet-screener-1` override, so changing the global revision alone does not
   enroll its submissions: the effective node revision would still have
   `fanout_shadow_mode=off`. First apply a complete successor to the global
   revision that preserves every authoritative field and sets the shadow image,
   limits, and mode. Then apply a complete successor to the
   `subnet-screener-1` revision with the same fan-out fields while preserving all
   of its authoritative fields, including its 16,000-token completion limit.
   The global mode is the fleet kill switch and supplies current operational
   ceilings; the effective node mode controls whether completed submissions are
   enrolled and pins the comparison settings. Both must be `shadow` for this
   fleet. Do not remove the node override.
5. Submit a new ordinary screening case. Do not rescreen or mutate a held case to
   manufacture coverage.
6. Read `get_screener_fanout_shadow`. Confirm the row has the same artifact and
   manifest identity as its baseline, only the shadow rental is present after
   authoritative completion, and reserved/reported/key-billed spend reconcile.

Watch coverage, disagreements, latency, response model ids, unmetered responses,
reserved spend, and dedicated-key billed spend. Stop the pilot on any model-id or
manifest mismatch, missing metering, baseline capacity pressure, or unexplained
spend difference.

## Rollback

First apply a complete successor to the `subnet-screener-1` revision with
`fanout_shadow_mode=off` so no new baseline completions enroll. Then apply a
complete global successor with `fanout_shadow_mode=off`. The global kill switch
marks queued rows skipped and active rows incomplete, revokes their job tokens,
and deletes their rentals. Preserve all authoritative fields in both successors,
including the node override's 16,000-token completion limit. Baseline screening
continues. After the cancellation is visible, clear
`platform_targon_fanout_shadow_secret` and converge Platform if the credential
path should also be disabled. Historical comparison rows remain read-only.

## Interpreting comparisons

Only succeeded reports with complete declared protocol coverage count as
comparisons or disagreements. `five-specialists-v1` requires exactly one complete
pass for each of the five named specialties, plus a completed, candidate-bound
adjudication when candidates exist. It is source-review protocol completion, not
an exhaustive per-file audit; the report explicitly sets
`exhaustive_file_audit=false`. Historical incomplete rows remain visible, with their original
errors and cost, but do not count as policy disagreement. A missing response is
an unmetered transport failure, not evidence of a different model; an explicitly
wrong model still stops admission.
