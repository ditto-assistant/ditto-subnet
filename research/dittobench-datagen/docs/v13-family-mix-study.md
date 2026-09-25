# Bench v13 memory-mix study

Bench v13 turns the memory mix into a published slot table
(`gen/v13_envelope.go`) and audits it per seed with `cmd/mixaudit`
(`gen/mixaudit.go`). This study records the interim measurement: the envelope
is complete and byte-reproducible, but three slots (personal programs,
abstention, point-in-time) are filled by non-monetary ordinary world questions
and two slots (business programs, record-determined quantity) still run the
monetary v12 generators. Every remaining gate violation below maps to one of
those pending generators. Nothing here changes generation bytes.

## Method

For each seed the tool generates the full-profile dataset, classifies every
memory case (slot, domain / sub-domain, answer kind × operation, money weight
with typed list items at their share of case credit, arithmetic, computed vs
verbatim over the declared evidence, dependency cluster), and evaluates
`gen.MixGateV13` (Owner decision — default taken: ≤ 12% target / 15% hard):

| Cap / floor | Value |
| --- | --- |
| money weight | ≤ 15% hard (12% target) |
| money-bearing cases | ≤ 22 |
| monetary open programs | 0 |
| arithmetic-required | ≤ 20% |
| money share of computed answers | ≤ 25% |
| personal / business | ≥ 30% / ≥ 40% |
| any sub-domain | ≤ 20% |
| any answer-kind × operation | ≤ 15% |
| abstention | 10% ± 1 |
| twin coverage | ≥ 40% |
| gate-exposed | ≤ 40% |
| largest dependency cluster (cascade) | ≤ 4% |
| project-outstanding cases | ≤ 4 |

The classifier is pinned to the v12 diagnosis in issue #1529
(`TestMixAuditReproducesV12MoneyExposure`): seed `123456789` reproduces 117
direct money cases, 143 money-bearing cases, and 127.83 / 251 = 50.9% of memory
weight, with 143 of 157 computed answers monetary.

## Commands

Run from `research/dittobench-datagen`:

```bash
go run ./cmd/mixaudit -bench-version 12 -seeds 40 -run-size full > v12mix40.md
go run ./cmd/mixaudit -bench-version 13 -seeds 40 -run-size full > v13mix40.md
go run ./cmd/mixaudit -bench-version 13 -seeds 40 -json -classes > v13mix40.json
go run ./cmd/mixaudit -bench-version 13 -seeds 40 -gate   # exit 1 on a violation
go test ./gen -run 'MixAudit|V13' -count=1
```

## v12 baseline, seeds 1–40

# Bench v12 full memory mix, 40 seeds

