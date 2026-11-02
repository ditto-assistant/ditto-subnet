# Isolated benchmark canary

Use `issue_benchmark_canary` to test one shipped, non-retired benchmark version
without starting a rollout. This is **not** `start_benchmark_rollout`: that
operation can automatically activate once its authority gates are met.

1. Read `get_benchmark_rollout_control`, `get_screening_submission` (source
   `artifact_sha256`), `agent_scoring_readiness` (`screened_image.sha256`), and
   `get_validator_fleet`. Readiness's ordinary `leaseable` flag is not canary
   eligibility: this operation intentionally targets already scored/live agents.
2. Choose an already scored/live, screened agent and one idle healthy validator
   slot supporting the requested version. Existing `(agent, version, validator)`
   ticket identities are refused, including terminal ones; choose another
   validator rather than replacing historical work.
3. Supply a fresh UUID `canaryId`, `agentId`, `benchVersion`, `validatorHotkey`,
   `slotId`, both `expectedArtifactSha256` and `expectedScreenedImageSha256`,
   `expectedActiveVersion`, a detailed `reason`, and exact confirmation
   `ISSUE CANARY V{benchVersion} {agentId}`. The authenticated Backroom staff
   identity becomes the audit actor. Only write-authorized staff can issue it.
4. Read `get_benchmark_canary` using that UUID. Transport retry with the same UUID
   and identical inputs is idempotent. Only one live canary is permitted across
   the fleet. It uses a full-profile normal signed validator job and the existing
   bounded inference grant and three-hour deadline; it does not automatically
   retry after failure or expiry.
5. Cancel using `cancel_benchmark_canary`, the UUID, reason, and exact
   `CANCEL CANARY {canaryId}` if needed. This revokes inference and closes the
   lease; it cannot forcibly terminate an already executing remote container.

Results are private diagnostic receipts, not `Score` or `ConfirmationScore`
records. They never contribute quorum, rollout activation, rankings, or miner
retry costs. The existing validator's best-effort public transcript upload has
no canonical score to attach to and is intentionally refused; no public
transcript is created by this operation. Backroom exposes a bounded report
summary, not raw scorer details. Seeds are decimal strings to preserve 63-bit
precision through JavaScript.

`completed` means a signed, version/seed/dataset-bound report was received. It
does not mean the scores are good, calibration passed, or activation is ready.
Review the result and confirm the active version and rollout authority remain
unchanged. Activation remains a separate explicitly authorized operation.

Deploy the Platform migration and application before using the Backroom tool.
Never issue a canary against mixed old/new Platform replicas. Old code does not
know the diagnostic lease lane. The database guard rejects authoritative score
writes while the ticket has canary purpose. Normal later leases may reuse an
expired ticket with a new deadline and a fresh ordinary attempt budget; the
separate canary receipt remains queryable.
