# Bench bumps without automatic readmission

Status: design for Peyton's decision, 2026-11-08. Docs only. This PR changes no
behaviour and enables nothing. Every setting named below is a proposal and does
not exist yet unless a file and line say it does.

## Why

Bench bumps are planned several times a week. Each one re-scores the previous
version's agents on the new contract, and that re-scoring is most of what a bump
costs. Peyton asked for a bump that costs the subnet little: previous-version
agents are not readmitted for free, miners stay on top of updates, and emission
that no miner earns is burned.

The cost story is less clear than the framing, so this doc separates what was
observed from what is inferred before comparing options (see Data and
Limits). The short version of the finding: after `rescore_cohort_size` fell from
15 to 8, most of what is left of the bump tax is contender scoring. Contenders
will resubmit under every option, so no readmission policy can remove that cost.
What these options change is who is scored, how long a bump takes, what happens
to emission while the board is thin, and who carries the risk. The reasons to
build them are therefore mostly structural, and the doc says where the money
claim is weak.

## Data

Observed figures come from the cost analysis notes of 2026-11-07 and 2026-11-08
(ledger rows, not OpenRouter bills). I did not re-measure them: the production
read tool was denied for this session, so the numbers below are the notes' own
and are marked as such. Code facts were read in the repository at
`31e9d72dd` (v0.464.1).

| Item | Value | Source |
|---|---|---|
| v17 agents created before the bump (48) | $310 | notes, UPDATE 4 |
| v17 agents created after the bump (12) | $48 | notes, UPDATE 4 |
| v16 agents created before the bump (18) | $270 | notes, UPDATE 4 |
| v16 agents created after the bump (11) | $112 | notes, UPDATE 4 |
| Bump tax, share of the bump's scoring cost | 87% (v17), 71% (v16) | derived: 310/358, 270/382 |
| Cost per scored run on gpt-oss-20b | $1.0–1.4 | notes |
| Tail agent / contender, scoring cost | $3–8 / $20–45 | notes |
| Seven contenders, share of v17 spend (at 290 grants, $302) | $187, 62% | notes, UPDATE 2 |
| v17 board | 22 entries; ranks 1–5 at 0.96/0.94/0.91/0.74/0.69; 17 at 0.16–0.24 | notes, UPDATE 2 |
| Upload fee | 0.1 TAO, about $22, since 2026-11-05 | notes |
| Uploads | 8–33 a day | notes |
| Emission split | memory 56%, generator 24%, gm 20% | task brief, not re-read |
| Rank shares | 0.65 / 0.14 / 0.10 / 0.07 / 0.04 | `apps/platform/ditto/api_server/koth.py:55` |

Two things the "created before the bump" split hides. First, it counts every
agent uploaded before the bump, including submissions that were still in
screening and first reached a validator queue after it. Those enter the new era
under their own rule, not as readmission
(`apps/platform/ditto/db/queries/benchmark_admission.py:101`,
`:215`). Second, the 48 is larger than any cohort the v17 rollout could have
frozen (15 under revision 15 of the policy, per the notes), so at least 33 of
those agents arrived by some other lane. Splitting the
$310 into frozen-cohort cost and everything else needs one read-only query on
the production database (Phase 0). Until it runs, the readmission part of the
tax is bounded above by the 15 cohort members and below by zero.

## How a bump works today (verified in code)

**Starting a rollout.** `start_benchmark_rollout` reads the queue policy once and
freezes it onto the rollout row
(`apps/platform/ditto/api_server/endpoints/admin_benchmark_rollout.py:1139-1141`).
`historical_rescore_cohort` picks the cohort from the previous benchmark's
scored agents: finalized quorum, at least 100 cases, positive composite, ranked
by median composite, one agent per owner family; if the previous version has
fewer than the limit, the next older version fills the rest, and never a third
(`apps/platform/ditto/db/queries/benchmark_rollout.py:460-540`, `.limit(2)` at
`:495`). The start refuses with 409 if fewer than 5 members qualify
(`admin_benchmark_rollout.py:1145`). Each member gets a frozen dataset pin on the
new version (`create_rollout_snapshot`, `benchmark_rollout.py:1225`).

