# V13 protected replay provisioning handoff

Status: **preparation only**. The replay and statistics PR stack is draft and
report-only. This document does not authorize a production write, a rescreen,
or a V13 CLEAR/REJECT. Keep the source-only terminal fence and Backroom
`adjudicator_mode=off` until independent verification and labeled canaries are
reviewed.

## Observed boundary (2026-09-24)

- The public tree contains the bank protocol and digest-only Platform registry,
  but no protected blueprint bank, approved control fixture, or concrete
  `ProtectedBlueprintBank` / `SealedPackagePublisher` production adapter.
- A path-name search under `/Users/peyton/code/ditto` found no local V13 bank or
  known-benign fixture. The Backroom MCP can read a generation group by ID,
  but offers no group or approval inventory. These checks do not prove that a
  protected store or approval is absent elsewhere.
- The five draft PRs are #2235, #2236, #2238, #2243, and #2244. Their code is
  not a deployed production provisioning path. The current Backroom setting
  read returned global revision 122 with `adjudicator_mode=off`.

## Smallest operator input

Provide **handles and attestations only**, never case bytes or an API key in a
chat, PR, log, or public repository:

1. The owner and non-secret identifier of a protected, reviewed V13 blueprint
   bank, and the private runner's intended **read-only mount path**. Identify
   the independent reviewers and the digest of their semantic review record.
2. One proposed known-benign control's exact `agent_id`, V13 `attempt_id`,
   artifact SHA-256, verified image SHA-256, and review-evidence SHA-256.
   Identify the reviewer who attests that this exact image is benign under V13.
3. One exact **quarantined** target's `agent_id`, `attempt_id`, artifact
   SHA-256, and a source-reviewed expected label, recorded **before** its private run. The
   first run remains report-only whether the predeclared label is CLEAR or
   REJECT. A later opposite-label canary is required before considering the
   terminal gate.

The bank owner must author and keep cases in a protected boundary outside the
public repository. A separate reviewer must check each control/variant pair
preserves the public schema, entity relationships, units, chronology,
authorization, and tool behavior. Each pair needs a distinct UUID and payload
bytes (1 to 128 KiB per side). The bank needs at least **20 pairs per required
class** so two disjoint hidden seeds can each select ten: field/entity rename,
request paraphrase, and record reorder/decoy. If a tool catalog applies, it
also needs 20 catalog reorder/alias pairs. The final sealed package therefore
has at least 60, or 80 with a tool catalog, distinct pairs. An independent
reviewer must attest the transformed answer/tool expectation for each pair;
syntactic difference alone does not prove semantic equivalence.

## Provisioning path to finish before a live canary

1. Implement the protected bank loader and sealed publisher behind
   `v13_private_provision.py`, plus a paired coordinator that generates **one**
   hidden inventory for both roles. Calling the current single-role provisioner
   twice would select different random seeds and fail the matched-inventory
   guard. Store digest-addressed bytes under a
   runner-only read-only view with `manifests/<sha256>` and
   `payloads/<sha256>`; retain hidden seed bytes separately. The runner
   checks that its bank root is a real read-only mount, not merely a directory
   with restrictive permissions. The private store must not be exposed through
   Backroom or public Platform APIs.
2. Expose guarded operator writes through the Backroom control plane. No
   secret-safe production setup command or Backroom UI path exists yet. The
   draft Platform admin contracts are
   `/api/v1/admin/v13-private-generation/known-benign-approvals`,
   `/api/v1/admin/v13-private-generation/replays/{replay_id}/group`, and
   `/api/v1/admin/v13-private-generation/replays/{replay_id}/packages/{role}`.
   They currently have no complete Backroom provisioning workflow. Add
   `backroom:read` views for replay claim, group/package digests, signed
   private receipt, statistics, and metered cost before operating the canary;
   the available Backroom group read requires a known group ID. Do not
   substitute raw production admin calls or DB writes. The approval request
   contains the control IDs and artifact/image/profile/review-evidence digests
   plus a reason. An authenticated, append-only approval must predate the
   generation group and both hidden seeds.
