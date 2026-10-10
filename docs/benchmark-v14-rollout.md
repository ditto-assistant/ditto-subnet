# Bench v14 qualification and rollout

Status: compatibility implementation; **activation blocked pending qualification**.
This plan is part of #2508. It supersedes #2405's proposal to change scored v13
outputs. It grants no authority to deploy, rotate the v13 cohort, regrade scores,
alter emissions, or change a production setting.

## Immutable boundary

V14 changes the declarative memory over-call rule, folds the typographic
hyphens U+2010/U+2011 in graded reply text to ASCII (#2734), and changes
benchmark identity.
V13 keeps its whole-case exclusion. V2–v12 keep their existing rules. Lifecycle
write cases remain excluded at every version. V14 counts each observed
declarative-ack case and permits save/update/delete memory actions on that case;
unrelated actions count once even when mixed with permitted writes.

| Example, all cases observed | V13 factor | V14 factor |
| --- | --- | --- |
| Declarative save plus unrelated action on recall | 0.75 | 0.875 |
| Unrelated action on declarative case alone | 1 | 0.75 |
| Declarative save plus email on that same case | 1 | 0.75 |
| Declarative save, read, or no call alone | 1 | 1 |

These are over-call factors. Other existing composite gates still apply.
The v14 full/public vector at seed 123456789 is
`8a08dfe713fd6df2d67ece92148118a3fbd64b5d0f6a90f78dccb9853e329d50`,
with epoch `2027-06-01T00:00:00Z`. The surface, grader (apart from the hyphen fold), v13 gate postures,
LongMem instrument and public harness wire (9) carry forward. There is no private
surface expansion or screening policy v14 activation in this change.

## Record the release packet before enabling any v14 work

The compatibility PR cannot name an image that has not been released. Leave the
qualification blocked until the release owner supplies all of the following;
never substitute a floating tag, local build identity, or environment assertion:

- The reviewed merge source SHA (40 characters), semantic release, immutable
  scorer image digest and signed stack descriptor digest.
- Binary `version` output matching that source and release, advertising v14,
  with no source mismatch; the release workflow checks the exact supported set.
- Fresh signed managed-validator heartbeats from the intended cohort, each with
  the same descriptor, scorer image/source and `v14_scored_runtime_env` digest.
  This new field is separate from the frozen v13 `scored_runtime_env` packet.
- The unchanged active v13 cohort pin and its scorer image/source/descriptor,
  archived for historical replay and rollback. Keep those validators and images
  available; do not rotate their v13 pin to the v14 image. Hold automatic stack updates on those v13
  members before the compatibility release. Qualify v14 on a separate cohort
  while the pinned v13 cohort drains its version-bound tickets.
- Exact-head CI, review approval, accepted diagnostic canaries, shadow report and
  an independently reviewed activation decision with a ticket cutoff.

Source baseline for local historical replay:
`861814b58a38c0582e9fc73848566d1b6fe8c325`. The regression test pins 1,536
v2–v13 composite vectors. This is repository-source compatibility evidence;
the release owner must additionally replay against the **actual pinned v13
image/source**, which may differ from this baseline.

## Qualification gates

1. Run generator and scorer full Go suites and the grader release audit; verify
   all old golden vectors without regenerating them. Verify v14 generation
   determinism and the new Go/Python signed score fixture. Run Platform,
   validator, shared protocol, screener, Backroom, dashboard and starter-kit CI.
2. Deploy compatible Platform and shared-contract consumers before candidate
   validators send the new signed capability field. Register the reviewed v14
   image/source in the separate rollout qualification
   record. Keep the active version and all v13 pin/rotation rows unchanged.
   Platform requires deterministic typed-semantic support and v14 environment
   evidence before counting a validator as v14-capable. Existing managed stack,
   heartbeat freshness, inference readiness, isolation, canary and activation
   checks still apply; a supported-version advertisement is insufficient.
3. Run accepted isolated diagnostic canaries with clean known-benign and
   adversarial harnesses. Exercise persistence, no-call/read baselines,
   unrelated/mixed actions, non-declarative writes, LongMem and confirmation
   using the existing instrument profile. Capture accepted signed packets from
   the exact release, including dataset and transcript digests.
4. Replay a representative consented corpus into **separate** v14 shadow
   results. Preserve original accepted v13 ledger rows. Compare both versions
   on the same observed cases to isolate the scoring change, then sample v14's
   rotated seeds separately to measure generation variance. Record affected
   case counts, denominator changes, composite deltas, rank changes, honest
   controls and adversarial controls. No measured results are claimed here.
5. The operator reviews the shadow report, fleet capacity and rollback evidence
   before issuing any activation. Keep missing evidence as a blocker.

## Ticket boundary and rollback

Use the existing guarded benchmark rollout endpoints from Backroom. Record the
activation time/block, reviewed release packet and ticket cutoff. Existing v13
tickets finish under their ticket-bound v13 dataset and scorer; only newly
issued v14 tickets use the v14 source/image and signed evidence. Never change a
ticket's version or reinterpret a completed v13 score as v14. Confirmation may
reuse the installed LongMem instrument but carries subject version 14 through
bundle, execution, evidence, signature and ledger.

If readiness, canary or shadow checks fail, leave v14 inactive. After an
authorized activation, rollback stops new v14 issuance through the rollout
control, drains or explicitly expires outstanding v14 leases, and restores the
previous active version with its archived exact scorer packet. Keep accepted
v14 rows versioned and auditable; do not rewrite them or the v13 ledger. Do not
deploy an old binary to a validator still holding a v14 ticket.

## Version sweep

The shared evidence Literal derives Python validator, confirmation and receipt
versions. Go generator/scorer bounds, Rust `/run` acceptance, explicit rehearsal
ceiling, Backroom schemas, dashboard unions, generated OpenAPI/contracts and
release identity checks admit 14. Research current-version, rehearsal live
default and harness active/wire constants remain unchanged.

Database score/ticket versions already have no upper bound. The equality-to-13
constraints belong to private v13 screening, canary or scorer-cohort records;
those remain version-specific. No schema migration or production write is
required for this compatibility change.
