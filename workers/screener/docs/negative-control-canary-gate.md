# Held source-review negative controls

Four source-reviewed submissions from 2026-09-22 and 2026-09-23 remain held. They are regression leads, not admission decisions. Their exact UUID, artifact SHA-256, and attempt must be re-read from Backroom immediately before any canary; this document does not authorize a rescreen.

| Control | Observed false-positive lead | Current code boundary |
| --- | --- | --- |
| Endpoint-absent tool stub (two artifacts) | I6 fabricated trajectory from a local no-endpoint branch | V13 L2 and safety-adjudicator guidance requires proof on the endpoint-present scored path. |
| Inference URL and timeout | Static cross-user access from `http://host.docker.internal` and a `read_timeout` field | The legacy source signal masks remote URLs and requires an access call. |
| Local rehearsal script | Static data exfiltration from a script absent from the final image; the script downloads a public fixture and removes provider keys from a child environment | Static v2 records build/runtime reachability and causal flow. Its default mode remains `off`, so the legacy finding can still route to serial source review. |

The sanitized controls in `tests/fixtures/static-preflight-v2-regressions.json` assert only the structural boundaries above. They do not contain miner source or prove that any full submitted artifact is safe. Keep positive controls for a reachable external secret sink and a reachable cross-user file read passing at the same head.

A SHA-verified, report-only L4 replay may compare a held artifact against an independent source-review label without the private verifier bootstrap. It must not write to Backroom or imply a full policy-v13 clearance. A production rescreen needs the separate trusted replay runner, sealed package, and runtime/private verification receipts; configuring or testing L4 alone cannot provide them.

Before proposing a single exact-artifact rescreen canary:

1. Record the current main commit, screener image digest, worker adoption, effective static-preflight mode, policy manifest digest, and L2/L4 settings from authoritative runtime controls. Do not infer the mode from the repository default.
2. Pin the exact held UUID, artifact SHA-256, attempt, and finding digest from Backroom. Obtain an immutable source digest and a complete readable manifest; treat truncated manifests and opaque build inputs as unresolved.
3. Run the static v2 shadow comparison on the exact artifact and retain its bounded proof audit. A proposed `enforce` change requires separate review of the complete shadow corpus, including false negatives, unresolved legacy leads, and the positive controls. A test fixture pass alone does not activate it.
4. Verify the L4 packet/read and explicit-operator-request fixes are merged, deployed, and adopted by the worker. Demonstrate a valid, source-cited verdict on a local replay for each applicable failure class; timeouts, oversized responses, missing telemetry, and contract failures are not verdicts.
5. Verify the trusted replay runner, sealed package, and private/runtime receipt path on the exact candidate. Obtain an operator-approved, one-artifact canary plan with a stop condition. After that canary, re-read the new attempt, exact source finding, image/build receipt, and served-path evidence before considering another artifact or a miner decision.

Until those gates pass, keep the four submissions held. In particular, no verified image or scored receipt exists for these attempts, and a pre-build static lead cannot be treated as a complete served-path review.
