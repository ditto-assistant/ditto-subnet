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

## Owner-link vs new hotkey

After attesting, do **not** tell the miner they must resubmit on another
hotkey. Attest binds two existing hotkeys. A new hotkey without a direct
link to the matched hotkey will copy-hold again.
