# Bench v13 memory-mix study

Bench v13 caps monetary exposure and rebalances the memory mix (issues #1518,
#1529, #1520, #1830). This study is the v12 **baseline** that rebalance is
measured against, produced by the deterministic histogram `cmd/mixaudit`
introduces, in the format of [v9-family-mix-study.md](v9-family-mix-study.md).
It publishes structural generation evidence only; no champion composite or
activation conclusion is claimed here. The v13 caps and floors are *defined*
in this study (`mixaudit.V13Envelope`) and asserted green only by the v13
envelope PR after the mix changes land; on v12 they document the gap.

## What the histogram measures

`cmd/mixaudit` classifies every memory case of a generated artifact along the
axes the envelope is written in:

| Axis | Source | Notes |
| --- | --- | --- |
| family | `question_type` | fails closed on any family it does not know |
| semantic domain / sub-domain | per-family profile, per list item | `personal`, `business`, `conversational`, `integrity` |
| answer kind | `answer_kind` and `answer_item_kinds` | fails closed on an unknown kind |
| head operation | per-family profile | `latest-by-time`, `prior-state`, `owner-of`, `balance-arithmetic`, `quantity-*`, `direction-of`, `verbatim-select`, `negation-select`, `counterfactual-select`, `reported-speech-select`, `attributed-select`, `preference-apply`, `acknowledge`, `chitchat`, `abstain`, `clarify` |
| monetary exposure | direct `money` kind **and** typed list items | list items weighted by their fraction of case credit |
| arithmetic-required | per-family profile | balance arithmetic, day sums and maxima, net change |
| computed vs verbatim | `gen.AnswerVerbatimInEvidence` over the case's evidence pairs | per claim |
| language | ASCII vs non-ASCII letters in the question | v13 multilingual fraction |
| twin / metamorphic relation | `twin_group`, `v10_provenance.relation`, counterfactual pairs | |
| gate exposure | twin, pair, provenance, catalog | share of weight a gate can move |

**Weighting contract.** A case contributes weight 1 to the memory denominator.
A list answer splits that weight evenly across its items because the grader
credits the fraction of items present: the `[direction, money]` net-change
answer is half monetary and the `[email, money, lesson]` summary is one third
monetary. Embedding money in a composite answer therefore cannot evade the
budget. On the public seed this reproduces issue #1529 exactly: **117** direct
`money` cases, **143** money-bearing cases, and **127.83 / 251 = 50.9%** of
memory weight monetary (`gen/mixaudit_test.go`).

## Reproducible structural audit

Run from `research/dittobench-datagen`:

```bash
go run ./cmd/mixaudit -bench-version 12 -seed 123456789 -run-size full
go run ./cmd/mixaudit -bench-version 12 -seeds 40 -run-size full -markdown > mix40.md
go run ./cmd/mixaudit -bench-version 12 -seeds 300 -run-size full -markdown > mix300.md
go run ./cmd/parserprobe -bench-version 12 -seeds 40 -run-size full -json > gih40.json
go run ./cmd/mixaudit -bench-version 12 -seeds 40 -run-size full -markdown -gih gih40.json
```

`-gih` attaches the per-seed generator-inverse family-identification rate from
`cmd/parserprobe` as the `gih_parse_rate` column; `-enforce` exits non-zero on
any seed that violates the envelope (the CI posture for v13, not v12).

## Seeds 1 through 40 (v12 `full`)

| Measure | Min | Mean | Max | v13 limit |
| --- | ---: | ---: | ---: | ---: |
| Memory cases | 251 | 251 | 251 | = 250 |
| Direct `money` cases | 112 | 115.7 | 119 | |
| Money-bearing cases | 138 | 141.7 | 145 | ≤ 22 |
| Money share of memory weight | 48.9% | 50.4% | 51.7% | ≤ 15% (target 12%) |
| Money-only cases | 112 | 115.7 | 119 | |
| Monetary open programs | 40 | 40 | 40 | = 0 |
| Arithmetic-required share | 51.8% | 53.7% | 55.4% | ≤ 20% |
| Computed share of evidence-bound claims | 56.5% | 58.5% | 60.1% | |
| Money share of computed claims | 85.3% | 88.3% | 91.7% | |
| Abstention share | 0.0% | 0.0% | 0.0% | 10% ± 1% |
| Twin/metamorphic coverage | 18.3% | 18.3% | 18.3% | ≥ 40% |
| Gate-exposed share | 18.3% | 18.3% | 18.3% | ≤ 40% |
| Cascade-dependent share | 14.3% | 14.3% | 14.3% | |
| Max single-error cascade | 1.2% | 1.2% | 1.2% | ≤ 4% |
| GIH parse rate (`cmd/parserprobe`) | 97.6% | 97.6% | 97.6% | report-only |

Domain share of memory weight: `business` 63.6–67.6%, `personal` 28.4–32.4%,
`conversational` 3.6%, `integrity` 0.4%. The largest sub-domains are
`business/project-finance` 22.4%, `business/ledger-programs` 15.9%,
`business/accounts` 9.6%, `personal/contacts` 6.8–11.6%, `personal/travel`
6.0–10.4%. The head-operation histogram is dominated by `balance-arithmetic`
(42.6–45.4%), then `latest-by-time` (14.1–17.7%) and `prior-state`
(7.2–11.6%); the `money × balance-arithmetic` cell alone carries 43.5–45.4%
of memory weight against the 15% anti-monoculture cap.

Every one of the 40 seeds violates the v13 envelope. Rules broken on all 40:
money weight (worst 51.7%), money-bearing cases (145), monetary open programs
(40), arithmetic share (55.4%), `money × balance-arithmetic` (45.4%),
`business/project-finance` sub-domain (22.4%), twin coverage (18.3%), and the
abstention band (0%, no v8+ family emits `decline`). `value × latest-by-time`
exceeds 15% on 37 seeds (worst 17.7%) and the personal floor fails on 7 seeds
(worst 28.4%). Gate-exposed share and the single-error cascade already sit
inside their caps.

## Seeds 1 through 300 (v12 `full`)

| Measure | Min | Mean | Max |
| --- | ---: | ---: | ---: |
| Direct `money` cases | 111 | 116 | 122 |
| Money-bearing cases | 137 | 142 | 148 |
| Money share of memory weight | 48.5% | 50.5% | 52.9% |
| Monetary open programs | 40 | 40 | 40 |
| Arithmetic-required share | 51.4% | 54.0% | 56.6% |
| Computed share of evidence-bound claims | 55.6% | 58.6% | 61.8% |
| Money share of computed claims | 84.8% | 88.4% | 93.1% |
| Twin/metamorphic coverage | 18.3% | 18.3% | 18.3% |
| Gate-exposed share | 18.3% | 18.3% | 18.3% |
| Max single-error cascade | 1.2% | 1.2% | 1.2% |

Only the ~62 ordinary world slots vary by seed; every other family count is
fixed, so twin coverage, gate exposure, and the cascade bound are constants of
the v12 contract and the money share moves within a four-point band. All 300
seeds violate the envelope; beyond the rules broken on every seed (worst money
share 52.9%, arithmetic 56.6%, `money × balance-arithmetic` 46.6%),
`value × latest-by-time` exceeds 15% on 256 seeds (worst 18.5%) and the
personal floor fails on 95 seeds (worst 26.8%).

## The generator-inverse baseline (`cmd/parserprobe`)

The GIH parse rate above is the memory family-identification rate of the
generator-inverse harness on the same 40 seeds. Its full pass-off result
(`go run ./cmd/parserprobe -bench-version 12 -seeds 40 -run-size full`):

| Slice | Cases | Family-id | Answered | Mean |
| --- | ---: | ---: | ---: | ---: |
| story | 3,640 | 100.0% | 97.7% | 97.7% |
| programs | 2,560 | 90.6% | 99.8% | 99.8% |
| personal | 1,616 | 100.0% | 100.0% | 100.0% |
| business | 826 | 100.0% | 100.0% | 100.0% |
| quantity | 878 | 100.0% | 100.0% | 100.0% |
| integrity | 520 | 100.0% | 100.0% | 87.9% |
| tool prompts | 4,000 | 99.0% | 100.0% | 99.9% |
| **composite** | | | | **0.992** (per seed 0.969–0.997) |

This is the number the private surface pass has to pull below the starter-kit
ceiling; see [bench-versions.md](bench-versions.md#bench-v13-surface-gate-and-mix-audit).

## What the v12 baseline says about the v13 rebalance

1. **Money is the difficulty axis, not one capability.** 88% of computed
   claims are monetary and one answer-kind × operation cell holds 45% of memory
   weight. Meeting the 15% cap and the 15% anti-monoculture bound requires
   replacing programs, story oracles, and project oracles together (#1520,
   #1529), not tuning one knob.
2. **Whatever replaces money must not become the next parse target.** The
   `value × latest-by-time` cell already exceeds 15% on 37/40 seeds; a
   contact-heavy rebalance would trip the same bound.
3. **Twin coverage is structurally fixed at 18.3%** (30 twin-grouped program
   cases plus 12 counterfactual-pair cases per 251). The ≥40% floor needs the
   #1520 decision/as-of twins on ordinary world families.
4. **No abstention exists.** The 10% ± 1% band is met only by the #1530
   abstention families.

## Required validation

```bash
go test ./...
go test ./gen -run 'Mixaudit|Parserprobe' -count=1 -v
go vet ./...
go run ./cmd/mixaudit -bench-version 12 -seed 123456789 -run-size full
```
