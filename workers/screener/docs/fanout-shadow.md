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
making an inference request. Every specialist and the fresh critic receives the
manifest identity and active module list in its policy prompt. The report calls
its coverage `source_review`: it does not claim to re-execute other manifest
modules such as the behavioral oracle.

Five independent specialists and up to four deterministic file groups run with
separate transcripts. A fresh critic receives candidate locations and must read
the original source. Minority findings, critic disagreement, missing reads, and
uncertainty are retained. `critic_also_flagged` is another candidate observation,
not proof and not a vote.

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
bound plus the full 2,400-token completion before each request. Concurrent passes
share one atomic ledger. The price envelope is $0.60/M input and $2.00/M output,
four times the highest listed non-batch route seen on 2026-09-14 for the exact
GLM model. See the [OpenRouter model page](https://openrouter.ai/z-ai/glm-5.3-flash)
and [Z.ai provider page](https://openrouter.ai/provider/z-ai). A response whose
model id changes, or whose token or cost fields are missing, stops further
admission and makes the report incomplete. A reported price above the envelope
does the same.

These are conservative application admission controls, not an upstream billing
guarantee. Before activation, create a dedicated Ditto Router key for this lane,
set its own $20 daily spend cap, and store it in a separate GCP secret. Set
`platform_targon_fanout_shadow_secret` to that secret resource. Never reuse or
constrain the authoritative source-review key. The dedicated key is the second
billing guard and its key-usage endpoint is the source for billed spend; Platform
continues to show reserved and response-reported cost separately.

## Activation

The code defaults off and the production Ansible secret resource defaults empty.
Both gates must be deliberately changed after the merged release exists:

1. Confirm release deployment completed and a succeeded trusted screener image
   exists with `source_sha` equal to the merged release commit.
2. Provision the dedicated Router key with the $20 daily cap, place it in its own
   GCP secret, grant only the existing screener bootstrap identity access, set
   `platform_targon_fanout_shadow_secret`, and converge Platform.
3. Read the complete global screener-review revision through Backroom. Apply a
   complete successor revision that preserves authoritative fields, sets
   `fanout_shadow_mode=shadow`, names the trusted image source SHA, and uses the
   pilot limits above.
4. Submit a new ordinary screening case. Do not rescreen or mutate a held case to
   manufacture coverage.
5. Read `get_screener_fanout_shadow`. Confirm the row has the same artifact and
   manifest identity as its baseline, only the shadow rental is present after
   authoritative completion, and reserved/reported/key-billed spend reconcile.

Watch coverage, disagreements, latency, response model ids, unmetered responses,
reserved spend, and dedicated-key billed spend. Stop the pilot on any model-id or
manifest mismatch, missing metering, baseline capacity pressure, or unexplained
spend difference.

## Rollback

Apply a complete global screener-review revision with
`fanout_shadow_mode=off`. The loop marks queued rows skipped and active rows
incomplete, revokes their job tokens, and deletes their rentals. Baseline
screening continues. After the cancellation is visible, clear
`platform_targon_fanout_shadow_secret` and converge Platform if the credential
path should also be disabled. Historical comparison rows remain read-only.
