# Report-only paired L4 verdict replay

`scripts/run_l4_verdict_calibration.py` tests whether Sol can replace the **final
L4 court**, given the **same frozen L1-L3 evidence ledger**, not whether one Sol
review can replace discovery by L1-L3. Both GLM 5.3 Flash and GPT-5.6 Sol run
the production `SourceReviewAdjudicator` contract with `ledger_final=True`,
policy v13, the same tool and compaction code, and the same case archive.
The script has no Platform or Backroom client and cannot release or reject a
submission. Without `--execute`, it only validates the private manifest and
artifact digests.

`--two-layer` is a separate report-only arm: it takes a frozen Sol-investigator
handoff for the **same exact case**, then runs only the model-diverse GLM final
court over that handoff's notes/finding/error code. It does not infer a final
decision from the investigator's risk label. The current #2022 result JSON
does **not** yet export its bounded canonical finding or all exact attempt
identity fields; a private SHA-bound exporter is required before real #2022
results can populate this handoff. Do not substitute its finding digest for
the actual finding or borrow L1-L3 notes.

The private manifest has `revision`, `policy_version: 13`, and a nonempty
`cases` array. Each case requires:

- exact Backroom `agent_id`, `attempt_id`, `artifact_sha256`,
  `manifest_digest`, `review_notes_digest`, `review_settings_revision`, and
  `policy_version`; also `baseline_worker_release` and the exact
  `baseline_prompt_revisions` for L1, L2, L3, and L4 (`not-run` for a layer
  that did not execute);
- a relative `archive` path under `--artifact-root`, whose bytes match the
  artifact SHA; frozen `notes` (1-48), their canonical JSON
  `notes_payload_sha256`, and the frozen `finding` and `error_code` (nullable);
- a named `cohort`; and `label` with `decision: clear|reject`,
  `provenance: independent-v13-source-review`, and nonempty
  `evidence_references` naming decisive `path:line` locations. Reject labels
  also require `accepted_reject_invariants` with one or more v13 invariant
  enum values (for example `i6_tool_execution_fidelity`).

`notes_payload_sha256` is SHA-256 of sorted-key compact JSON of `notes`. The
Backroom manifest and review-notes digests are separately retained as
provenance; this offline script cannot authenticate them against live Backroom.
Export the exact attempt from Backroom immediately before freezing the case.
Keep the manifest and artifacts private. Do not substitute a later attempt,
same-hotkey relative, or a new archive for the pinned row.

For `--two-layer`, each case may include `sol_investigator` with matching
`agent_id`, `attempt_id`, `artifact_sha256`, `policy_version`,
`manifest_digest`, and `review_settings_revision`; exact Sol model and prompt
revision; 0-48 frozen notes and their payload SHA; full canonical `finding`
and its digest (or both null); `error_code`; compaction count; elapsed
milliseconds; and metered investigator cost. An absent handoff, empty notes,
or unmetered investigator is an incomplete arm with no verifier call.

The same case may include `mandatory_host_evidence` references bound to its
UUID/SHA/attempt: `image_identity_digest`, `runtime_receipt_digest`,
`private_verification_or_nonapplicability_digest`, and
`i1_i8_s1_s3_sweep_digest`. Missing references cause an explicit
`mandatory-host-evidence-missing` incomplete result **before** any model call.
The replay validates only field presence, digest shape, and exact identity;
it cannot authenticate the receipts or certify their policy sufficiency.
Even with all references supplied, the report says
`references_supplied_unverified` and `policy_ready: false`. Only the actual
V13 host verification path may certify CLEAR or REJECT.

Before any paid run, assemble independently reviewed v13 cases covering
confirmed violations across I1-I8, legitimate safe harbors, difficult
false-positive patterns (including the V13 I6 endpoint-absent stub versus
endpoint-present scored-execution distinction corrected by #2167), easy
controls, and recent failures. Record selection
criteria before seeing either candidate result. Active escalations and
automated rescreens are **unlabeled stress cases**, not clear/reject gold.
Deduplicate by artifact family for accuracy reporting and retain exact rows for
operational replay. A single pilot or a failure-enriched sample is not a
representative parity test.
Keep pre-#2167 and post-#2167 worker/prompt baselines in separate comparison
strata. The report flags mixed baseline releases or prompt sets; an aggregate
across them is not a valid noninferiority comparison. Do not mark #2167 live
until Backroom confirms the release on the workers that screened those cases.

Execution requires a dedicated metered OpenRouter key with an independently
configured upstream hard spend cap. The command's reported-cost cap is only a
second, local guard; it cannot prevent an unmetered or in-flight request from
incurring charges. Do not use the production worker key or the GLM fanout key.
The runner stops after a missing cost/token/model field or model mismatch and
marks that case incomplete. It writes a mode-0600 private report after every
arm; no raw source, prompt, model text, or miner-visible reason is written.
An ordinary timeout/provider failure is also persisted as an incomplete arm
with a sanitized error class/code and elapsed time; the paired arm continues
only while the configured external hard cap remains the spend backstop and no
metering or served-model mismatch has occurred. Reported cost on an incomplete
arm is a lower bound, not a verified bill.

```bash
uv run --project workers/screener python \
  workers/screener/scripts/run_l4_verdict_calibration.py \
  --manifest /private/l4-corpus.json \
  --artifact-root /private/artifacts \
  --results-file /private/l4-results.json
```

Only after the route and cap have been verified, add `--execute`,
`--api-key-file`, `--max-reported-cost-usd`, and `--external-route-cap-usd`.
The latter is an operator attestation, not proof that the upstream cap exists.
Add `--two-layer` only with a private frozen Sol handoff and mandatory host
evidence references. The local spend cap applies to new verifier calls; the
investigator's already-incurred metered cost is separately recorded and added
to the report's end-to-end per-case cost.

The report records decisions, sanitized citations, invariant or clear clause,
token/cost metadata, route, latency, incomplete coverage, and the number of
fully paired cases. It separately flags exact invariant and label-citation
matches; a different candidate finding needs source-level human adjudication
and is not automatically wrong or "better." Escalations are neither false
negatives nor true negatives;
they remain incomplete. Compare only fully paired, independently labeled
cases, and inspect every disagreement against the cited source. This study
does not assess I1-I8 discovery recall, S1-S3, deterministic runtime/private
gates, or end-to-end throughput. The separate single-Sol reviewer experiment
must pass those bars before any production layer can be removed.