3. Create one exact replay for the held target, claim it with an enrolled
   worker whose hotkey differs from the source screener, and verify the replay
   image archive SHA-256, byte size, Docker config image ID, and active lease.
   Only then commit the replay generation group binding target, control,
   approval, and profile digest. Generate the two hidden rotations after that
   group timestamp. Register target and control packages with their distinct
   role receipts and the **same ordered pair inventory digest**. The Platform
   rejects stale identities, mismatched roles, expired leases, and conflicting
   registrations.
4. Mount the sealed store read-only on the independent runner. Put the provider
   credential in a file readable by that runner only (mode `0600` or stricter);
   pass its path through `--provider-key-file`. The runner refuses symlinks or
   group/world permissions. The exact report-only CLI is:

   ```bash
   python3 workers/screener/scripts/run_v13_private_replay.py \
     --replay-id "$REPLAY_ID" --group-id "$GROUP_ID" \
     --bank-root "$BANK_ROOT" --provider-key-file "$PROVIDER_KEY_FILE"
   ```

   This command is **not runnable today** without the protected bank,
   registered packages, enrolled independent lease, deployed stack, and
   budget guard. It prints only the replay ID, receipt digest, status, and
   `policy_verification_complete=false`.

## One labeled report-only canary

1. Before any paid call, record the expected label, exact target/control
   identities, independent reviewers, bank-review digest, profile SHA-256,
   expected case count, and a **proposed $5 maximum total provider spend for
   this one canary**. Use a dedicated credential with a verified external $5 hard cap
   or add an enforced in-run dollar cap first. The current private relay caps
   requests per fresh case but has no aggregate dollar cap; the existing
   source-review `$5` setting does not govern this runner. A 60-pair package
   opens 240 fresh case sessions across two sides and two images (320 for an
   80-pair catalog package), so its per-case request cap is not a total spend
   cap. If the dollar cap cannot be verified, do not start the run. Exhaustion
   is inconclusive.
2. Confirm current Backroom `adjudicator_mode=off`, the source-only terminal
   fence, one exact quarantined target, control still SCORED/LIVE, and an
   independent replay worker. Confirm the read-only mount without printing
   case contents and the credential mode without reading or logging the key.
3. Run one group with the command above. Each target and control side runs
   through fresh container/network/broker state, with a settled attribution
   ledger. Any missing case, failed broker attribution, timeout, mismatched
   image, changed lease, or incomplete teardown yields no pass.
4. Read back the append-only receipt and the admin private-statistics report.
   Recheck receipt signature, exact replay/attempt/artifact/image/group,
   matched inventory, source binding, per-seed counts, clean-control result,
   and actual metered spend against the predeclared cap. The conservative
   statistical result is a **proposal**, always `terminal_eligible=false` and
   `policy_verification_complete=false`; a signal is not a policy violation,
   and an inconclusive result is not CLEAR.
5. Compare the report with the predeclared label and have an independent
   reviewer inspect both private semantics and the separate V13 mechanical and
   runtime checks. Preserve only digest/aggregate evidence in the public
   audit. Do not enable adjudicator or rescreen. Repeat with the opposite
   predeclared label only after reviewing the first canary and its cost.

## Stop, rollback, and no-op checks

- Before the first call and after the canary, reread Backroom review settings:
  `adjudicator_mode=off` and ordinary source-review settings unchanged.
- Reread the exact target's hold/status and score count. A replay, registration,
  private receipt, or statistics GET must not change the hold, rank, emission,
  or miner-facing decision. If any changes, stop and investigate.
- On lease/budget/identity failure, stop the runner; let the claim expire or
  finish it as failed with a specific nonterminal reason. Do not recycle a
  manifest, seed, package, or receipt for a different target or retry. Do not
  delete append-only evidence to hide a failed canary.
- If a setting or worker rollout changes unexpectedly, restore the last
  recorded `adjudicator_mode=off` posture through a guarded Backroom settings
  update, then verify worker adoption and the target remains held. This is a
  contingency, not a step in the normal canary.

The terminal gate stays closed until the private bank/control are independently
reviewed, the provisioning workflow and aggregate spend guard exist, the
other V13 checks are independently verified, and both CLEAR and REJECT labeled
canaries match their predeclared expectations under the same reviewed method.
