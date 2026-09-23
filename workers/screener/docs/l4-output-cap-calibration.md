# L4 completion-cap calibration

The `adjudicator_max_completion_tokens` review setting controls only the L4
forced-verdict call. When unset, L4 inherits `max_completion_tokens`, preserving
the existing worker-specific budget. This change does not alter the live cap.

Before lowering it, run a **report-only** replay on a fixed corpus of exact
submission UUID, artifact SHA, policy version, and independently adjudicated
L1–L3 notes/source excerpts. Include both difficult completed reviews and
attempts that previously timed out. Keep the artifact, prompt, model, routing,
temperature, and reviewer instructions fixed; vary only the L4 cap among
16,000, 8,000, and 4,000 tokens. Run in an isolated review environment with
no Backroom verdict or eligibility writes. Do not log private source excerpts
or the private transformations in the report.

For each cap and artifact, record completion/timeout, time to first stream
event, total latency, output tokens, upstream/provider, parsed tool verdict,
schema validity, evidence citation validity, and agreement with the independent
decision. Keep the exact artifact and attempt IDs in access-controlled evidence;
publish aggregate counts and latency percentiles only. A lower cap is eligible
for a guarded live setting change only if it lowers completion timeouts without
introducing any false CLEAR or REJECT, malformed/incomplete verdicts, or loss of
required citations. A timeout remains inconclusive at every cap.

The production setting should be changed separately after this calibration,
with worker-adoption confirmation from Backroom and a small canary cohort.
