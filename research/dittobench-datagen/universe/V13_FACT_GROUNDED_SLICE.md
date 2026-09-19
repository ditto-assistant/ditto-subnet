# Fact-grounded V13 owner slice

`GenerateV13OwnerSlice(worldSeed, presentationSeed)` is an opt-in research
entry point. It emits the existing six-record / four-member owner program
shape with normal grading claims, evidence IDs, and counterfactual scoping.
It is not wired into the private producer or rollout qualification.

## Boundary

- World events contain entity, person, and chronology. The evaluator derives
  the answer without inspecting prose or presentation randomness.
- Rendering consumes that world, preserving proper names and explicit
  supersession. Both the question and records reference the same entity/role.
- The renderer rejects unsupported histories instead of dropping events.
- Independent presentation entropy selects sentence forms and clauses. The
  existing outer record wrappers add format variation. These are finite,
  reviewed compositions, not unconstrained random language or an LLM rewrite.
- Milestone information and the existing unrelated-entity distractor provide
  bounded irrelevant content. No intentional typos are introduced.
- The causal member changes one assignment; the other members preserve truth.
- Public seeds are reproducible, not secret. Keeping presentation entropy
  private prevents exact public-seed replay but does not establish resistance
  to generalized parsers. Never claim privacy from seed variation alone.

## Evidence and limits

Tests cover 200 world seeds × 12 presentation seeds × four members, repeated
generation, grading/evidence agreement, and preservation of the default
generator. Another 1,000 presentations check that a causal mutation changes
only its selected fact. Negative tests reject ambiguous chronology, missing
evidence, and unsupported render shapes.

This validates structural semantics, not an independent natural-language
interpretation. Independent semantic checks and honest/adversary evaluation
on these exact records remain required. Other six business families, personal
records, story/quantity questions, and tools are not migrated by this slice.

## Next integration gates

1. Independently inspect the rendered slice and run semantic evaluation;
   fix renderer defects rather than changing the world-derived answer.
2. Run the honest agent and held-out shortcut adversary against the same
   complete slice with provider failures classified separately from scores.
3. Migrate remaining families with equivalent typed evaluators and relation
   tests. Add private rendering entropy to artifact provenance/validation
   atomically; do not feed this output through the old blind rewrite stage.
4. Requalify the full exact artifact before private canary or rollout.

No existing public/private qualification receipt applies to this new revision.