| seed | cases | money wt | money cases | $ programs | arith | $ of computed | personal | business | max sub-domain | max kind x op | abstain | twin | gate-exposed | cascade | violations |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 251 | 0.509 | 143 | 40 | 0.522 | 0.935 | 0.327 | 0.673 | business/finance 0.442 | money/balance 0.402 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 2 | 251 | 0.509 | 143 | 40 | 0.542 | 0.941 | 0.327 | 0.673 | business/finance 0.442 | money/balance 0.402 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |
| 3 | 251 | 0.513 | 144 | 40 | 0.538 | 0.941 | 0.335 | 0.665 | business/finance 0.442 | money/balance 0.406 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 4 | 251 | 0.505 | 142 | 40 | 0.522 | 0.928 | 0.323 | 0.677 | business/finance 0.442 | money/balance 0.398 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 5 | 251 | 0.501 | 141 | 40 | 0.526 | 0.934 | 0.327 | 0.673 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 6 | 251 | 0.513 | 144 | 40 | 0.530 | 0.954 | 0.343 | 0.657 | business/finance 0.442 | money/balance 0.406 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 7 | 251 | 0.509 | 143 | 40 | 0.534 | 0.941 | 0.311 | 0.689 | business/finance 0.442 | money/balance 0.402 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 8 | 251 | 0.509 | 143 | 40 | 0.534 | 0.923 | 0.315 | 0.685 | business/finance 0.442 | money/balance 0.402 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |
| 9 | 251 | 0.497 | 140 | 40 | 0.506 | 0.933 | 0.339 | 0.661 | business/finance 0.442 | money/balance 0.390 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 10 | 251 | 0.501 | 141 | 40 | 0.522 | 0.953 | 0.343 | 0.657 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 11 | 251 | 0.489 | 138 | 40 | 0.510 | 0.932 | 0.335 | 0.665 | business/finance 0.442 | money/balance 0.382 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |
| 12 | 251 | 0.513 | 144 | 40 | 0.542 | 0.923 | 0.323 | 0.677 | business/finance 0.442 | money/balance 0.406 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 13 | 251 | 0.501 | 141 | 40 | 0.526 | 0.934 | 0.343 | 0.657 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 14 | 251 | 0.509 | 143 | 40 | 0.522 | 0.966 | 0.315 | 0.685 | business/finance 0.442 | money/balance 0.402 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 15 | 251 | 0.497 | 140 | 40 | 0.526 | 0.921 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.390 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 16 | 251 | 0.497 | 140 | 40 | 0.514 | 0.933 | 0.327 | 0.673 | business/finance 0.442 | money/balance 0.390 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |
| 17 | 251 | 0.505 | 142 | 40 | 0.518 | 0.934 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.398 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 18 | 251 | 0.509 | 143 | 40 | 0.526 | 0.953 | 0.307 | 0.693 | business/finance 0.442 | money/balance 0.402 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 19 | 251 | 0.497 | 140 | 40 | 0.542 | 0.903 | 0.343 | 0.657 | business/finance 0.442 | money/balance 0.390 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 20 | 251 | 0.501 | 141 | 40 | 0.522 | 0.959 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 21 | 251 | 0.509 | 143 | 40 | 0.522 | 0.941 | 0.327 | 0.673 | business/finance 0.442 | money/balance 0.402 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 22 | 251 | 0.505 | 142 | 40 | 0.534 | 0.953 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.398 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 23 | 251 | 0.489 | 138 | 40 | 0.514 | 0.920 | 0.327 | 0.673 | business/finance 0.442 | money/balance 0.382 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |
| 24 | 251 | 0.517 | 145 | 40 | 0.534 | 0.942 | 0.315 | 0.685 | business/finance 0.442 | money/balance 0.410 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 25 | 251 | 0.501 | 141 | 40 | 0.522 | 0.910 | 0.327 | 0.673 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 26 | 251 | 0.517 | 145 | 40 | 0.526 | 0.942 | 0.323 | 0.677 | business/finance 0.442 | money/balance 0.410 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 27 | 251 | 0.501 | 141 | 40 | 0.518 | 0.922 | 0.347 | 0.653 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |
| 28 | 251 | 0.497 | 140 | 40 | 0.530 | 0.915 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.390 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 29 | 251 | 0.505 | 142 | 40 | 0.506 | 0.940 | 0.319 | 0.681 | business/finance 0.442 | money/balance 0.398 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 30 | 251 | 0.497 | 140 | 40 | 0.526 | 0.927 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.390 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 31 | 251 | 0.513 | 144 | 40 | 0.526 | 0.947 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.406 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 32 | 251 | 0.497 | 140 | 40 | 0.510 | 0.940 | 0.323 | 0.677 | business/finance 0.442 | money/balance 0.390 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 33 | 251 | 0.501 | 141 | 40 | 0.530 | 0.910 | 0.339 | 0.661 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 34 | 251 | 0.505 | 142 | 40 | 0.534 | 0.947 | 0.327 | 0.673 | business/finance 0.442 | money/balance 0.398 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 35 | 251 | 0.501 | 141 | 40 | 0.534 | 0.910 | 0.339 | 0.661 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 36 | 251 | 0.505 | 142 | 40 | 0.526 | 0.934 | 0.315 | 0.685 | business/finance 0.442 | money/balance 0.398 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 37 | 251 | 0.493 | 139 | 40 | 0.518 | 0.908 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.386 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |
| 38 | 251 | 0.501 | 141 | 40 | 0.526 | 0.910 | 0.331 | 0.669 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |
| 39 | 251 | 0.501 | 141 | 40 | 0.522 | 0.934 | 0.323 | 0.677 | business/finance 0.442 | money/balance 0.394 | 0.000 | 0.183 | 0.183 | 0.012 | 11 |
| 40 | 251 | 0.509 | 143 | 40 | 0.530 | 0.923 | 0.323 | 0.677 | business/finance 0.442 | money/balance 0.402 | 0.000 | 0.183 | 0.183 | 0.012 | 10 |

Mean over 40 seeds: money 0.5040 (cases 141.7), arithmetic 0.5252, money-of-computed 0.9328, personal 0.3281, business 0.6719, abstention 0.0000, twin 0.1833, gate-exposed 0.1833, cascade 0.0120. Seeds violating the v13 gate: 40/40.

Slots (first seed):
- story: 91
- ordinary-world: 62
- business-programs: 40
- record-quantity: 24
- divergence: 12
- integrity: 13
- isolation: 9

