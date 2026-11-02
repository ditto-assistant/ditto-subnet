# Bench v14 qualification and rollout

Status: **activated 2026-10-28 01:28:21 UTC** (see *Activation record*).
Before that: compatibility implementation, activation blocked pending
qualification. This plan is part of #2508. It supersedes #2405's proposal to change scored v13
outputs. It grants no authority to deploy, rotate the v13 cohort, regrade scores,
alter emissions, or change a production setting.

## Activation record

V14 is the active scoring contract. Backroom `get_benchmark_rollout_control`
reads `active_version` 14, `desired_version` 14, `status` `activated`.

- **Start.** `start_benchmark_rollout` (desired 14) succeeded at
  2026-10-28 00:30:26 UTC on Platform v0.400.2. The first attempt, at
  2026-10-27 23:40 UTC, returned a 500: the cohort-freeze statement hit its
  statement timeout. #3232 (released in v0.400.2) narrowed the freeze to the
  ranking columns, and the retry succeeded.
- **Activation.** The rollout earned desired authority at
  2026-10-28 01:28:21 UTC, with 12 ranked quorum agents (minimum 5) and the
  priority cohort complete (5 of 5 positions at 3 distinct-validator scores).
  At activation 12 of the 15 cohort members held a quorum and 5 validators
  were v14-capable.
- **Operator-delegated waivers.** Two qualification requirements were waived
  under operator delegation, not closed with evidence:
  - *No family hold-out.* Gate 4 closed as MEDIUM with no family held out of
    v14 generation (#3231; see *Gate 4 (family admission): measured from
    executed canaries*). The familygate tuning follow-up targets v15.
  - *No separate v14 cohort.* V14 activated on the shared validator cohort
    that also serves v13 (blocker 2 below). No separate v14 cohort was
    stood up.
- **Generator rounds.** At 01:51 UTC `preview_generator_round` froze bench 14
  for the next round (epoch 25988), with no reason, `capable_validators`
  {13: 4, 14: 5} against `min_round_validators` 3, and
  `scorer_grading_versions` [13, 14]. Rounds now open at v14.
- **Miner rehearsal.** The offline fallback `LIVE_SCORING_BENCH_VERSION` and
  the `mine` skill's live bench moved to 14 in the post-activation PR. The
  starter kit change ships the next staged provenance manifest (v16).

The activation block number was not captured in this record.

## Immutable boundary

