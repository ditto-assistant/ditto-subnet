# V13 fact-world migration

`GenerateV13FactPrograms(worldSeed, presentationSeed, count)` is the new opt-in
business-program path. It covers every business family, not only ownership.
It does not yet replace the full private artifact producer.

## One source of truth

The seeded draw becomes typed assertions: entity, field, canonical value,
surface alias, chronology/date, source record, and evidence semantics.
Queries operate over those assertions, not over rendered strings:

| Operation | Evidence | Rejection |
| --- | --- | --- |
| read | One static field value | Missing or multiple values |
| latest | Explicit ordered changes | Missing evidence or tied chronology |
| latest_date | Dated occurrences | Missing dates or tied chronology |
| conflict | Two independent source records | Missing or same-source records |

Tuple queries combine results, e.g. the next action and its current owner.
The evaluator generates the canonical answer and grading claims. Rendering
uses the same facts and query; it neither generates nor reinterprets facts.
Status aliases stay attached to canonical ontology values. Dates are explicit
calendar dates, not inferred from note order.

The bounded renderer rejects histories it cannot faithfully express. A
causal counterfactual changes exactly one fact and one record, not the query
or unrelated facts. Presentation entropy varies syntax without altering the
world. Independent conflicting records are explicitly non-superseding, and
the question requests both claims and the disagreement; they are not an
unanswerable current-owner task. Distractors are separate-entity assertions.

## Tests and remaining gates

- 100 world seeds × six presentations × 28 cases check answer, item, claim,
  query-program and distractor parity with the seeded world contract.
- Every family has one-fact/one-record counterfactual tests, input-order
  invariance, and unrelated-entity isolation checks.
- Negative tests reject unsupported, missing and ambiguous evidence.
- Legacy generation remains unchanged; default deployment does not use this
  opt-in entry point.

These are structural checks, not a language-model semantic review or proof
of shortcut resistance. Finite reviewed templates can still be learned.
Next: independent exact-slice evaluation; personal/story/quantity/tool migration;
atomic producer/decoder provenance integration without blind rewriting or
mandatory typo injection; full qualification and private canary before rollout.
Never reuse the old rewritten candidate's qualification identity for this path.
