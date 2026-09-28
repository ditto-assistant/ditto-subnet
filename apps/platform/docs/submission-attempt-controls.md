# Delta-aware submission admission

Issue #2043 separates submission timing from misconduct review. The policy never
quarantines an agent, copies a predecessor's verdict, changes a score, or changes
emission ownership. Existing same-owner copy exemptions continue to apply.

## Collection and ownership

The default is **shadow**. The existing owner cooldown and fee remain effective
while Platform records source comparisons and proposed admission delays. **Off**
stops new comparisons and retains baseline admission. Existing payment recovery
and admission reservations retain their quoted terms.

The CLI sends the signed archive to `/api/v1/upload/check-artifact` before paying.
Platform checks its actual size, SHA, and archive safety without extraction or
execution. Old clients may use the JSON check in off/shadow mode. An enforcing
deployment requires artifact verification before payment (code 1107); the CLI
falls back to JSON only if the new route returns 404.

History is anchored to immutable paid submissions from the verified coldkey or
direct, active owner attestations signed by **both coldkeys**, bound to their
paid hotkey provenance on the deployment's subnet. Hotkey-only attestations are
insufficient to transfer historical attempt budgets between payers.
Names, apparent similarities, historic shared payers, an unsigned hotkey match,
and transitive links do not establish punitive ownership. Replay uses recorded
attestation validity at the candidate's submission time.

Archive metadata and file names do not change runtime identity. Runtime identity
hashes the multiset of raw file contents. Only the root Dockerfile and
`.dockerignore` belong to the packaging channel; prompts, manifests, scripts,
data, and binaries remain runtime inputs. Compatible reference-aware lexical
sketches identify small changes. Changed opaque binaries are inconclusive.
Unknown or incompatible profiles receive no delta penalty. Earlier submissions
without a profile remain inconclusive; this change does not fetch or relabel
production artifacts. Collect new shadow history before calibrating them.

## Proposed timing

Comparisons name exact predecessor and lineage UUIDs. Repeated small changes are
also compared with the lineage's original artifact so accumulated material work
can start a fresh review. The initial thresholds and budgets are shadow defaults,
not evidence that they are appropriate for production.

Only authoritative, completed feedback consumes low-information budget. A pending
evaluation or active pre-payment reservation uses tentative capacity after clear
feedback exists. Failed/expired screening and signed validator infrastructure or
scoring failures release that capacity. A subsequent infrastructure retry does
not consume budget. Two bounded fast retries are initially available after
allowlisted deterministic build/runtime failures; repeated failed repairs cannot
reset that allowance. Owner locks serialize admission across proven links.

Enforce mode replaces the baseline cooldown with this timing policy. After the
configured low-information budget is exhausted, further similar submissions
receive code 1106 and an appealable retry time. Material work receives its own
review. Frequency alone does not establish cheating.

## Calibration and operator controls

Backroom MCP exposes:

- `get_submission_attempt_policy`: configured/effective settings, revision audit,
  and any calibration compatibility block.
- `get_submission_attempt`: source-safe guidance and appeal audit for a paid UUID.
- `replay_submission_attempts`: persist independently reviewed labels for actual
  paid submission UUIDs, replayed against feedback available at each admission.
- `get_submission_attempt_calibration`: retrieve the report and audit actor.
- `set_submission_attempt_policy`: append a revision with exact confirmation.
- `appeal_submission_attempt`: permit one following accepted retry for an exact
  predecessor and policy revision; no release of review holds or fee waiver.

Reads require `backroom:read`; all mutations require `backroom:write` and carry
the signed-in operator's email. Platform also requires its admin bearer token.
These tools return no source, fingerprints, private diagnostics, or similarity
ratios. Appeals are reserved with an admission quote and consumed by its paid
comparison record; quoted payment recovery remains idempotent.

Replay reports observed false throttles, false allows, classification mismatches,
inconclusive cases, class coverage, and immediate admissions deferred. It uses
the recorded quote-comparison time, rather than a later paid-upload time.
Equal timestamps and unreconstructable temporary capacity/feedback are
inconclusive and block activation; they are never presented as a measured saving.
A deferral
does **not** prove a benchmark run was saved; measure queue/provider compute
separately during rollout. Include independently verified legitimate repairs and
material iterations, fault retries, and repeated low-information sequences.

Enforcement requires at least eight distinct labeled paid cases covering all four
retry/change classes, some proposed deferrals, and zero observed false throttles,
false allows, mismatches, or inconclusive results. Collection and replay settings
must match. This is a minimum activation guard; operators must choose a
representative dataset and review its limitations. Changing tuning requires new
shadow collection under that tuning before calibration.

Apply an eligible report within seven days, with its calibration UUID, expected
revision, reason, and `SET SUBMISSION ATTEMPT MODE ENFORCE`. The calibration digest
binds tuning, classifier version, and reference corpus. A later incompatible
build automatically uses shadow mode and exposes the block. Roll back by applying
off or shadow with the matching exact mode confirmation. No production policy
change is included in this implementation.
