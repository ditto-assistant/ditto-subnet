# Bench v14 generator-competition credits

Bench v14 datagen takes ideas, and some code, from DittoBench generator
competition submissions. The competition runs each generator's cases against
a panel of top agents. A case is **discriminating** when at least two
independent owners solve it but not every panel agent does, **trivial** when
every agent solves it, and **unsolved** otherwise
([generator-competition.md](generator-competition.md)).

This ledger has one row per shipped v14 improvement. Each row gives the
change, where it came from, the panel evidence and the PR. Contributors are
generator-competition miners credited by a stable letter (A-I), never by
hotkey, submission ID, generator name or source location. Generator A is the
public default starter kit.

| v14 change | Origin | Evidence | PR |
|---|---|---|---|
| Family admission gate (`internal/familygate`) and the excluded-shape ledger (`universe.V14ExcludedShapes`) | Generator competition, measured over rounds 25944-25963; no generator code ported | Shapes held as always trivial: the public default kit's update chain, two-hop, retraction and scattered list (generator A; 76/76 trivial over rounds 25946, 25955, 25963), plus generator G's needle and single-hop as-of columns (column aggregate, 59 of 63 trivial over rounds 25955 and 25963). Shapes held as unsolved: generator C's conditional (6 of 7 judged cases unsolved) and alias-reroute (5 of 5), and generator D's owner-alias-email (3 of 6). Generator D's revert-to-original was admitted (discriminating on 2-3 of 5 panel agents in rounds 25946 and 25963) | #3185 |
| Explanation of the generator competition's 0.5 per-case ceiling, and the `hold-half-credit` admission rule in `internal/familygate` (SN-352) | Generator competition (generator B, rounds 25944-25967). Analysis only: the two-item nickname list was not ported, because its hard half is almost never earned at full credit | All 27 top promotion candidates have panel mean 0.2 (2 of 5 agents, each exactly 0.5), and every trivial case in round 25955 has mean 0.5 with 5/5 solvers. Discriminating on 25/32 (round 25955) and 26/32 (round 25963) columns at the 0.5 threshold | #3194 |
| Supersession chains with reverts, declined proposals and entries logged in error, with seeded cue-free and misplaced-cue placement (SN-353, SN-354) | Chain kinds from generator D's revert family (round 25946) and the later SN-353 discriminating-chain evidence. The unnamed revert ("back to the original entry", no value restated) is ported and adapted from generator D. Cue placement and the chain engine are new to v14 | Generator D's revert-to-original was discriminating on 2/5 (round 25946) and 3/5 (round 25963) panel agents, and trivial in round 25955. Over 120 seeds, no surface shortcut solves both readings of more than 16% of v14 chained groups | #3193 |
