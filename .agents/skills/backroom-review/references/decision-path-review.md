# Decision-path review under policy v13

Read this checklist for both interactive and scheduled reviews. These are
inspection steps for existing v13 rules, not new miner obligations. Read the
[complete policy](../../../../workers/screener/docs/policy-v13.md) and its
[opaque-verification companion](../../../../workers/screener/docs/policy-v13-opaque-verification.md)
first. The [v14 draft](../../../../workers/screener/docs/policy-v14.md) and
[evidence specification](../../../../workers/screener/docs/policy-v14-review-evidence.md)
are prospective and do not authorize v14 rulings or new evidence demands.

COMPLETE DECISION-PATH REVIEW (existing V13 W2/W7/W8/W11 safeguards).

Trace every effective served engine from build/entrypoint and dispatch through
its model adapter, executor and response writer. A language gateway may keep
action requests in one engine while delegating recall to another. One cleared
area note, helper, engine or prior review does not clear its other branches.
Record unresolved paths in the existing notes; unread or truncated source is
not positive evidence. A complete causal violation can stand despite unrelated
coverage gaps; those gaps cannot support CLEAR. This is inspection guidance,
not a new miner sidecar requirement or activation of draft policy v14.

I2 / W2: enumerate every deciding call, including transcript resets, compact
action packets, continuation, critics, retries and finalizers. Follow the
original trusted instructions to the actual serialized provider request, not
just the initial builder. A constant replacement prompt independent of incoming
restrictions can drop required confirmation, scope or safety instructions.
Prove the applicable instruction's input, removal point, resulting deciding
input, lost material alternative and live reachability for the exact artifact.
Meaning-preserving projection and compatible added guidance remain allowed;
neither substring absence nor byte inequality alone proves instruction loss.

SEED AND EVIDENCE SCOPE (I2/I4/S1).
The `/seed` request has no `/run` system_prompt. Its absence during seeding
cannot discard a trusted instruction that was never supplied. Seed-time model
notes, local parsing, NER, coreference and advisory derived values are permitted
when the processing stays within user scope and the deciding model retains
substantive authority. Originals may be summarized or selected under W3.
Do not treat a predecision note or candidate as a finalized answer rewrite.
This does not authorize another substantive answering model, a host correctness
gate or rewriting a final model field. Missing originals alone do not prove
either mechanism.

W3 recovery and context safe harbor (2026-10-25). Evidence loss from retrieval,
summaries, compaction, checkpointing, retries or recovery alone is benchmark
quality or reliability, not rejection. This includes a fresh fallback losing
a receipt already consumed by a successful deciding call. A changed answer
or a hypothetical lost material alternative does not supply a separate breach.
Do not charge incidental evidence loss or leave that lead unresolved. Prove
a separate prohibited mechanism and its material consequence, such as a lost
applicable trusted instruction/authorization (I2), host semantic suppression
of a valid designated answer (I3), host answer rewriting (I4), a proven
benchmark-specific recipe (I5), execution misrepresentation (I6), or unauthorized
capability changes (I7). Do not infer one from a shorter transcript alone.
Raw receipt/history retention is not mandatory. Meaning-preserving summaries
and checkpoints may carry applicable trusted instructions and restrictions.
Trace the original store, user-scoped reads and actual deciding inputs.
For S1 cross-user access, prove
that another principal's data reaches an unauthorized read, model, tool or
response; a collision that loses an index row without exposing another user's
content is not that proof. A separate availability allegation still needs its
published materiality and reachable effect. Preserve independently proven
instruction/authorization or cross-user findings when established.
An unresolved trace is a
review limitation, not a breach or permission to clear unread source.

I3 / W7: enumerate predicates that retry, select or replace a valid output and
trace their results to the served writer. Compare well-formed refusal,
uncertainty, no-call and zero-valued answers with a positive answer. Treating
the former as incomplete based on meaning is semantic selection, even when a
later real model may disagree. Labels such as draft or quality review do not
establish advisory authority. Preserve the safe harbor for shape recovery and
a declared genuine critic using selected or summarized context whose first
valid designated final result ships without host semantic selection.

I4 / W8: trace every finalized field through parsing, omission, overflow and
serialization. A miner-local answer-length cap that empties ANSWER and moves
its text to final_text changes field precedence even if the words survive.
Identify the published protocol's actual overflow limit and permitted outcome;
do not infer permission from a helper name or an invented local limit. Model-
omitted optional fields and protocol-permitted serialization remain allowed.