**The two cohort settings.** `rescore_cohort_size` and `priority_cohort_size`
are "next rollout" fields with a floor of 5 and a ceiling of 25
(`apps/platform/ditto/api_models/queue_policy_settings.py:65`, `:69`, `:552`,
`:558`); the priority size may not exceed the rescore size (`:682`). The floor is
enforced three more times: in the snapshot (`benchmark_rollout.py:1306`), in the
start endpoint (`admin_benchmark_rollout.py:1145`), and in database CHECK
constraints on the rollout row (`apps/platform/ditto/db/models.py:5788`, `:5797`,
`:5807`). So zero carry-over cannot be reached through the setting. It needs
code and a migration.

**Who is admitted to the new era's queue.** Agents created at or after
`rollout.created_at`, agents whose first queue entry came in the new era, frozen
cohort members, adopted previous-generation carryover, and audited contract
refreshes (`benchmark_admission.py:215-286`). A fresh upload after the rollout
starts is placed on the new version immediately (`arrival_bench_version`,
`benchmark_rollout.py:911`). Previous-generation carryover is a separate lane;
it exists and is disabled by default (`PrevGenCarryoverSettings.enabled = False`,
`queue_policy_settings.py:339`; consumed at
`apps/platform/ditto/api_server/benchmark_rollout.py:545`).

**The authority switch.** The ledger flips to the new version when both hold:
the first `priority_cohort_target` frozen members each have 3 scores on the new
version (permanently ineligible members skipped), and at least
`MIN_DESIRED_AUTHORITY_AGENTS = 5` owner families hold a ranked 3/3 quorum there
(`_live_desired_authority_ready`, `benchmark_rollout.py:694-720`; constants at
`:90`, `:108`, `:145`). The flip is recorded sticky
(`maybe_record_desired_authority`, `:765`) and the whole pool flips at once:
after it, every row that is not a new-version quorum is dropped from the ledger
(`apps/platform/ditto/db/queries/scores.py:2234`). Before it, new-version
scores are rollout progress and are not on the ledger (`scores.py:2241`).
Agents outside the cohort that never resubmit therefore already vanish from the
ledger at the flip today. Their `scored` status and old scores remain and
`?bench_version=` still shows the historical view. This is the practical
meaning of "no automatic readmission", and it already holds for everyone
outside the cohort.

**Open rollouts.** Only one can be open. `supersede_open_rollout`
(`benchmark_rollout.py:1029`) abandons a rollout that has not yet taken
authority.

## Fee credits today (verified)

`evaluation_payments.credit_for_agent_id` is not a free-resubmission hook.

- It records a reusable credit created by an accidental byte-identical upload:
  the miner paid, the Platform found an agent with the same sha256 under the
  same payment-time coldkey, kept the on-chain proof as a credit row with
  `agent_id` NULL and `credit_for_agent_id` set, and returned
  `payment_disposition="reusable_credit"`
  (`apps/platform/ditto/api_server/endpoints/upload.py:790-832`;
  duplicate lookup `payments.py:146`, insert `payments.py:176`).
- A credit is consumed atomically by a different artifact from the same hotkey
  (`consume_evaluation_credit`, `payments.py:267`; call site `upload.py:965`).
  A credit older than 24 hours from the on-chain block timestamp is refused
  (`UPLOAD_ADMISSION_TTL`, `apps/platform/ditto/db/queries/submission_settings.py:38`,
  `upload.py:194`).
- The row still needs a real on-chain proof as its primary key, a positive
  `amount_rao`, and an XOR between `agent_id` and `credit_for_agent_id`
  (`models.py:1969`, `:2054`, `:2056`). The fee-revenue report sums that column
  (`apps/platform/ditto/api_server/endpoints/admin_miner_fees.py:46`, `:62`), so
  a synthetic zero-fee row would distort revenue unless it is excluded by kind.
- A paid identical resubmission already works: the form flag
  `allow_identical_rescore` skips the duplicate refusal (`upload.py:567`,
  `:793`) and `/upload/check` returns error 1104 without it (`upload.py:332`).
  A same-artifact resubmission costs the full fee today.

So no free-resubmission credit exists. Option C needs a new credit type that
does not depend on a chain proof.

## How unfilled rank shares fold today (verified)

- The validator folds `rank_shares[:len(recipients)]`, so a short board gets a
  short prefix of the curve (`ditto/validator/weights.py:958`; docstring at
  `:877`). `apply_miner_emission_cap` then divides each weight by the total and
  multiplies by `miner_share`, so a lone champion receives all of it
  (`weights.py:154-192`). With one eligible miner, that miner takes the whole
  memory pool. With zero eligible miners the vector goes 100% to the burn hotkey
  (`weights.py:184`). The jump from "everything burns" at zero miners to
  "nothing burns" at one is a discontinuity worth fixing in its own right.
