# Deterministic enterprise slice

Delivery: https://github.com/ditto-assistant/ditto-subnet/issues/2027
Post-V13 scale: https://github.com/ditto-assistant/ditto-subnet/issues/2028

## Foundation implemented here

The enterprise world has explicit entities and assignment/set-edit events.
Chronology is independent of document order. Invalid set edits, dangling
references and ambiguous same-field chronology are rejected. Queries follow
up to eight typed reference edges, then return values or a count at a specified
event step. CSV and JSON serialize the same facts, without receiving a query,
answer or relevance flag. Rendering has a separate deterministic seed.

Six domain labels currently exercise a common relationship skeleton; this is
NOT yet six domain-specific workflows. The generator draws valid membership
edits across multiple members, and opaque entity/channel bindings prevent
suffix-based join inference. Programs now compose scalar/set reference follows,
scalar equality filters and terminal projection/count/typed-hour sum. Comparison,
broader domain-specific operations and tuple-answer programs remain delivery work.

CSV, JSON and Markdown round-trip tests verify the same event histories across
multiple documents. Slack-style, email and synthetic transcript frames express
explicit assignment/add/remove semantics and use long records with shuffled
events. No renderer receives a target query, so irrelevant and supporting facts
share their format and grammar. These initial frames still need independent
honest/adversarial runtime qualification; phrase-coverage tests alone are not it.

Tests cover seed replay, three-hop answers checked against source events,
historical set membership, query-target rotation, world growth and ordering
invariance, lossless quoted/multiline CSV and JSON, and invalid histories.
No test claims proof that every shortcut or relevance classifier is defeated.
Semantic filtering that preserves all necessary evidence is legitimate.

## Next stack layers

1. Domain-specific workflows and valid bounded arbitrary histories; generalize
   typed query composition and independent oracle tests.
2. Deterministic CSV/JSON/Markdown/Slack/email/transcript renderers with long
   records and shared grammar for all facts; format and leakage audits.
3. Explicit V13 deterministic generation identity and scored-envelope wiring;
   disable private/model-authored V13 route while preserving its code for V14.
   Reconcile Platform gates, validator/scorer contracts and public rehearsal.
4. Honest-friendly grading and negative controls, held-out source-aware
   adversaries, and the same pinned honest harness on OSS20B versus OSS120B.
5. Exact-head review, CI, merge/deploy, supported canary and rollout.

This API is opt-in and not yet called by the scored V13 envelope. No change to
active benchmark selection or scoring is made by this foundation. Public seed
reproducibility is not privacy; no unreconstructibility claim is made.