Violations (first seed):
- project-outstanding cases 8 > 4
- money weight 0.5093 > 0.15 hard cap
- money-bearing cases 143 > 22
- monetary open programs 40 > 0
- arithmetic-required share 0.5219 > 0.20
- money share of computed answers 0.9346 > 0.25
- sub-domain business/finance share 0.4422 > 0.20
- answer-kind x operation money/balance share 0.4024 > 0.15
- answer-kind x operation value/current-channel share 0.1673 > 0.15
- abstention share 0.0000 outside 0.10 +/- 0.01
- twin coverage 0.1833 < 0.40 floor

## v13 interim, seeds 1–40

# Bench v13 full memory mix, 40 seeds

Interim slots (filled by non-monetary world questions until their generator lands): personal-programs, abstention, point-in-time

| seed | cases | money wt | money cases | $ programs | arith | $ of computed | personal | business | max sub-domain | max kind x op | abstain | twin | gate-exposed | cascade | violations |
| ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | --- | --- | ---: | ---: | ---: | ---: | ---: |
| 1 | 250 | 0.363 | 106 | 28 | 0.424 | 0.876 | 0.420 | 0.580 | business/finance 0.348 | money/balance 0.272 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 2 | 250 | 0.367 | 107 | 28 | 0.436 | 0.884 | 0.436 | 0.564 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 3 | 250 | 0.367 | 107 | 28 | 0.440 | 0.863 | 0.416 | 0.584 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 4 | 250 | 0.367 | 107 | 28 | 0.428 | 0.856 | 0.428 | 0.572 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 5 | 250 | 0.367 | 107 | 28 | 0.420 | 0.870 | 0.416 | 0.584 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 6 | 250 | 0.363 | 106 | 28 | 0.440 | 0.855 | 0.452 | 0.548 | business/finance 0.348 | money/balance 0.272 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 7 | 250 | 0.359 | 105 | 28 | 0.412 | 0.882 | 0.424 | 0.576 | business/finance 0.348 | money/balance 0.268 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 8 | 250 | 0.367 | 107 | 28 | 0.436 | 0.870 | 0.436 | 0.564 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 9 | 250 | 0.367 | 107 | 28 | 0.420 | 0.884 | 0.432 | 0.568 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 10 | 250 | 0.367 | 107 | 28 | 0.428 | 0.899 | 0.416 | 0.584 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 11 | 250 | 0.351 | 103 | 28 | 0.428 | 0.866 | 0.428 | 0.572 | business/finance 0.348 | money/balance 0.260 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 12 | 250 | 0.367 | 107 | 28 | 0.436 | 0.892 | 0.424 | 0.576 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 13 | 250 | 0.363 | 106 | 28 | 0.436 | 0.869 | 0.424 | 0.576 | business/finance 0.348 | money/balance 0.272 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 14 | 250 | 0.367 | 107 | 28 | 0.432 | 0.884 | 0.432 | 0.568 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 15 | 250 | 0.367 | 107 | 28 | 0.424 | 0.870 | 0.428 | 0.572 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 16 | 250 | 0.355 | 104 | 28 | 0.400 | 0.860 | 0.456 | 0.544 | business/finance 0.348 | money/balance 0.264 | 0.000 | 0.128 | 0.128 | 0.012 | 11 |
| 17 | 250 | 0.363 | 106 | 28 | 0.420 | 0.869 | 0.432 | 0.568 | business/finance 0.348 | money/balance 0.272 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 18 | 250 | 0.367 | 107 | 28 | 0.432 | 0.870 | 0.428 | 0.572 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 19 | 250 | 0.367 | 107 | 28 | 0.436 | 0.870 | 0.440 | 0.560 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 20 | 250 | 0.367 | 107 | 28 | 0.444 | 0.899 | 0.420 | 0.580 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 21 | 250 | 0.363 | 106 | 28 | 0.444 | 0.848 | 0.444 | 0.556 | business/finance 0.348 | money/balance 0.272 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 22 | 250 | 0.355 | 104 | 28 | 0.424 | 0.867 | 0.440 | 0.560 | business/finance 0.348 | money/balance 0.264 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 23 | 250 | 0.367 | 107 | 28 | 0.416 | 0.907 | 0.432 | 0.568 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 11 |
| 24 | 250 | 0.359 | 105 | 28 | 0.428 | 0.861 | 0.420 | 0.580 | business/finance 0.348 | money/balance 0.268 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 25 | 250 | 0.367 | 107 | 28 | 0.448 | 0.843 | 0.440 | 0.560 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 26 | 250 | 0.355 | 104 | 28 | 0.432 | 0.839 | 0.436 | 0.564 | business/finance 0.348 | money/balance 0.264 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 27 | 250 | 0.359 | 105 | 28 | 0.436 | 0.820 | 0.448 | 0.552 | business/finance 0.348 | money/balance 0.268 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 28 | 250 | 0.367 | 107 | 28 | 0.436 | 0.849 | 0.432 | 0.568 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 29 | 250 | 0.359 | 105 | 28 | 0.436 | 0.814 | 0.452 | 0.548 | business/finance 0.348 | money/balance 0.268 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 30 | 250 | 0.359 | 105 | 28 | 0.412 | 0.897 | 0.440 | 0.560 | business/finance 0.348 | money/balance 0.268 | 0.000 | 0.128 | 0.128 | 0.012 | 11 |
| 31 | 250 | 0.367 | 107 | 28 | 0.428 | 0.892 | 0.436 | 0.564 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 32 | 250 | 0.363 | 106 | 28 | 0.428 | 0.869 | 0.424 | 0.576 | business/finance 0.348 | money/balance 0.272 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 33 | 250 | 0.367 | 107 | 28 | 0.436 | 0.849 | 0.436 | 0.564 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 34 | 250 | 0.367 | 107 | 28 | 0.440 | 0.892 | 0.424 | 0.576 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 35 | 250 | 0.363 | 106 | 28 | 0.428 | 0.869 | 0.416 | 0.584 | business/finance 0.348 | money/balance 0.272 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 36 | 250 | 0.367 | 107 | 28 | 0.440 | 0.843 | 0.436 | 0.564 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 37 | 250 | 0.367 | 107 | 28 | 0.428 | 0.877 | 0.444 | 0.556 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 38 | 250 | 0.367 | 107 | 28 | 0.440 | 0.843 | 0.420 | 0.580 | business/finance 0.348 | money/balance 0.276 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 39 | 250 | 0.355 | 104 | 28 | 0.420 | 0.860 | 0.436 | 0.564 | business/finance 0.348 | money/balance 0.264 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |
| 40 | 250 | 0.359 | 105 | 28 | 0.432 | 0.868 | 0.440 | 0.560 | business/finance 0.348 | money/balance 0.268 | 0.000 | 0.128 | 0.128 | 0.012 | 10 |

