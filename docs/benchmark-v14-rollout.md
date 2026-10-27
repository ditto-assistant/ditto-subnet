# Bench v14 qualification and rollout

Status: compatibility implementation; **activation blocked pending qualification**.
This plan is part of #2508. It supersedes #2405's proposal to change scored v13
outputs. It grants no authority to deploy, rotate the v13 cohort, regrade scores,
alter emissions, or change a production setting.

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
`87eafc34950860ad60ccff97edbb34b835811dc0da40e22e5a65dec12424348c`,
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

This is the October 26 readiness audit. V13 is active and confirmation runs in
`shadow`. Each item below must be closed with evidence, or explicitly waived
by the operator, before anyone calls `start_benchmark_rollout` for v14. An
item listed here is a blocker. It is not a claim that the work has started.

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

Open, and requiring an operator, release-owner or paid run:

1. **Anti-template qualification (#1832).** The steps are in
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
   GIH diagnostic still inverts v14 at a quality of 0.9501. Comments on #1832
   give the report SHA.
2. **Release packet and fleet readiness (#2508).** This document already
   describes it: the exact merge SHA, scorer image and descriptor digests,
   `v14_scored_runtime_env` heartbeats from a separate cohort, and the
   archived v13 cohort packet.
3. **Accepted diagnostic canaries and the shadow report.** These are gates 3
   to 5 above. No measured v14 shadow results exist yet.
4. **Confirmation at v14.** Bundles stay `shadow` until qualified v14 shadow
   bundles exist. Enforce stays off at activation unless that evidence is
   recorded separately.
5. **Generator-competition rounds stay on v13 at activation (explicit
   fallback).** Rounds now freeze a bench version when they open and follow
   the active version where a round contract exists. The Platform lane,
   tickets, capability routing, V13 pin and the validator all run at the
   round's frozen version, and in-flight rounds never change contract.
   - What migrates automatically: nothing at v14 activation itself. A round
     opened after activation records `bench_contract` ("active bench v14 has
     no generator-round contract") and runs at v13. It is leased only to
     validators verified for v13 that sign the V13 cohort authority packet.
   - Still pinned to v13, and why: `GENERATOR_ROUND_BENCH_VERSIONS` is `(13,)`.
     Round packs grade at `challenge.GradingBenchVersion = 13` (pack contract
     v1, which miners' generators emit). dittobench-api's `generator-round-v1`
     mode accepts only `bench_version` 13.
   - Remaining step: ship a v14 pack contract and a scorer round mode that
     accepts v14 and advertises it as a scorer feature. Then add 14 to the
     tuple. The pin test in `ditto/tests/test_bench_version_pins.py` keeps
     these in step.
   - Until then, rounds depend on a staffed v13 cohort after v14 activates. A
     round with no validator able to run v13 is void at opening with the
     reason.
6. **Miner rehearsal default (resolved for v13).** `local-rehearsal.py`
   now reads `active_version` from the public rollout endpoint, so the
   default follows activation without a code change. The activation PR still
   bumps the offline fallback `LIVE_SCORING_BENCH_VERSION` (now 13) and the
   bench number quoted in `.agents/skills/mine/SKILL.md` (bump checklist
   step 8).

Work in progress elsewhere that also changes the v14 vector:

- SN-351 to SN-360 (generator-derived datagen families)
- the answer-checker normalization and markdown-tolerance work

The vector and the replay evidence must be re-pinned after the last of these
lands, and before the release packet is cut.
