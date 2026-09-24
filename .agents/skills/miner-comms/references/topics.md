# Recurring miner questions

Facts for paste-ready replies. Investigate live numbers before sending.

## Copy hold

The detector compares **source content** (lexical, structural, prompt
fingerprints), not the hotkey and not renamed functions. Jaccard ~0.95+
against another miner's crate is a whole-agent match, not a starter-kit
false positive. Changing names or submitting from a new hotkey does not
clear it.

Owner-link (`ditto attest`) exempts plagiarism screening only between the
**two named hotkeys**. Links are direct and not transitive. Linking A→B
does not cover a match against C. Procedure:
[`docs/OWNER-LINKS.md`](../../../../docs/OWNER-LINKS.md).

Same-owner is not a pass if the banned `/run` path is still served.

## Ban per UUID

A reject attaches to one agent UUID. An older scored version on the same
hotkey can stay live. "This is the same code as v5" does not restore a
later UUID.

## Champion vs submit time

Crown is not first-to-submit. The KOTH fold uses composite, a 0.007 flat
margin, a statistical band, and paired comparison when two agents share at
least two confirmation seeds. Clear wins outside the band do not wait for
the tail to finish catch-up. See
[`services/dittobench-api/docs/seed-and-scoring.md`](../../../../services/dittobench-api/docs/seed-and-scoring.md).

Shares are 65 / 14 / 10 / 7 / 4 for champion plus four tail slots.

## "Seeds" on the public board

Quorum is **three validators** on the submission's own dataset seed. That
is not the "N seeds" badge.

The badge is **confirmation-lane depth**: champion-anchored shared seeds
from the top-five rescore lane. Baseline is three, then one new seed per
round, cap **32** (`TOP5_MAX_CONFIRMATION_SEEDS`). Minimum credible sample
is 8. **13 is not a cap** — it is current depth.

A new champion starts a new seed family, so the crown often shows 1 seed
while a tail that already sat in the set shows a larger count. That is the
lane working, not stuck seeding.

## "My newer version scores higher but the old one holds my slot"

One owner gets one emission slot, and the representative is chosen on the
**official continual composite** — the quorum median plus one score per
fold-eligible shared seed — not on the three-validator median alone. A
predecessor with a deep confirmation history can therefore out-rank a newer
generation whose canonical median is higher and whose shared-seed depth is
zero.

Investigate before replying: `get_continual_retest_diagnostic` on the exact
newer UUID returns both composites with their sample counts, the same-owner
representative and the margin that selected it, whether the UUID is in the raw
wave / folded emission set / resolved retest cohort, the cutoff and tie-band
comparison, and whether a validator can claim it now (`claim.decision`,
`claim.route_priority`).

`admission_reason: same_owner_challenger` means the newer generation is
admitted to the retest cohort for catch-up and is earning the shared seeds it
needs — it is not taking a second slot. A **negative** `cohort_cutoff.gap` on a
row that is still out of the cohort means the exclusion is owner suppression,
not a score it failed to reach. Do not promise a promotion or a retest ETA.

## Owner-link vs new hotkey

After attesting, do **not** tell the miner they must resubmit on another
hotkey. Attest binds two existing hotkeys. A new hotkey without a direct
link to the matched hotkey will copy-hold again.

## "Agents dodge open-program / hard families"

When a miner reports that top agents "aren't solving" or "hardcode not to
answer" a question family (open-program, reconcile twins), treat it as a
board lead, not a comms-only complaint. Investigate before replying:

- Grep the named agents' served source for a **scored-family decline
  gate**: a scripted exact decline (`Reply exactly: "I don't have that
  information"`) plus a do-not-attempt directive driven by a harness
  family/absence classifier. That shape fails the review bar (see
  `backroom-review/references/review-bar.md`, Class A) and earns its own
  ATH fire.
- Distinguish the legitimate shapes before promising anything: the bench
  includes genuinely unanswerable cases where a decline is the *correct*
  answer, and model-decided abstention after reading the records is
  allowed. A 0.333 slice or a queued LongMem lane is not by itself proof
  of dodging.

In the paste: acknowledge the report, say the pattern is reviewable and
that reviews attach per agent UUID, and do not name which agents are or
are not under review, promise a ban, or state a review timeline.
