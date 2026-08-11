# DittoBench v9 score gates

This package is the pure policy core for issue #384. It accepts already-trusted,
case-level telemetry and produces internally consistent evidence for two v9
gates:

- model use: successful controlled inference cases divided by eligible cases;
- authoritative tool use: validator-observed matches divided by expected
  executions. Unexpected observed executions are published diagnostic evidence
  but do not change the factor without a separately calibrated contract.

Both ratios use integer basis points and floor division. A result at the
published threshold passes. A result below it has factor `0`; a passing or
not-applicable result has factor `10000`. A measured run with eligible cases and
no inference is explicitly `zero_inference` with factor `0`, so it remains a
completed, signable agent outcome rather than becoming retryable infrastructure
failure.

Aggregate broker request counts are not distinct-case evidence. They are
published separately as `request_coverage_bps`; when trusted distinct-case
attribution is unavailable, `case_attribution_complete` is false, semantic
`coverage_bps` is zero, and the result is `insufficient_evidence` with factor
zero. Repeated requests from one case therefore cannot satisfy a multi-case
gate.

## Trust boundary

The package does not collect or infer telemetry. The integration layer must:

1. set distinct successful-inference case counts only when the controlled
   broker/runner path can prove case attribution. The v9 scorer serializes
   ordinary cases and snapshots the source-bound ticket broker at both edges;
   any overlapping request makes attribution incomplete. Aggregate request
   totals alone must leave `CaseAttributionComplete` false;
2. exclude protocol preflight, ablation, undelivered, and validator-fault cases;
3. derive tool matches and unexpected executions only from validator-observed
   `tool_endpoint` transcript events; and
4. set `TelemetryComplete` only after the relevant trusted stores have been
   read successfully.

There is intentionally no harness `RunResponse.tool_calls` input. Self-report
cannot create or repair authoritative credit.

## Version and signing seams

`ApplyForVersion` returns pre-v9 scores without arithmetic, preserving their
floating-point bits, and rejects v9 evidence on those versions. V9 requires
validated evidence. The current factor is a binary run-level multiplier; the
individual gate factors remain separately published for future composition. A
signed `shadow` report publishes the same result and factor while leaving the
composite unchanged; `enforce` applies the factor.

`CanonicalBytes`, `Digest`, and `SignatureInput` define a domain-separated,
fixed-order representation. The report signer should bind `SignatureInput` to
the existing signed fields rather than placing the evidence only in unsigned
details. Integration should retain the golden digest test when changing this
schema.

The production v9 contract uses one-basis-point enforce thresholds: trusted
evidence must prove at least one eligible model-backed case and one matched
authoritative tool execution. This is a binary integrity floor, not a sampled
quality calibration; efficiency is versioned and ranked separately. Evidence
binds both profile ID and manifest SHA-256 so arbitrary numeric thresholds
cannot masquerade as the production contract.