Mean over 40 seeds: money 0.3638 (cases 106.1), arithmetic 0.4301, money-of-computed 0.8673, personal 0.4321, business 0.5679, abstention 0.0000, twin 0.1280, gate-exposed 0.1280, cascade 0.0120. Seeds violating the v13 gate: 40/40.

Slots (first seed):
- story: 78
- ordinary-world: 93
- business-programs: 28
- record-quantity: 16
- divergence: 12
- integrity: 14
- isolation: 9

Violations (first seed):
- money weight 0.3633 > 0.15 hard cap
- money-bearing cases 106 > 22
- monetary open programs 28 > 0
- arithmetic-required share 0.4240 > 0.20
- money share of computed answers 0.8760 > 0.25
- sub-domain business/finance share 0.3480 > 0.20
- answer-kind x operation money/balance share 0.2720 > 0.15
- answer-kind x operation value/current-channel share 0.2120 > 0.15
- abstention share 0.0000 outside 0.10 +/- 0.01
- twin coverage 0.1280 < 0.40 floor

## Reading the interim result

- Money falls from 50.9% to ~36% of memory weight and from 143 to ~106
  money-bearing cases before any dedicated v13 generator lands, because story
  arcs keep six oracles (one pure-money oracle dropped per arc), the ordinary
  slot caps `project-outstanding` at 4, and the interim fill is non-monetary.
- The structural bounds already hold on every seed: `project-outstanding` ≤ 4,
  gate-exposed 12.8%, largest cascade 1.2% (`TestV13MixAuditStructuralAcrossFortySeeds`).
- The remaining violations are exactly the pending generators: 28 monetary
  open programs (#1520), 16 monetary record-balance cases (#1837), two
  pure-money story oracles per arc (#1841), zero abstention (#1530), no
  point-in-time twins (#1844), personal programs (#1838). The
  `value/current-channel` answer-kind × operation share (~21%) is the interim
  contact fill and disappears with those slots.
- `TestV13MixAuditGateAcrossFortySeeds` reports these violations (first failing
  seed) and skips while `gen.V13InterimSlots` or `gen.V13InterimGenerators` is
  non-empty; it enforces the full gate the moment both lists are empty. Until
  then `TestV13MixAuditStructuralAcrossFortySeeds` pins the interim ceilings
  (money ≤ 37%, ≤ 110 money-bearing cases, arithmetic ≤ 45%) so the interim
  exposure cannot widen unnoticed.