V14 changes the declarative memory over-call rule, one claim-set grading
correction, folds the typographic hyphens U+2010/U+2011 in graded reply text to
ASCII (#2734) with matching claim-gate forms and twin/result-usage reads, adds
three memory matcher corrections from #2734 (a hyphen joining the words of a
known multi-word value reads as a space; set removal may end in a short place
phrase such as `from the list`; action claims accept the verb's -ing form), and
changes benchmark identity. It also reads every graded reply as rendered text
(see *V14 answer-normalization contract* below).
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

The grading correction: in a typed claim set, a sentence that is wholly one of
the case's own claim values is no longer read as a self-correction just because
that value is also a correction cue. "better safe than sorry" is published as
`safe; sorry`, and v13 discards `safe` because the next sentence starts with
`sorry`. V14 credits both. A sentence that carries anything beyond the value
("Sorry, I mean safe.") is still a correction, and v13 grading is unchanged.
The v14 full/public vector at seed 123456789 is
`760d155a9f192b635d3f5a19ef58ebba211576dd9ae2bccf5ae89135ff8492d9`,
with epoch `2027-06-01T00:00:00Z`. The grader (apart from the claim-set
correction above, the #2734 hyphen fold and matchers, and the answer-normalization
contract below), v13 gate postures,
LongMem instrument and public harness wire (9) carry forward. The surface carries
forward apart from case-file records, which v14 files in story order instead of
shuffled-presentation order (#3088), the supersession chains of SN-353/354, and
the generator-competition families below (SN-361); each moves the v14 vector and
leaves every v13 seed untouched. There is no private
surface expansion or screening policy v14 activation in this change.

## Anti-template work remains unqualified

The October 26 scope in
[ditto-subnet#1832](https://github.com/ditto-assistant/ditto-subnet/issues/1832)
adds resistance to public-generator template matching to the intended v14
launch. The compatibility implementation above does not deliver that work.
The public phrase banks still carry forward; changing the seed, grading
normalization or case-file dates does not establish generalization.

See [the implementation and qualification handoff](benchmark-v14-template-resistance.md)
for the retained experiments, missing version boundaries and required controls.
Keep anti-template qualification incomplete until those results exist. A
Backroom `start_ready` result checks mechanical rollout prerequisites; it is
not that qualification or operator approval. This document does not change a
production guard or authorize a rollout.

## V14 answer-normalization contract

V14 grades the reply a user would read, so a harness needs no final-writer
typography pass to be scored. Top v13 agents strip paired `**` and fold U+2011
before returning (`typography.py`/`output.cpp` in the dan0l line, released
agent `82172ff9`), which operators accepted as W8 typography (SN-342). From v14
the grader does that itself, in the memory grader and in every scorer path that
reads reply text (`grade.ReplyAsRead`: text-graded tool cases, result-usage
needles, twin post-pass answers). V13 and older grade byte-identically.

- **Rendered markdown** (`grade.PlainReplyTextV14`). Paired `*`/`**`/`***`/`_`/
  `__` emphasis (word-bounded, so `snake_case` and `2*3*4` stay text), inline
  code and code-fence lines, ATX headings, blockquote markers, bullets and task
  boxes, table outer pipes and delimiter rows, thematic breaks, and link or image
  syntax (`[text](url)` reads as `text`) are removed; the characters they mark up
  are kept. Ordered-list ordinals are removed only for a real numbered list
  (two or more items numbered 1, 2, ...); a lone `5. That's it.` keeps its
  number. Strikethrough is kept, so `~~Oslo~~ Lisbon` still names the
  distractor. Non-ASCII spaces read as a space, zero-width characters are
  dropped, U+2212 reads as `-`.
- **Sentence punctuation ends a number.** A `.` or `,` that no digit follows
  ends a number token: `Your PIN is 4471, as saved` names `4471`, and the same
  rule catches a forbidden, dump-guard or decoy number followed by punctuation.
  A decimal or grouping separator (`4471.5`, `4471,500`), a sign, or another
  digit still attaches.
- **Percent claims.** A quantity claim in `percent` or `percentage points`
  reads `15%` as `15 percent`. A count claim still ignores percentages.

Every other v13 guard is unchanged: distractor and hedge detection (two or more
distinct asserted values), cited-and-rejected excusal, the stuffing quantifier,
slot-in-prose, the forbidden-value and dump guards, and number-token boundaries
against neighbouring digits and signs. Audit bank `v14-2` adds reviewed
positives for each rule and negatives for a bolded distractor, a markdown
hedge, strikethrough, a decimal tail, a comma hedge, a punctuated forbidden
value and a percent distractor. No generation vector moves; only v14 grading.

Deliberately not relaxed: number words beyond the existing lexicon, abbreviated
amounts (`$1.2k`), numeric slash dates (`03/04/2026` is ambiguous between
locales), unicode en/em dashes inside identifiers (they separate clauses), and
the v13-pinned effect reads (`grade.MemoryEffectV13`), which receive the
rendered reply but keep the v13 number boundary.
## Generator-competition role families (SN-359, SN-360)

Both families come directly from the generator competition; see
`research/dittobench-datagen/docs/v14-generator-credits.md`. V14 generation
adds them as business-program families and rotates them through the role slot
of the business cycle (positions 3, 7 and 8). Positions 0–2 and 4–6 keep their
v13 families up to the temporal-slot draw of positions 2, 4 and 5 (SN-355,
SN-358), and v13 never reaches positions 7 or 8, so every v13 seed is
byte-identical. On the full profile the fourth business group is
client-vs-vendor, holder-vs-actor or identity-join by a per-seed draw. The
grader is unchanged: both families use existing person and value claims.

- **SN-360, holder-vs-actor.** Custody records name the doer of a step and the
  resulting holder in one sentence with no role label ("A signed the release
  and passed the keys to B"), over a one- or two-hop chain whose second hop
  names the item only by its bare noun (capped at two: 3+ hop generator
  analogs sat at 0/5). The question asks who holds the item now (a holder
  fold) or who performed a named step. The actor, or the recipient for step
  questions, is the distractor. The counterfactual changes one handoff
  recipient or one step's actor. Contributed by generator-competition miners
  (generators C and D, rounds 25955-25963).
- **SN-359, identity-join.** A directory record binds each person to one
  synthetic handle (`example.invalid` address or chat handle), stated once.
  The owner records use one side of the binding only, so the answer never
  appears in the governing record: handles in the records with the name
  asked, or names (or initial and surname) with the contact asked. Distractors
  are the previous owner and a near-miss handle owned by another person (never
  edit distance 1). The counterfactual rebinds one directory entry. This is
  the riskiest shape (generator analogs often hit 0/5) and still needs the
  SN-351 admission gate before activation. Contributed by
  generator-competition miners (generators C and D, rounds 25955-25963; generator J, round 25946).

Both families move the v14 vector. Any v14 qualification evidence recorded
against 8f1005957 (v0.391.0) or an earlier v14 vector is stale and must be
re-run on the new vector.

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

## Qualification evidence (October 27)

Recorded on `main` after every v14 generation change landed (SN-351 to SN-360,
answer normalization #3189, weight-fold fix #3184). Nothing here activates v14
or changes a production setting.

**Gate 1 (source).** At `0b46da26f` (v0.397.0), the scorer source every online
validator runs:

- `go test -count=1 ./...` in `research/dittobench-datagen`: every package
  passes. `genrun` `TestLocalEvaluate` hit its 3 s generator timeout once while
  both suites shared the host, and passed alone.
- `go test -count=1 ./...` in `services/dittobench-api`, run in a
  `golang:1.27.3` Linux container because the coding packages are Linux-only:
  all 75 packages pass. `cmd/updater-bootstrap` read-only-tree tests fail only
  as container root and pass as uid 1000.
- `go run ./cmd/graderaudit -release-gate`: clean for v9 to v14, with v14 on
  audit bank `v14-2`.
- Old golden vectors verified without regenerating them: `TestV14KnownVector`,
  the v13 vector docs test and the scorer's v2 to v13 historical replay
  (`internal/scorer/v14_historical_replay_test.go`) pass. `cmd/generate -sha`
  reproduces the v13 vector
  `59bd8592ff8b820b2e0bb49ceb992940c9a45a2935a47ba8062b80707f85766a`.
- v14 determinism: two `cmd/generate -bench-version 14 -seed 123456789
  -run-size full -sha` runs both print
  `760d155a9f192b635d3f5a19ef58ebba211576dd9ae2bccf5ae89135ff8492d9`, the
  vector above.

**Public adversary.** #3210 adds the public wire parser for the holder-vs-actor
and identity-join role families. Before it, the 40-seed GIH diagnostic failed
`-require-public-control` (programs 0.8313 < 0.90), because the adversary had
no grammar for those families, not because they resist it. With it, the wire-only
program count is 398/400 and every v14 business family is at 100%.

| Source | Quality | Memory | Tools | Public control | Report SHA-256 |
| --- | --- | --- | --- | --- | --- |
| `8f1005957` (stale vector) | 0.9501 | 0.9242 | 0.9760 | pass | `6243cd79...363d3` |
| `0b46da26f` | 0.9445 | 0.9129 | 0.9760 | fail (programs) | `8fbd48c5...a57a3` |
| `7852c80c3` (#3210) | 0.9498 | 0.9237 | 0.9760 | pass | `cb59080a...58574` |

Slice means at `7852c80c3`: story 0.9681, programs 1.0000, personal 0.9590,
quantity 0.9274, tool prompts 0.9760, business 0.5455 (22 cases), integrity
0.3205 (560 cases). No family is unclassified. The full SHAs are in the #1832
comment. The public v14 candidate is still inverted at about its earlier
strength, so anti-template qualification stays open.

**Gate 2 (fleet capability).** The Backroom rollout control reports v14
`start_ready=true` with 4 capable validators. Platform counts a validator as
v14-capable only with a fresh verified scorer, deterministic v13 datasets and a
`v14_scored_runtime_env` packet. All four online validators run the signed
v0.397.0 stack (descriptor
`sha256:8f2af963cd0abec114792dafa2eb3617ba8426cf7be85f141665c9c2947aa0f5`,
scorer image
`sha256:33ec9c72fa0c037d3b30c1db2703e32464c78083a6423e223b99af6bfba009c6`,
source `0b46da26f`). They are the same validators that serve v13; no separate
v14 cohort exists. `start_ready` is a mechanical check, not qualification.

**Gate 3 (diagnostic canaries).** Isolated, non-authoritative full-profile v14
canaries on the exact scored artifact and screened image of each agent in the
current hybrid top five, all on validator `5HT9U1J8...` (the first three on
scorer `0b46da26f`, the last two on v0.398.0 `b96737ea7`, which changes no
scored bytes). A canary's seed is derived from the agent's seed block, the
agent and the validator, never the bench version, so each v14 canary ran on
the same seed as that validator's own canonical v13 score. The v13 column is
that score, not the 3-validator mean. Every v14 dataset digest below was
reproduced offline with `cmd/generate -bench-version 14 -seed <seed> -run-size
full`.

The same seed is only partly the same test. Regenerating v13 and v14 at these
five seeds, 195 to 212 of 250 memory cases (78% to 85%) keep the same
question and expected answer, while the histories, epoch dates and 93% to 98%
of tool prompts change (case-file story order #3088, the SN-353 to SN-360
families, the v14 epoch). The delta therefore mixes the grading change with
the new cases, but much less seed variance than the earlier "different seeds"
reading assumed.

| Canary | Agent (v13 raw rank) | v13 composite (same seed) | v14 composite | Delta | v13 memory | v14 memory | v13 tools | v14 tools | v14 inference |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `b91b551d` | 1 | 0.9308 | 0.8850 | -0.046 | 0.9040 | 0.8240 | 0.9700 | 0.9500 | $1.09 |
| `f7884ad9` | 2 | 0.9164 | 0.8459 | -0.071 | 0.9120 | 0.8860 | 0.9413 | 0.9480 | $1.09 |
| `11d45359` | 3 | 0.9043 | 0.8597 | -0.045 | 0.8527 | 0.8653 | 0.9600 | 0.9267 | $1.09 |
| `0e84f29a` | top five | 0.9064 | 0.7957 | -0.111 | 0.8567 | 0.7907 | 0.9600 | 0.9700 | $0.34 |
| `1bcc87ee` | top five | 0.9231 | 0.8439 | -0.079 | 0.8847 | 0.8440 | 0.9700 | 0.9287 | $1.10 |

All five are lower at v14 (mean -0.070). The leader stays first on both
versions; the other four reorder (on this validator's seeds the v13 fifth
rises to second and the v13 second falls to fourth). The memory axis carries most of
the loss; tools move by at most 0.041. The v13 `memory_over_call` factors on
these agents' accepted scores are 0.993 to 1.0, which suggests the v14
over-call rule is a small part of the loss and that the new cases and the
rendered-text grading carry the rest. The fourth agent's 0.111 drop
is more than twice the v13 cross-validator spread for that agent (0.853 to
0.914). These are five single-seed runs, so they bound the rank impact but do
not measure it: gate 5 must. Platform keeps each canary's per-case report
(`benchmark_canaries.result.per_case`). Gate 4 below splits it by family.

Each run completed with signed evidence and no scorer error. These are clean
runs of honest, top-ranked agents. Known-benign and adversarial control
harnesses, the persistence and over-call probes, and LongMem confirmation at
subject version 14 have not been exercised:

- *Control harnesses.* `issue_benchmark_canary` accepts only a `scored` or
  `live` agent row with a verified screened image. The canonical starter
  fixture (`get_v13_official_controls`) is a screener control identity, not an
  agent row, and no adversarial harness is registered as one. Running either
  needs a control agent row (an upload, or a Platform path that admits an
  official control image as a canary target) or the local phase-1 stack.
  The local stack does not build on macOS (the coding packages are
  Linux-only), so run it in a Linux container.
- *Probes.* The persistence, no-call, unrelated, mixed-action and
  non-declarative-write behaviours are pinned at unit level
  (`internal/scorer/v14_overcall_test.go`, `TestV14DeclarativeOverCallContract`,
  `TestV14DenominatorAndHistoricalScoring`), not by an executed harness.
- *LongMem at subject version 14.* Confirmation bundles are selected from the
  live leaderboard and a retest keeps its source bundle's subject version, so
  no v14-subject bundle can exist before v14 canonical scores do, which needs
  `start_benchmark_rollout`. This item cannot precede the start. Either accept
  it as a during-rollout check (confirmation stays `shadow`, so it cannot move
  weights) or waive it.

Gate 3 is therefore partly met.

**Gate 4 (family admission).** `universe.V14FamiliesAwaitingPanel` now returns
14 families. The SN-359 and SN-360 role families were missing from
`universe.V14FamilyMix`, because the coverage test only enumerated the v13
cycle. They are now listed as pending panel. No `familygate` table exists for
any of the 14. Generator rounds (70 cases per round since 25969) grade miner
packs at bench 13 under the pack-v1 contract. Their cases carry no v14 family
label: the public round view gives per-column aggregates only, and Backroom's
promotion candidates give case and column ids. No round contains v14 benchmark
cases. The doc's other two sources are an operator column of v14 datagen cases
in live rounds, which is a production setting change, and an offline panel of
at least 2 independently owned agent harnesses, which means running miner code
with paid inference. Neither was authorized here. The only offline per-family
evidence is the public parser above: each of the seven business families
awaiting the panel is solved 100% wire-only (seeds 1 to 10). That shows a
template matcher can invert them; it is not the trivial/unsolved panel the gate
requires.

**Can the v14 pack contract (#3212) supply gate 4 before activation?** No,
for three independent reasons, checked against `main` and live Backroom state
on October 27:

1. *Rounds cannot open at v14 yet.* `choose_round_bench_version` only
   considers round contracts at or below the active version, so every round
   opened at v13 while v13 was active, and a column can only declare a policy at
   or below its round. A v14 pack column needs v14 active first.
2. *The default generator holds no column.* `select_columns` gives the public
   default only counted columns that miners leave empty. Fifteen miner
   generators are qualified and fill all five counted columns plus the
   challenger column (`preview_generator_round`). Making room for it means
   changing `counted_columns` or `challenger_columns`, which reshapes the
   generator track's paid columns (the generator share is 30% of
   non-burn emission).
3. *Wrong cases and no labels.* Starter kit version 4 emits its own
   competition families, not the `universe.V14FamilyMix` program families,
   and a round's per-case verdicts carry no family label.

So re-registering the default generator from kit version 4 is still right
(it keeps the default ready for v14 rounds), but it produces no gate 4 rows.
The operator's choices are unchanged:

- *Operator column (new code plus a setting).* An uncounted operator column
  whose pack is v14 datagen program cases with a family label on each case,
  outside generator scoring. Because of point 1, before activation it would
  have to run in v13 rounds with v14 cases graded at policy 13. That measures
  family difficulty but not the v14 grading. Cost: about one sixth more panel
  inference per round. Blast radius: adds no paid column, but changes
  round composition and needs a Platform change before any setting.
- *Offline panel (spend).* Run at least two independently owned harnesses
  (the top five are five owners) on the same v14 seeds and join per-case
  results to `V14FamilyMix` labels. One full-profile v14 run cost $0.34 to
  $1.09 of locked-model inference in the canaries above. About 3 to 4
  program cases per family per full seed means roughly 10 seeds for 3+
  rounds of 30+ judged cases per family: about 5 x 10 x $1.09, or $55,
  plus a Linux host. The per-case-to-family join tool does not exist yet.
- *Waiver.* Waive gate 4 for the structural families (SN-353 to SN-358 and
  SN-360). Identity-join (SN-359) should not ship on a waiver. Without a
  panel it would need a code change that holds it out of v14 generation,
  which moves the v14 vector and reopens gate 1 and the GIH diagnostic.

**Gate 4 (family admission): measured from executed canaries, closed as
MEDIUM (October 27).** Rounds cannot supply the table before activation (see
above), so the evidence comes from isolated v14 canaries. A canary's seed
derives from the agent and validator, so no two agents share a case and
familygate's per-case panel verdict cannot be formed directly.
`cmd/familypanel` (#3228) fills that gap:

- it regenerates each run's dataset from its seed and checks the digest;
- it labels every program case with its v14 family: the business group's
  `V13FamilyOfVersion`, or the personal-life domain in the case provenance;
- it folds each owner's per-family solve rate p_i;
- it bounds the panel verdicts over every possible coupling of case
  difficulty across agents: trivial is at most `min p_i`, unsolved is at most
  the U-capacity bound;
- it runs those worst cases through familygate's own thresholds
  (`familygate.Judge`), adding a five-owner, eight-case-per-owner panel floor.

Evidence: 9 completed full-profile v14 canaries.
- 5 are the gate 3 runs above. 4 more were chosen with `familypanel -plan` to
  cover the rare role and temporal families.
- 6 independently owned agents: the hybrid top five plus the rank-7 agent,
  which joined as a fifth owner for identity-join and latest-event (one
  top-five agent has no identity-join seed on any validator). Owners were
  checked by payment coldkey; a same-owner agent was excluded.
- Every dataset regenerated to its production digest, and every program case
  was labelled and scored.
- One more canary failed on the lease collision fixed in #3230. One was
  cancelled unstarted.
- Spend: $4.56 of locked-model inference for the gate 4 runs, $9.96 for all
  v14 canaries.

The baseline is the same tool run on the same six agents' accepted live v13
scores (18 runs).

| Family | Owners | Seeds | Cases | v14 solved | Trivial max | Unsolved max | Unsolved (indep.) | v14 verdict | Live v13, same agents |
| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- |
| appointments-medication | 4 | 7 | 28 | 79% | 0.38 | 0.00 | 0.00 | hold-thin-panel | 98% (hold-trivial) |
| client-vs-vendor | 2 | 2 | 8 | 100% | 1.00 | 0.00 | 0.00 | hold-trivial | 83% (hold-trivial) |
| current-channel | 3 | 4 | 16 | 94% | 0.88 | 0.00 | 0.00 | hold-trivial | new in v14 |
| family-milestones | 6 | 8 | 32 | 56% | 0.25 | 0.25 | 0.02 | hold-thin-panel | 81% (admit) |
| hobbies-clubs | 6 | 8 | 32 | 81% | 0.25 | 0.00 | 0.00 | hold-thin-panel | 100% (hold-trivial) |
| holder-vs-actor | 3 | 3 | 12 | 100% | 1.00 | 0.00 | 0.00 | hold-trivial | new in v14 |
| household-chores | 5 | 7 | 28 | 29% | 0.00 | 0.75 | 0.45 | hold-unsolved | 98% (hold-trivial) |
| identity-join | 4 | 4 | 16 | 100% | 1.00 | 0.00 | 0.00 | hold-trivial | new in v14 |
| latest-event | 3 | 3 | 12 | 100% | 1.00 | 0.00 | 0.00 | hold-trivial | 100% (hold-trivial) |
| next-action-responsible | 2 | 2 | 8 | 62% | 0.25 | 0.75 | 0.75 | hold-unsolved | new in v14 |
| owner-after-correction | 6 | 9 | 36 | 81% | 0.50 | 0.00 | 0.00 | hold-thin-panel | 100% (hold-trivial) |
| school-logistics | 6 | 9 | 36 | 100% | 1.00 | 0.00 | 0.00 | hold-trivial | 100% (hold-trivial) |
| standing-status | 6 | 9 | 36 | 72% | 0.25 | 0.00 | 0.00 | hold-thin-panel | 100% (hold-trivial) |
| subscriptions-renewals | 5 | 6 | 24 | 50% | 0.25 | 0.00 | 0.00 | hold-thin-panel | 71% (admit) |
| travel-logistics | 6 | 9 | 36 | 56% | 0.12 | 0.00 | 0.00 | hold-thin-panel | 100% (hold-trivial) |

Reading:

- *Hold-trivial is no worse than live.* With the same tool, 8 of the 10
  families comparable at v13 are hold-trivial at 98 to 100% on the live
  benchmark today. v14 lowers the solve rate on 8 of the 10 shared families,
  by 19 to 69 points. The two still trivial at v14 (school-logistics,
  latest-event) were equally trivial at v13.
  The new role and temporal families solve at 94 to 100%. Trivial cases are
  uniform points for every agent and cannot reorder them.
- *Identity-join (SN-359)* was solved on 16 of 16 cases by 4 independent
  owners. It is neither broken nor misgraded: it holds only as too easy. The
  concern that it is adjacent to the excluded owner-alias-email shape is not
  borne out.
- *Household-chores* is the one family with real unsolved mass: 29% solved,
  5 owners, unsolved bound 0.75 (0.45 under independence). The cases are
  well-posed revert chains. One example: the dog walk passes Lauren, then
  Christopher, then Erica, and then a record says it "is back to what it was
  before the most recent change", so the answer is Christopher.
  - The inference traces of the champion's run show the agent answering
    "Erica Abraham" on the base and its variants. Its own prompt heuristic
    takes the latest record that announces itself as a change.
  - Subscriptions-renewals fails the same way on a struck-entry chain: the
    agent answers "switch it to annual billing" for an entry the notes say was
    struck.
  - The grader scored both correctly. This is the difficulty SN-353/354 were
    added for, and every agent fails it alike. Weight: one 4-case group in
    about 6 of 7 seeds, about 1.6% of memory cases, under 1% of the composite.
- *Thin panels.* Next-action-responsible (2 owners, 62%) and the 3-owner
  role and temporal families sit below the panel floor. Their measured rates
  are admissible or trivial.

Verdict by the letter of familygate: five families hold as trivial
(school-logistics, identity-join, holder-vs-actor, latest-event,
current-channel) and household-chores holds as unsolved. The orchestrator,
under the operator's activation authority, closed H2 as MEDIUM with **no
hold-out**:

- hold-trivial is no worse than live v13 and cannot misrank;
- identity-join is solved and correctly graded;
- household-chores is well-posed, uniform difficulty under 1% of the composite.

Holding families out would move the v14 vector and reopen gate 1, the GIH
diagnostic and the canaries. Familygate tuning (a hold-trivial rule relative
to the live baseline, panel depth for rare families) is recorded for v15 as
an SN-351 follow-up.

**Gate 5 (partial, no spend): the v14 composite rule on accepted v13 cases.**
Platform keeps each accepted score's per-case rows (`details.per_case`: kind,
category, score, correct, called and observed tools), but not the reply text.
So the v14 *composite* rule (the declarative over-call change) can be replayed
on the exact observed cases. The v14 *grading* changes (rendered-text reading,
the claim-set correction, the #2734 matchers) and the new v14 cases cannot.
Method: every accepted v13 score of every `scored` or `live` agent with at
least 3 of them (684 scores, 228 agents), decoded into
`[]protocol.CaseScore`, then `scorer.AggregateForVersion` at 13 and 14.
`AggregateForVersion(…, 13)` reproduces all 684 stored composites exactly.

- v14 minus v13 per score: mean -0.0100, range -0.208 to +0.086; 535 of 684
  scores move.
- Ranked by mean composite, the top six keep their places and places 7 and 8
  swap. 188 of 228 agents move somewhere.
- The current hybrid top five lose 0 to 0.0025 each.

So the over-call rule moves top agents very little. It mainly hits agents
that take unrelated actions on declarative cases, by up to 0.21. The 0.045
to 0.111 canary losses above come almost entirely from the new v14 cases and
the v14 grading, which only executed runs can measure. Gate 5 is still open:
the replay of executed transcripts into separate v14 rows has not been run.

## Generator-competition changes (SN-361)

These change v14 generation only. V13 generation and grading stay
byte-identical. Each change that moves the v14 vector makes the earlier
v14 qualification evidence stale, including the 40-seed GIH diagnostic at
`8f1005957` (#3179: quality 0.9501 over 14,000 cases). Rerun it on the new
source before citing it. Contributors are credited by generator letter in
[the credits ledger](../research/dittobench-datagen/docs/v14-generator-credits.md).

### SN-351 family-mix admission gate

`research/dittobench-datagen/internal/familygate` (CLI `cmd/familygate`) folds
per-case panel verdicts, labelled by family, into one row per family. A family
is admitted only with at least 3 rounds of evidence, a trivial rate of at most
0.60 and an unsolved rate of at most 1/3. A case counts as solved only when at
least 2 distinct owners solve it, as in the generator competition's
`min_solver_owners`. The rows come from a private operator column, an offline
panel run or Backroom's generator round view. The column `at threshold` counts
cases where every solver scored exactly 0.5 (SN-352).
`universe.V14FamilyMix` lists every v14 program family with its origin and
admission state. `universe.V14ExcludedShapes` lists the shapes SN-351 rules out,
with round evidence: simple update chain, two-hop, single retraction,
scattered list, single-hop as-of and needle as always trivial; conditional,
alias-reroute and owner-alias-email as unsolved. Tests refuse a new v14 or
generator-derived family that uses an excluded shape, and a generator-derived
family that is generated before admission. Applied to the SN-351 evidence
(`internal/familygate/testdata`), the gate holds the public default and
needle/as-of shapes as trivial, holds conditional, alias-reroute,
owner-alias-email and revised-cheapest as unsolved, and admits
revert-to-original. This change adds no generation bytes.
The role-slot families SN-359 (identity-join) and SN-360 (holder-vs-actor) are
ledger entries too. They are v14-structural re-implementations of generator C,
D and J shapes, pending their own panel. Identity-join sits next to the
excluded owner-alias-email shape, so its panel row decides whether it ships.

Qualification gate 4 must include the `familygate` table for every family
`universe.V14FamiliesAwaitingPanel` returns, measured over at least 3 rounds.

### SN-352 the leading generator's 0.5 per-case ceiling

The leading generator (generator B in
`research/dittobench-datagen/docs/v14-generator-credits.md`) earns about 4x
the next generator's score. Almost no panel agent scores above 0.5 on its
cases. Its primary family is a **two-item list about one person** asked by full
name:

- One item is recorded under the full name.
- The other is recorded under a nickname that only an identity record binds
  to that person.
- Several look-alike nicknames, one suffix away, each carry an item in the
  same frame. Those items are distractors.

The memory grader credits a list by the fraction of items present
(`grade/v13.go` `gradeListV13`). So the full-name item alone scores exactly
0.5, a look-alike's item zeroes the case, and only both items score 1
(`challenge/half_credit_test.go`). The panel matches this exactly. All 27
top promotion candidates in rounds 25955-25967 have panel mean 0.2, which is
2 solvers × 0.5 / 5. Every trivial case in round 25955 has mean 0.5 with 5/5
solvers, and only two cases in round 25963 exceed 0.5.

Verdict: the hard half is real, not a grading or format artifact.
`challenge.ValidateGenerated` rejects any pack whose gold answer (both items)
scores below 1.0 under the production grader, and round 25963 has two cases
with panel means 0.6 and 0.7, so some agents do earn full credit. The
nickname is honestly recoverable from the identity record, and the
look-alikes are legitimate interference. But the panel almost never earns
the hard half. The generator is credited for who avoided a look-alike's item, not for
who resolved the nickname, because `solve_threshold = 0.5` counts the easy
half as a solve.

V14 does not port the mechanic at this difficulty. `internal/familygate` now
holds a family as `hold-half-credit` when more than half of its solved cases
are solved only at the threshold. Replaying the candidates
(`internal/familygate/testdata/sn352_candidates.json`) holds the family. The
same rows with full-credit solves would be admitted. Porting is deferred, not rejected. In v14's continuous scoring, the hard
half is exactly the signal worth having. A port needs two things: a
program-envelope slot (the v14 business and personal slot counts are fixed,
and SN-356 to SN-360 are changing those families now), and full-credit
evidence over 3 rounds, for example a single-value nickname hop scored 0 or 1. Changing the live generator scoring so a
half-credit-only case does not count as solvable is recorded as a follow-up
for the generator competition owners. It changes live rewards, not v14, so
it is not part of this change. No v14 generation bytes change.
### SN-353 / SN-354 supersession chains without announcing cues

In v13, each correction family has one superseding record, and that record
announces itself ("the newest entry governs", "replacing ..."). With v14's
story-order stamps, the cue sentence, the second record and the latest filing
all point to the answer. V14 renders a 2-3 hop chain in the owner,
standing-status and current-channel business families and in six personal
domains (household, appointments, milestones, subscriptions, travel,
hobbies). Every chain has one of:

- a revert (A, then B, then A again, or A, B, C, then B);
- a declined proposal, optionally after a real change;
- an entry logged in error, so the value before it stands.

The counterfactual flips exactly one link: the proposal is adopted, the entry
is confirmed, or the last change names a different value. Cue placement is
seeded per group: half announce every adopted change, a quarter are cue-free
(state the change plainly, with precedence only from filing order), and a
quarter misplace the cue onto a non-governing record. Half of the reverts do
not restate the value ("back to the original entry"). That idea is ported from
generator D's revert family (`research/dittobench-datagen/docs/v14-generator-credits.md`).
`universe/v14_supersession_test.go` proves the counterfactual changes exactly
one record. It also proves that the last-cue, second-record and latest-named
shortcuts each solve both readings of at most 16% of chained groups (0.08,
0.00 and 0.16 over 120 seeds). On v13, the second-record shortcut is exact.
The oracle scores 1 under the v14 grader.
Next-action, school list, latest-event and client-vs-vendor are left to
SN-356 to SN-360. The v14 known vector moves; v13 stays byte-identical. The
chained families are listed in `universe.V14FamilyMix` as v14-structural,
pending a 3-round `familygate` panel measurement.

### SN-355 point-in-time questions and SN-358 relative event sequences

From the generator competition (generator C's dated-status, range-lookup,
long-order and relative-trips shapes, rounds 25946-25963; generator G's
single-hop as-of lookups are the trivial control). The mechanics are
re-implemented in the v14 contract; no generator code is copied.

- **Channel as of a date (SN-355)** replaces v13's current-channel family at
  v14. The case file holds four dated channel changes. The question names a
  date strictly inside an interval that is neither the first nor the last,
  never on a change date, so the latest value and the first value are both
  wrong (and both are distractors). The counterfactual moves one change
  across the question date.
- **Relative event sequence (SN-358)** replaces v13's latest-of-three family
  at v14. The case file holds five to eight dated events, one or two of them
  dated only as "N days after/before" an absolutely dated event within four
  weeks (one hop). The question asks for the nth event in date order, or the
  event immediately before or after a named one. The answer is never the
  first or last event by date, and its record is never the first or last
  filed. The counterfactual moves the answer event's date across a
  neighbour. The pair count varies with the event count, so the case length
  is no longer a constant six.
- **Temporal slot rotation.** The full profile scores four business groups,
  so v13 families 4-6 never reach a scored run. From v14 each seed draws which
  of latest-event (now the event sequence), next-action-responsible and
  current-channel (now as-of) fills cycle position 2; the other two take
  positions 4 and 5.
- **Run-contract stamp.** The v13 slot table stamped the business, personal,
  enterprise, record-quantity and injection cases with bench version 13 even
  in a v14 run, so the grader read them under the v13 policy and no v14
  grading correction (#2734, #2769) reached them. V14 runs now stamp every
  memory case 14. V13 keeps the builders' stamps.
- **Admission ledger (SN-351).** Both families are recorded in
  `universe.V14FamilyMix` as `v14-structural`, `pending-panel` (shapes
  `dated-interval-as-of` and `relative-dated-sequence`), so they appear in
  `V14FamiliesAwaitingPanel` and qualification gate 4 must measure them with
  `familygate` before activation. Generator C's panel evidence motivated them;
  the v14 renderings themselves have not been panel-measured yet.

### SN-356 multi-step process and SN-357 membership fold

From the generator competition (generator D's next-open-step and
removed-member shapes, rounds 25955-25963; generator C's set-intersection,
round 25955; generator G's single next step and the public default kit's
scattered list are the trivial controls). The mechanics are re-implemented
in the v14 contract; no generator code is copied.

- **Multi-step process (SN-356)** replaces next-action-responsible at v14.
  The plan record lists three to five ordered steps with their owners. Later
  records close steps (done or dropped), may finish a later step early, may
  reopen a closed step, and reassign one step's owner. The question asks for
  the first step still open and its current owner. The plan's first step is
  never the answer, so copying the action from record 0 (half credit on
  every v13 case) now scores 0. The counterfactual flips one step's state in
  one record.
- **Membership fold (SN-357)** replaces the school list's single drop and
  add at v14: a removal followed by a re-add, a replacement, an optional
  addition and an optional no-op removal (three to five events). Grading
  keeps one set-member claim per item at 1/n: a missing member costs its
  share, while a stale (replaced) item, the no-op item or the decoy list's
  item zeroes the list. The first list scores 0. The counterfactual changes
  only the replacement record.
- **Admission ledger (SN-351).** Both are `v14-structural`, `pending-panel`
  in `universe.V14FamilyMix` (shapes `multi-step-process-fold` and
  `membership-fold`) and must be measured by qualification gate 4.

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

## Open activation blockers

This is the October 27 readiness audit, kept for the record. V14 activated on
2026-10-28 (see *Activation record*). Each item now carries its disposition at
activation. An item marked "not closed at activation" was not recorded here
as closed or waived. It stays an open follow-up, and this record does not
claim the work was done.

Closed in code:

- **Weight fold receipt parity.** Issue #2989 was fixed by #3184. The
  validator fold and the enforce ledger check now read the full-confirmation
  receipt for every confirmation-capable version, matching Platform's crown.
  Before the fix, enabling confirmation enforce on a v14 ledger could crown a
  different champion than the Platform pin. Every weight receipt would then
  fail `ledger_pin_mismatch`. The fix does not change live v13 shadow
  behavior. Validators must run a release that includes #3184 before enforce
  is served on any v10+ ledger.
- **Public v14 adversary control.** #3179 lets `parserprobe
  -require-public-control` validate a v14 report.
- **Public parser for the v14 role families.** #3210 restores the
  `-require-public-control` pass on the final v14 vector (see *Qualification
  evidence* above).
- **Fleet v14 capability.** Four validators on v0.397.0 advertise verified
  v14 capability with `v14_scored_runtime_env`. Three v14 diagnostic canaries
  completed cleanly.

Open, and requiring an operator, release-owner or paid run:

1. **Anti-template qualification (#1832).** *Not closed at activation; open
   follow-up.* The steps are in
   [the handoff](benchmark-v14-template-resistance.md):
   - an advisory-guidance carrier run
   - paired cases with fresh phrasing and misleading cues, plus paired cases
     where the stored decision differs
   - a coordinated v14 contract for the private producer and its delivery,
     replacing the private base, surface and artifact decoders that are still
     bound to v13
   - clean honest-agent and adversary controls against the documented 0.02
     and 0.05 limits

   These need private producer runs, provider spend and owner design
   decisions, so no code is merged for them here. The latest offline public
   GIH diagnostic, at `7852c80c3` on the final vector, still inverts v14 at a
   quality of 0.9498. Comments on #1832 give the report SHA.
2. **Release packet and a separate v14 cohort (#2508).** *Separate cohort
   waived by operator delegation at activation; v14 runs on the shared
   cohort.* Candidate packet,
   observed on October 27 at 21:20 UTC. It is not reviewed or accepted:
   - Merge source `8730e77d45e828c47c2a7f34969986a59453ed2a` (#3222).
     Release v0.399.0, release commit
     `e3f18675dd52ecdb43e3ded22bc50323abf9200f`, run 40577508333. It
     contains #3204, #3210, #3211, #3212, #3213, #3219 and #3222. Only
     #3212 and #3222 change v14 code, and neither moves the v14 vector or
     v14 grading.
   - Signed stack descriptor
     `sha256:d9b58c6e80bc2641d9077499639ef881f13a7a62df1d87f42be048c0f5a40a6b`,
     dittobench-api image
     `sha256:e965c94cbc49b9ad94b15878812e07d7d416b24f3f78d48ec5825acb1d3ee1c2`.
   - `v14_scored_runtime_env` sha256
     `57ea6e21d68c678ab8ccb0dcb76f89d49cb3e809a454864a3d547a01b2e27c63` on the
     three cohort members already on v0.399.0. The fourth member (`5CFtzzb4...`)
     was still on v0.398.0 and must converge before the packet is recorded.
   - Archived v13 cohort pin: rotation 12, packet source
     `c2eeb1dd6bb7afe5a50c34b06065fa9dd88f0fa3`, scorer image
     `sha256:60b03a024f86a67438e1b5b0d93cc975ac515d9f6029baa0fc5e32564092d745`,
     descriptor
     `sha256:16e3b8baabceb7cdc7746b015b727114f64f2c9fab24e784be06acf373a8926f`.
     The live authority packet has already followed the fleet to newer
     releases, so the v13 pin is not image-frozen.

   The cohort validators also serve v13, and no separate v14 cohort exists.
   Either stand up a separate v14 cohort, or have the operator explicitly
   waive that requirement and hold automatic stack updates on the v13
   members.
3. **Gate 3 controls.** *Not closed at activation; open follow-up.
   LongMem at subject version 14 is now a during-rollout shadow check.* The
   honest-agent canaries pass on all five of the
   current hybrid top five. Still missing: known-benign and adversarial
   harness canaries (no control agent row exists to canary), and the
   persistence, no-call, unrelated and mixed action and non-declarative write
   probes as executed runs (unit-pinned only). LongMem confirmation at subject
   version 14 cannot run before `start_benchmark_rollout` (see gate 3 above).
   Accept it as a during-rollout shadow check or waive it.
4. **Gate 4 family admission (SN-351): closed as MEDIUM, no hold-out
   (operator-delegated, #3231).** See
   *Gate 4 (family admission): measured from executed canaries* above. The
   familygate tuning follow-up targets v15.
5. **Fleet depth.** *At activation rollout control counted 5 v14-capable
   validators.* During October 27 one of the four v13 cohort members
   (`5HEouuDa...`) dropped out of v14 capability for several hours with a live
   v13 ticket, leaving three. That equals the three-validator quorum, so one
   more outage would have stalled every v14 rollout member. It returned on
   v0.399.0. At 21:20 UTC rollout control counts 5 v14-capable validators: the
   4 cohort members and `5CqJAjSj...`, a paused source-mode validator with
   `local_unverified` provenance. Before the start, confirm that at least 4
   managed, signed-descriptor validators are capable. Do not rely on the
   paused source-mode one.
6. **Shadow replay report (gates 4 and 5).** *Not closed at activation; open
   follow-up.* The no-spend part is measured:
   the v14 composite rule replayed on 684 accepted v13 per-case rows (see
   *Gate 5 (partial)* above), and five same-seed top-five canaries. Still no
   executed-transcript replay into separate v14 rows, no full rank-impact
   report, no operator review.
7. **Confirmation at v14.** *Unchanged by activation: confirmation stays
   `shadow`.* Bundles stay `shadow` until qualified v14 shadow
   bundles exist. Enforce stays off at activation unless that evidence is
   recorded separately.
8. **Generator-competition rounds follow activation (resolved; rounds open
   at v14).**
   Rounds freeze a bench version when they open and follow the active version
   where a round contract exists. The v14 pack contract adds v14 to the round
   contracts (`GENERATOR_ROUND_BENCH_VERSIONS = (13, 14)`).
   - What migrates automatically: a round opened after v14 activates runs at
     v14 once a validator's scorer is verified for v14 and advertises
     `generator-round-bench-14`. Until then it falls back to v13 with the
     reason in `generation.bench_contract`, as before.
   - Mixed generators: a v14 round offers v13 and v14 packs
     (`grading_bench_versions: [13, 14]`). A generator built before v14 keeps
     a v13 column, graded byte-identically; a v14-aware generator (starter kit
     version 4) declares v14. A column whose v14-offering request fails is
     retried once on the original v13 request. No generator is excluded by the
     upgrade.
   - Live after activation: `preview_generator_round` freezes bench 14 with
     no reason, 5 v14-capable round validators and the scorer grading
     [13, 14] (see *Activation record*). The first v14 round's checks are in
     tandem-upgrades.md runbook steps 6 and 7.
   - Was remaining before activation, operational only: release the scorer and
     validator carrying the change, confirm v14-verified validators report
     `generator_round_bench_versions` including 14, re-register the public
     default generator from starter kit version 4, and announce the v14 pack
     policy to generator miners (see the starter kit migration note). No live
     setting changes.
   - Rollback: deactivating v14 sends new rounds back to v13 at the next
     opening; a round already frozen at v14 finishes at v14.

9. **Miner rehearsal default (resolved for v14).** `local-rehearsal.py`
   reads `active_version` from the public rollout endpoint, so the default
   followed activation without a code change. The post-activation PR bumped
   the offline fallback `LIVE_SCORING_BENCH_VERSION` to 14 and the bench
   number quoted in `.agents/skills/mine/SKILL.md` (bump checklist step 8),
   and staged starter-kit provenance manifest v16 for the kit change.

All v14 vector work has landed (SN-351 to SN-360, answer normalization
#3189). The final vector is
`760d155a9f192b635d3f5a19ef58ebba211576dd9ae2bccf5ae89135ff8492d9`, and gate 1
was rerun against it (see *Qualification evidence*). Any further change that
moves the vector reopens gate 1 and the GIH diagnostic.
