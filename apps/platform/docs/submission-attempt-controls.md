# Report-only paid submission comparisons

PR #2488 provides **operator reads only**. No paid upload or pre-payment check
runs this profiler. There are no attempt-policy, appeal, replay, enforcement,
reservation, or new persistence writes and no new migration. Existing paid
receipt recovery, reusable credits, fee quotes, CLI, cooldowns, copy checks,
source screening and scoring retain their existing contracts.

## Read the evidence

- `get_submission_attempt_policy` (Backroom read scope) returns classifier
  version, deployed source build, settings digest, current reference corpus and
  resource bounds.
- `get_submission_attempt` takes `agent_id` and optional `reference_agent_id`.
  Both must be accepted, paid artifacts. The default reference is the latest
  earlier paid submission in the proven payer scope. Supply an exact older
  predecessor for a repack investigation; this is a pair comparison, not an
  exhaustive owner lineage or a count of attempted admissions.
- Payer scope uses the verified payment coldkey and direct mutually coldkey-signed
  links that existed at the candidate's submission timestamp. Names, hotkey reuse,
  common funding, transitive links and later attestations cannot expand it.
- Reads verify stored SHA-256 and byte length before profiling. At most two
  archives are read (2 MiB each), with 8 MiB decompressed bytes and 512 members per
  profile, and at most 100 direct owner links. Unknown, changed, unsafe, oversized
  or missing evidence is inconclusive. Nothing is extracted or executed.
- Return classifications cover infrastructure retry, packaging-only repair,
  small source delta and material new work, plus first submission/inconclusive.
  Only authoritative infrastructure feedback completed before the candidate's
  submission can establish an infrastructure retry. Expiry alone is not proof
  of an infrastructure fault. Canonical ticket infrastructure failures count;
  diagnostic canaries, scoring errors and sandbox OOMs do not. Labels describe
  lexical/content differences, not semantic novelty or a calibrated admission
  decision. Source text, fingerprints and profiles remain
  internal and are not persisted or returned.
- `feedback_status` describes the reference's latest screening outcome as of the
  candidate's timestamp. A quarantined reference stays `pending` until a release
  or reject ruling recorded before that timestamp, which reports `completed`
  with the operator ruling code and time; a rescreen ruling stays `pending`.
  Failed, expired and unknown attempt states are never reported as completed.

## V13 authority

Every response is explicitly `report_only=true`, `admission_effect=none`,
`source_clearance=false`, and `integrity_clearance=false`. A material-work label,
packaging repair or infrastructure retry **cannot release a hold, issue a ticket,
clear source review, or satisfy V13 integrity/lease requirements**. Observational
similarity is never proof of original or benign source. The source/integrity
and V13 allocation producers remain authoritative.

## Deferred enforcement and calibration

Issue #2043 remains open. Admission enforcement and all policy/replay/appeal
write controls are excluded from this PR and require a separately reviewed
follow-up. The previous full implementation is preserved separately for later review.

Before an enforcement merge, provide hosted gates on its exact source head and
an independently labeled replay of actual paid candidate/reference IDs covering
all four classes. Pin classifier, settings digest, source build, reference corpus,
verified archives and feedback cutoff. Report false-throttle/false-allow rates,
classification errors and inconclusive coverage; separately measure queue/provider
load rather than equating deferrals with saved benchmark runs. Obtain operator
review of the calibration and an explicit V13 source/integrity authority check.
Synthetic fixtures and code tests establish behavior, not production calibration.
This PR supplies no calibration or enforcement approval. Merge/deploy and future
observation collection remain separate steps.
