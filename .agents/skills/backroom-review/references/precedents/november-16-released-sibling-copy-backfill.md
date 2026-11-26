---
id: november-16-released-sibling-copy-backfill
resolution: clear
tags: [policy-v14, copy, near-duplicate, released-base, released-sibling, copy-court, family-diff-v3, dan0l, beast-d41, backfill]
holding: A near-duplicate copy hold that names an unreleased generation is a false positive when a released generation of the same owner, public before the upload, explains every shared file and none of the unreleased generation's distinctive lines appear. Clear it; the fork's own runtime delta must be non-empty. Releasing one version does not by itself clear forks of its unreleased siblings.
---

Copy-hold backfill, 2026-11-16, rulings batch from
`docs/copy-hold-backfill-2026-11-16.json` (11 clears, executed 19:56Z).

Nine holds named unreleased dan0l v57 `7042e3c8`, v58 `430a581c` or v61
`73014300`. Two holds named beast-d41 v10 `36c7cd9b` or v12 `08041a9e`,
which were themselves public before those uploads. In every case
`preview_copy_court_triage` (family-diff-v3) found:

- every shared file byte-identical to a base that was public before the upload.
  For the dan0l line that base was dan0l v59 `a06c97e5`, public by exact-SHA
  handoff 2026-11-15 17:32Z;
- 0 distinctive lines of the named unreleased generation in the held agent;
- a non-empty runtime delta (own files or edits), so this is not an unmodified
  fork (see [october-29-unmodified-fork-not-eligible](october-29-unmodified-fork-not-eligible.md));
- an unreleased-copy (uc3) top score of 1 or lower, against a threshold of 100.

Cleared: tern-mem v1/v2/v3 (UID 14), nickbridge v3/v4 (UID 238), will v9,
lh2mem v7, ditto1 v2/v3/v4/v5.

**Consistency answer (why dan0l v59 is public but these were held):** the
detector built its publication alibi from the owner-reduced ledger. Each owner
has one representative row there, here a later unreleased dan0l generation, so
released v59 was invisible and the hold fell back to the earliest unreleased
sibling. That is a detection bug, not a policy distinction. The clears rest on
the evidence above, not on v59 having been released.

**Kept held:** nanodevs v19 `e882c7ad`. The court recommended REJECT on 4
"distinctive" Hades v31 lines (`app/menu.py:97-98,126,129`). Those lines are a
Python port of the public upstream scorer
`services/dittobench-api/internal/scorer/catalog_semantic.go:45-48,108-130`,
so that ground is refuted. A separate uc3 overlap with unreleased beast-d41
v4–v8 (43 items, score 23.5) has no item detail readable through Backroom, so
it was left for a human.
