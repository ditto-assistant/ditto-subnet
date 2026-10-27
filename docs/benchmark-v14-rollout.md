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
`cb5962df023627c294be1de814ae22b65a3c58f6eb9a2bc9601bee8439f10432`,
with epoch `2027-06-01T00:00:00Z`. The grader (apart from the claim-set
correction above, the #2734 hyphen fold and matchers, and the answer-normalization
contract below), v13 gate postures,
LongMem instrument and public harness wire (9) carry forward. The surface carries
forward apart from case-file records, which v14 files in story order instead of
shuffled-presentation order (#3088); that moves the v14 vector and leaves every
v13 seed untouched. There is no private
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
5. **Generator-competition rounds stay on the v13 contract.**
   - `validator_generator_round.py` issues `benchmark_contract(13)` jobs.
   - `issue_generator_round_ticket` keys the ticket `(agent, 13, validator)`
     and applies the v13 scorer-cohort pin.
   - Activating v14 does not move rounds. They keep depending on a live v13
     cohort.
   - Before activation, the owner must decide whether rounds stay on v13
     (and keep that cohort staffed) or get their own versioned migration.
6. **Miner rehearsal default.** Since v13 activated,
   `local-rehearsal.py` `LIVE_SCORING_BENCH_VERSION` (still 12) and
   `.agents/skills/mine/SKILL.md` have lagged the live bench. Update both
   with the activation PR (bump checklist step 8). Otherwise miners rehearse
   an older contract by default.

Work in progress elsewhere that also changes the v14 vector:

- SN-351 to SN-360 (generator-derived datagen families)
- the answer-checker normalization and markdown-tolerance work

The vector and the replay evidence must be re-pinned after the last of these
lands, and before the release packet is cut.