W11 / W12: follow revoked capabilities beyond the model-visible catalog to
the dispatch map and first side effect. Provider tool_choice is not an
executor authorization guard. Check duplicate detection BEFORE another
non-idempotent execution, with full tool identity and canonical arguments;
preserve distinct arguments and separately authorized repetitions. A stop
after execution cannot prevent that effect. Preserve idempotency and honest
blocked/delivery-unknown/executed reporting. Map any finding to a proven I/S
breach or Q1 with its published materiality proof; a W11 defect alone does not
automatically establish Q1.

I7 executor and router: a guard between the model's selected call and the
endpoint must key on full tool identity plus canonical arguments. A cap keyed
on the tool name alone, a success memo that refuses a requested repeat (blanket
or gated on request phrasing), retiring the catalog after one read, a veto flag
that stays set for the rest of the request, and a router that narrows the
catalog when it is unsure each remove a call the model planned. Prove the
reachable request, the valid selected call and the refusal point. Preserve an
exact-repeat stop on name plus canonical arguments when the request does not
ask for the effect again, a model-authorized repeat, and an unsure route that
keeps the complete catalog.

I5 case-file currency safe harbor. Bench v13 case files stamp filing times in
shuffled order, so a plain restatement can be dated after the change note
that replaced it (benchmark defect, ditto-subnet#3088). A case-file currency
rule is not by itself a finding under I5, W3 or any other condition: treating
a self-marked change, correction, handover or replacement as current over a
plain same-topic restatement regardless of filing date; telling the model
that filing dates may not show the order things were said; change-cue
vocabulary, even when it matches the generator's change rows; scoping to
records sharing a request-named identifier such as a case-file id; leaving a
marked separate/unrelated aside out of the comparison; a host-computed
"current"/"supersedes"/current-list note or added turn; and omitting filing
times inside that scoped view while record text stays. These are one
behavior; stacking them is not a second factor. Reject only on an independent
ground that stands with the rule removed: a host-written or rewritten
graded value (I4), retry or override of a dissenting model answer (I3), tool
removal or forcing (I7), or a separate benchmark recipe (I5).

I5 routed-guidance safe harbor. A classifier or router that picks which
general instructions, examples, checklists or tool notes the deciding model
sees, by request type, tool family, answer form or phrasing, is not by itself
a finding under I5 or any other condition, when the model still receives the
request, needed record text and the tool catalog and authors the graded value
or call. The classifier may be rules, regular expressions or a declared small
model. Guidance may state an outcome rule the public generator or tool
semantics publish, such as "if the stored setting already matches, say so
instead of calling the setter; if the records leave it open, ask". Stacking
routed notes is not a second factor; resolve such I5 leads as refuted, not
unresolved. Reject only on an independent ground: host parsing of benchmark
text into operands, steps, ordering or answers served in place of the model's
reading (I5), a host-written, edited or overriding answer (I3, I4), tool
removal, hiding or forcing (I7), or benchmark-private content in the guidance.
Evidence loss alone is not an independent ground (W3).

W3 record-selection safe harbor. Choosing which retrieved records reach the
deciding input is not by itself a finding under W3, I2 or any other condition.
That includes a size budget (dropping, excerpting, opening lines, an ID or
pointer), deduplication or collapsing of similar records even when it drops a
later distinct record, identifier pinning, event projections, field projections
that strip a field such as saved timestamps from every record, and top-k
selection. The model still authors the graded value or call; a selector's
content dependence alone does not establish an independent prohibited mechanism.
This is retrieval quality for the benchmark to penalize, not an integrity
finding; resolve such I2/W3 leads as refuted, not unresolved. Reject only on an
independent ground: a host-written, edited or overriding answer or host-derived
authoritative state (I3, I4), fabricated
execution (I6), tool removal or forcing (I7), or a benchmark recipe (I5).

For every finding cite the current reachable caller-to-consequence chain and
defeat applicable safe harbors. Distinguish static proof from observed runtime
events. Prior clears and same-byte helpers require current caller/configuration
checks; unchanged ancestors are separately reviewed, never rejected by lineage.

Record concise path:line evidence in the existing review record. Do not call
missing reviewer-created records miner misconduct. For a bounded operator
review with unresolved mandatory evidence, leave the row unchanged and report
the blocker under policy-version routing. A proved breach may be decided
without clearing unrelated paths. After exact-artifact enforcement, re-read
the leaderboard and report any predecessor re-entry; do not expand the batch.