- Platform mirrors the same prefix: `emission_shares` returns
  `curve[:len(members)]` (`koth.py:414`) and the ledger pin divides by the
  occupied total (`ledger_pin.py:584`, in `pin_expected_shares` at `:438`).
- `burn_share` is a flat fraction served on the ledger and read by
  `resolve_miner_emission_share` (`weights.py:118`); it is 0 today per the
  notes. It does not depend on board size.
- The mechanism a burn rule can reuse already exists. Protocol 28's provisional
  incumbent puts a never-paid share under `UNPAID_SHARE_KEY` (`weights.py:84`);
  `split_unpaid_share` removes it and returns the paid fraction (`weights.py:494`);
  the worker multiplies `miner_share` by that fraction so the remainder burns
  (`ditto/validator/worker.py:2861`, `:2940`); and the ledger pin already
  prescribes paid shares summing below one and computes the burn from them
  (`ledger_pin.py:597`, `pin_expected_burn`).

## What a burn-unfilled-shares rule needs

The rule: with `k` recipients, shares `rank_shares[k:]` are not renormalised
onto the recipients; they burn. Burn share of the miner pool at `k` recipients
with the current curve, and the corresponding share of total emission when memory
holds 56% (the 56% is the task brief's figure, not re-read):

| Recipients | Burned share of memory pool | Share of total emission |
|---:|---:|---:|
| 5 | 0% | 0% |
| 4 | 4% | 2.2% |
| 3 | 11% | 6.2% |
| 2 | 21% | 11.8% |
| 1 | 35% | 19.6% |

A lone champion would receive 0.65 of the memory pool (about 36% of total
emission) instead of all of it (56%).

Work it requires, following the protocol 35 pattern in `docs/title-duels.md`
section 6:

1. **Protocol 36.** Raise `HEARTBEAT_PROTOCOL_VERSION` from 35
   (`ditto/validator/build_info.py:175`). A flat `burn_share` or an operator
   `rank_shares` curve cannot express a board-size-dependent burn, so this is a
   fold change and cannot ship as a setting alone.
2. **Validator fold.** In `compute_weights` (`weights.py:835`), when the served
   marker is on, add `1 - sum(recipient_shares)` under `UNPAID_SHARE_KEY`. The
   existing split, cap and treasury paths already handle it
   (`worker.py:2861-2940`). The score-ceiling joint crown (all shares equal,
   sum one) has no unfilled share and is unchanged. Plumb the flag through
   `MemoryFoldParams` (`ditto/validator/tracks.py:93`, `:135`).
3. **Platform fold.** `emission_shares` (`koth.py:400`) and
   `pin_expected_shares` (`ledger_pin.py:438`, `:584`) must stop renormalising
   under the marker, so the pin prescribes paid shares summing below one.
   `pin_expected_burn` and the classifier's `paid < 1` branch
   (`ledger_pin.py:597`, `:660`) already carry the result. The smallest rank
   share is 0.04, above the comparison tolerance, so an unfilled slot is
   distinguishable.
4. **Served marker and gating.** A new setting alongside `tie_weighting_mode`
   and `crown_duel_mode`, a `*_is_active` helper in the style of
   `apps/platform/ditto/api_server/continual_retest_settings.py:56`, and the
   fleet check `live_weight_setter_fleet_supports_protocol`
   (`apps/platform/ditto/db/queries/heartbeats.py:456`), which covers every
   recently-live weight setter and fails closed on an empty fleet. The ledger
   field is added to the validator wire model (`ditto/api_models/validator.py`).
   A requester below 36 gets 428 and a stale replay drops the marker, as with
   protocol 35. Exposing a fold-changing field to part of the fleet would make
   validators submit different vectors for the same evidence, so the gate is not
   optional.
5. **Parity fixtures.** Both folds run one shared fixture, as the duel cases do
   (`ditto/tests/fixtures/crown_gate_cases.json`, run from
   `ditto/tests/validator/test_crown_gate.py` and
   `apps/platform/ditto/tests/api_server/test_crown_gate.py`). Add short-board
   cases with k = 1..4, with and without tie pooling, with a provisional
   incumbent, and with a joint crown. Extend the pin fixture
   `ditto/tests/fixtures/provisional_incumbent_pin.json`, the emission golden
   (`ditto/tests/validator/test_emission_allocation_golden.py`), and regenerate
   the contract goldens with `scripts/gen_validator_contract.py` plus the
   Backroom generated types, since the ledger model gains a field.
6. **Rollout gating.** Ship the code, wait for every weight setter to report 36,
   confirm the weight diagnostics show `matches_pin`, then flip the setting.
   Rollback is a settings revision back to `disabled`, as with `crown_duel_mode`.
7. **Operator visibility.** Backroom shows and sets the field, and the public
   treasury forecast shows the burn. `CLAUDE.md` asks for a Backroom read tool
   for every live knob.

## Options

The options differ in who is scored, not in what a scored agent costs. Cost
figures use the observed per-agent averages (v17: $6.46 per pre-bump agent and
$4.00 per post-bump agent; v16: $15.00 and $10.18) and are **estimates** for
anything not already observed.

### A. Status quo with cohort 8

*What it is.* Revision 16 of the queue policy (`rescore_cohort_size = 8`,
`priority_cohort_size = 5`), effective at the next rollout start. Nothing else.

*Bump scoring cost.* Observed baseline $310 for pre-bump agents on v17 with
cohort 15. The notes infer a $30–60 saving from the smaller cohort; I could not
verify it. Estimate: $250–280 tax, $300–330 total at v17 scale. Because 48 > 15,
the cohort cut may not touch the larger part of the tax (see Data).

*Miner experience.* Unchanged. Top-8 agents are rescored without action;
everyone outside drops from the ledger at the flip without notice.

*Emission during the transition.* The old board is paid until the flip, then the
whole pool flips at once. The flip is system-paced (the cohort is rescored by
validators without miner action), so the old board is paid only for the
scoring time.

*Gaming and hoarding.* Smallest surface. Thin boards are not a risk because the
cohort guarantees 5 members. A miner can still sit in the cohort at the system's
expense without engaging.

*Code.* None.

*Rollout and rollback.* Already applied; another queue policy revision undoes it.

### B. Hard reset: resubmission required, no carry-over

*What it is.* Cohort of 0. Every agent, including the champion, must be
uploaded again, paying the 0.1 TAO fee.

*Bump scoring cost.* Pre-bump rescoring is $0 by construction. Cost is the
resubmitted agents plus new entrants. Contenders resubmit, so their cost stays:
if the seven contenders resubmit, about $187 (observed share of v17) plus about
$48 for new entrants, about $235. If the whole cohort-8 equivalent resubmits it
is about the same as A. Savings versus A are bounded by the number of cohort-8
members who do not resubmit, about 0–3 agents ($0–90, estimate). Fees at 0.1 TAO
return about $22 per resubmitter, which is roughly $154 for seven contenders and
close to their scoring cost.

*Miner experience.* The worst of the four. Several bumps a week at $22 each is
$66 or more a week per kept agent. Miners with a tail agent worth little will
leave; that lowers cost and board size together. Contenders either pay or are
dropped.

*Emission during the transition.* The old board keeps being paid until the flip,
and the flip now needs 5 owner families to resubmit and reach 3/3. Four engaged
families means no flip. A champion on the old board profits from delay, so
non-resubmission is a strategy. Without a deadline rule the rollout can stall
for good.

*Gaming and hoarding.* The first resubmitter on a thin board is paid at the
flip. Fee-farming is not a risk since the fee is the full price. Sybil fillers
(extra coldkeys each paying 0.1 TAO to fill ranks 2–5) pay off if four slots are
worth more than $88 of emission per hold.

*Code.* Allow cohort 0: the setting floor (`queue_policy_settings.py:65`), the
snapshot and start checks (`benchmark_rollout.py:1306`,
`admin_benchmark_rollout.py:1145`), the three CHECK constraints
(`models.py:5788-5807`, new migration), and a new flip rule. With the priority target
also 0, the existing priority gate is vacuously true
(`rollout_cohort_score_complete`, `benchmark_rollout.py:1698`; it returns false
today whenever the member count differs from the target), which leaves only the
5-family condition and its stall. No protocol change.

*Rollout and rollback.* Setting plus code behind a default-off mode, frozen at
rollout start. In flight, `supersede_open_rollout` abandons a rollout that has
not yet taken authority.

### C. Opt-in readmission with a free resubmission credit

*What it is.* At rollout start, each eligible owner gets a credit. Within the
credit window the owner can upload the byte-identical artifact of their agent
without paying. Only owners who act cost scoring.

*Eligibility (proposal).* The owner's best agent holds a ranked 3/3 quorum on the
previous authoritative version and sits within the top M of that board (M = 10 is
a starting point). One credit per owner family
(`attested_emission_owner_roots`, as the cohort already uses), not per agent.
Without the rank limit a junk agent that paid once would get a free scoring
slot every bump.

*Bump scoring cost.* Same per-agent cost as A, times the engaged fraction of the
top M. If every one of the top 8 opts in it equals A. Estimate $150–300; the
saving over A is the disengaged part of the top M, plausibly $0–90. Fee revenue
is lost on the resubmissions (about $22 each).

*Miner experience.* Better than B. A kept agent needs one CLI call per bump, no
fee. Bots will automate it, so engagement filtering will weaken over time: a bot
makes C close to A with the cost still on the subnet. Miners who edit their
agent for a harder bench must pay the normal fee because the artifact is no
longer identical (decision 4 below).

*Emission during the transition.* As B: the flip depends on 5 families acting.
Needs the deadline rule from D.

*Gaming and hoarding.* Credit farming is bounded by the rank limit and by one
credit per owner family. A farmer's gain is a free scoring slot for an agent
that is already competitive, so there is little to farm. Sybil resubmits need
separate coldkeys that each hold a top-M agent, which means paying the fee
upfront and scoring in the top M. Thin-board hoarding is as in B.

*Code.* A credit table keyed by (rollout, owner family) with expiry and a
consumed-by agent; an upload path that accepts a credit id in place of a payment
proof and requires the sha256 to equal the source agent's; a bypass of the
duplicate refusal (`upload.py:793`); a payment row for the new agent. Every
reader joins `evaluation_payments` to find the owner (`payments.py:146`,
`scores.py` emission-owner key, `count_ranked_quorum_agents`), so the new agent
needs a row: either a synthetic row with a `kind` column, a reserved proof key
and `amount_rao = 1` (satisfies `models.py:2056`) excluded from fee sums, or
making the column nullable and auditing every reader. The synthetic row is the
smaller change. Also: the CLI (`ditto/miner_cli/commands/upload.py`), the
1104 message ("The previous submission cannot be resubmitted"), the
per-owner concurrency limit of 3 (`queue_policy_settings.py:124`) and
submission cooldown (error 1105), and a check that the similarity rail does not
treat a byte-identical own resubmission as a copy. I did not verify that last
one.

*Rollout and rollback.* Policy field frozen onto the rollout at start (the
existing next-rollout pattern). Rollback is a revision back to cohort mode for
the next rollout; in flight, supersede.

### D. C plus burn of unfilled shares and a flip rule

*What it is.* C, plus the burn rule above, plus a flip rule: flip when 5 owner
families hold a ranked 3/3 quorum (today's rule), or at the end of the credit
window with at least `min_agents_at_deadline` families, or at a hard deadline
with a smaller minimum.

*Bump scoring cost.* Same as C.

*Miner experience.* Miners who stay current are paid in full. Miners who miss a
bump lose their place and, if enough miss, the whole board pays less
(the unfilled share burns). That is the "stay on top of updates" pressure Peyton
described, and it is also what makes churn costly. A miner who is away for a
day is out of the next flip.

*Emission during the transition.* The flip is bounded in time. The old board is
paid until the earliest flip condition holds. If few families engaged, the new
board pays them 65/14/10/… and burns the rest, so thin boards have a built-in
reserve price. The flip cannot be held hostage by a champion who stops
resubmitting.

*Gaming and hoarding.* See the next section. The rule removes the stall
incentive and removes the lone-miner windfall, but it does not prevent a weak
agent from holding the crown for the length of a dethrone (about a day,
`docs/title-duels.md`). Sybil fillers remain: with burn, empty slots have a
price, so slots 2–5 are worth filling for anyone who can get a nonzero composite
past the 100-case eligibility floor (`weights.py` `filter_eligible`). A minimum
composite relative to the champion for paid slots would close it; that is a
further decision (6).

*Code.* C, plus protocol 36 and everything under "What a burn rule needs", plus
the flip rule in `_live_desired_authority_ready` and `maybe_activate_rollout`.

*Rollout and rollback.* Three independently reversible pieces: cohort mode,
flip rule, burn marker. Burn is fleet-gated and reversible by a settings
revision.

## Hoarding when the board is thin after a reset

- **The first resubmitter profits only if the flip happens early.** The ledger
  stays on the old board until the flip (`scores.py:2241`), so an early resubmitter
  earns nothing on the new version until then. The hoarding window starts at the
  flip and lasts until a better agent dethrones. A flip at the deadline with two
  families would put a weak champion on 65% of the memory pool, and the paired
  duel needs about a day to remove a champion (title duels, risks).
- **Delay is a strategy.** A champion on the old board earns on the old bench
  until the flip. Under B and C without a deadline, not resubmitting extends that.
- **Counter-measures, cheapest first.** (1) A minimum-families rule so a lone
  early resubmitter cannot flip alone. (2) A grace period equal to the credit
  window so prior-board contenders have a full chance before any forced flip;
  until then the old board pays. (3) A hard deadline so delay stops paying.
  (4) The burn, so a thin board reduces the champion's take. (5) An optional
  minimum composite relative to the champion for rank shares.
- **Crown standing.** The previous champion's owner can inherit incumbency across
  the bump because the incumbent is matched by owner root
  (`ledger_pin.py:1105`); I read it, I did not exercise it. If it holds, the
  previous champion keeps a head start when they resubmit, which strengthens
  their reason to resubmit first and weakens a hoarder.
- **Not verified.** How long contenders take to reach 3/3 after a resubmission.
  The windows below assume it is hours, not a day.

## Comparison

| | A cohort 8 | B hard reset | C opt-in credit | D C + flip rule + burn |
|---|---|---|---|---|
| Bump scoring (estimate) | $300–330 | $150–300 | $150–300 | $150–300 |
| Saving vs A (estimate) | none | $0–90 | $0–90 | $0–90 |
| Fee revenue per bump | none | about $22 per resubmitter | none | none |
| Miner cost per bump | none | 0.1 TAO | one CLI call | one CLI call |
| Churn risk | low | high | medium | medium, higher if windows are short |
| Flip time | system-paced | miner-paced, can stall | miner-paced, can stall | bounded by deadline |
| Thin-board payout | n/a | lone miner takes all | lone miner takes all | lone champion takes 65%, rest burns |
| Protocol change | no | no | no | yes (36) |
| Schema change | no | yes (CHECKs) | yes (credit, payment kind) | yes |
| Rollback | settings | settings, supersede | settings, supersede | settings, supersede |

The dollar rows are not independent: they share the unknown split of the $310.
Treat "saving vs A" as an upper bound until Phase 0 reports.

## Recommendation

Build D in phases, and let Phase 0 decide whether to go past it. A is already
applied and is the right default until the v18 numbers exist. B is not worth its
churn: it moves cost to miners at several bumps a week, and the contenders who
make up most of the cost will pay it and stay. C alone leaves the stall and the
lone-miner windfall in place, so it is incomplete without D's flip rule. D's burn
has value independent of bumps, because today's zero-versus-one cliff
(`weights.py:184` versus `:154-192`) pays a lone miner everything.

If Phase 0 shows that the readmission part of the tax is under about $60 a bump,
skip the credit machinery (Phase 2) and ship only the flip rule and the burn.
They address emission integrity, which does not depend on the cost split.

## Phases

0. **Measure, read-only, no code.** After the v18 rollout, split the pre-bump
   agents' scoring grants into frozen-cohort members, cohort-8 equivalents,
   rotation re-scores, and everything else (queued submissions that first reached
   a queue in the era). Report the readmission part of the tax and the
   time from rollout start to flip. Report contender time to 3/3 for the
   window sizing. This is the number the whole project hangs on.
1. **Flip rule, Platform only.** Add `min_agents_at_deadline`, `flip_deadline_hours`
   and a grace period to the queue policy as next-rollout fields. Extend
   `_live_desired_authority_ready` and rollout state telemetry. Cohort
   stays at 8, so behaviour only differs when a flip stalls. Backroom shows the
   remaining condition (`bench_promotion_requirement`, `benchmark_rollout.py:2672`).
2. **Opt-in credit, Platform and miner CLI.** Credit table and upload path,
   cohort mode `opt_in`, migration relaxing the three CHECK constraints so
   cohort 0 is storable. Default `cohort`. Preview-tested with minted accounts.
3. **Burn unfilled shares, protocol 36.** Both folds, parity fixtures, served
   marker, fleet gate, Backroom visibility. Default `disabled`.
4. **Enable, in order, one at a time:** flip rule on a real bump, then `opt_in`
   on a bump with a miner announcement, then the burn after every weight setter
   reports protocol 36 and `matches_pin` holds across one epoch.

Operator payloads (Peyton applies; never flipped by the build). Field names are
proposals; they do not exist today. Written from the current revision with only
these fields changed, as `docs/title-duels.md` does.

Phase 1, after it ships:

```json
{"flip_deadline_hours": 36, "min_agents_at_deadline": 3}
```

Phase 2, for the bump that tests the credit:

```json
{"readmission_mode": "opt_in", "resubmit_credit_window_hours": 24, "resubmit_credit_top_m": 10}
```

Phase 3, after the fleet reports protocol 36:

```json
{"unfilled_share_mode": "fleet_ready"}
```

Rollback for each is the same payload with the prior value (`"cohort"`,
`"disabled"`, or the previous numbers). Changes to the readmission mode take
effect at the next rollout start because they are frozen onto the rollout row.

## Decisions for Peyton

1. **Credit window.** Proposal: 24 hours to claim. It must exceed contender time
   to 3/3 (unmeasured) and cover a full waking day in the main miner time zones.
   Bumps every 2–3 days leave room for one window and one settle period only.
2. **Minimum agents to flip, and the deadline.** Proposal: flip at 5 families, or
   at 36 hours with at least 3, with one 12-hour extension; below that,
   hold the old board and page the operator. A minimum of 1 is only safe with the
   burn on.
3. **Whether the burn applies, and from when.** It also applies outside bumps
   whenever fewer than 5 miners are eligible. That is intended here, and it
   reduces miner income in any thin period. The 56% memory share (task
   brief) was not re-read.
4. **Identical artifact only, or any artifact.** Identical-only keeps credit
   farming near zero but gives nothing to miners who must change code for a
   harder bench; any-artifact makes the credit a free upload and costs about $22
   of fee revenue per use.
5. **Eligibility.** Top M by previous-board rank, or every owner with a current
   agent. The second is what the brief described; it is more generous and
   exposes the subnet to the tail cost of junk agents.
6. **Minimum composite for a paid slot** (relative to the champion), to stop
   sybil fillers on a burn-priced thin board. Not required for D to work.
7. **Miner communication.** What is announced, how many hours' notice before a
   bump, and whether a missed bump is final (no grace for absent miners). The
   public rollout state already explains the remaining condition in one sentence
   (`benchmark_rollout.py:2672`); new-version scores stay off the leaderboard
   until the flip, so a long window looks like silence to miners unless
   progress is published.
8. **Bump cadence.** The largest cost lever is how often bumps happen. At the
   observed contender cost (about $190) three bumps a week is about $570 a week
   for contenders alone, before new entrants (estimate). Neither this doc nor any
   option changes that.

## Risks

- **The money case is weak.** After cohort 8, B, C and D mostly cut cost that
  cohort 8 already cut. The estimated extra saving is $0–90 a bump, with an
  unmeasured lower bound of zero. Do not approve this on cost alone.
- **Engagement bots.** If most miners automate resubmission, C and D approach A in
  cost and add schema, CLI and fold complexity.
- **Fold parity.** A burn rule touches the consensus fold. A mistake in either
  fold fails every weight receipt with `ledger_pin_mismatch`. The fleet gate and
  the shared fixtures are what stop this; the rollout order above exists for
  it.
- **Silent bump failure.** Per the bump skill, past bumps were stranded by a
  leftover version pin in one layer. A new flip rule adds a path that needs the
  same cross-layer audit.
- **Longer stalls hurt harder miners.** Miners in other time zones lose the most to
  a short window.
- **Revenue.** C and D forgo about $22 per resubmitter; B keeps it.

## Limits of this doc

Observed in code at `31e9d72dd`: the rollout, cohort, admission, authority
switch, credit and fold behaviour cited with file and line above. Taken from the
notes without re-measurement: all dollar figures, board composition, the 56/24/20
split, the fee, `burn_share = 0`, and the policy revision state.

Inferred, not run: the cost split between frozen-cohort members and other
pre-bump agents, the savings estimates, previous-champion incumbency across a
bump, the dethrone time, and that a byte-identical own resubmission passes the
similarity rail.

Could not verify: any production state. The production read tool was denied in
this session by the permission classifier, so no query was run. No setting was
changed, no rollout started, and nothing was posted.
