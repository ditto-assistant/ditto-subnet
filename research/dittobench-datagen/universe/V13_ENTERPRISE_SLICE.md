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
broader domain-specific operations and tuple-answer programs remain post-V13 work
in #2028.

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

## Launch boundary

The four-PR launch stack includes the typed world/query/renderer foundation,
scored-envelope integration, honest-answer grading fixes and deterministic
issuance/capability negotiation. It is based directly on main: the unmerged
model-authored fact-generation stack and its database migration are preserved on
their existing branches for V14, not required to launch this slice.

Remaining release evidence is honest/adversarial runtime testing, including the
same pinned harness on OSS20B versus OSS120B, exact-head CI/review, deployment,
supported canary and rollout. Local oracle/negative-control tests are not a
substitute for those runtime results.

The deterministic V13 envelope now calls this API: 12 full-profile business
slots (four small/medium) carry three-hop contacts, filtered membership counts
and hour sums, with renderer/noise invariants and causal counterfactuals. The
total envelope is unchanged. Trusted provenance protects these records from
the legacy surface pass. Served CSV/JSON answers are independently replayed in
tests. Negated structured answers are no longer accepted as positive claims.

The issuance layer now selects deterministic generation for new V13 leases and
requires the exact scorer feature through signed capability negotiation. Private
producer code remains available for deferred research but is not queued by
production lease creation. No live activation, honest-model qualification or
complete domain-specific workflow coverage is claimed. Public seed
reproducibility is not privacy.
