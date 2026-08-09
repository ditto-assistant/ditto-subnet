# Source-review causal evidence v2

`SourceReviewFinding` keeps its original location-only payload as evidence
schema v1. A finding opts into schema v2 only by supplying `causal_evidence`.
Historical v1 JSON and canonical digest bytes are unchanged.

V2 binds a bounded authority transition and artifact-visible role bindings for:

- `served_trigger`
- `authority_bypass`
- `scorer_visible_effect`
- `reachability_link`

Every binding must reference an existing `path`, `line`, and `category` in the
finding's public-safe evidence list. The canonical form sorts role bindings, so
equivalent model output ordering has one digest, while every role, location,
category, or authority-transition change produces a different digest.

The authority transition is one of `model_skipped`, `model_output_overwritten`,
`tool_execution_bypassed`, `tool_trajectory_fabricated`,
`selective_model_disablement`, or `scorer_field_rewritten`. Unknown transition
or role names, duplicate bindings, and over-budget collections fail validation.
Every v2 object also names the concrete `scorer_visible_effect`: `final_text`,
`answer`, `abstain`, `tool_calls`, `validator_observed_trajectory`, or
`graded_outcome`. The effect is part of canonical signing bytes. Reviewer
policy validates that it is compatible with the authority transition and emits
the public-safe summary from that exact pair; generic or tampered causal wording
cannot become authoritative.

Parsing v1 remains permissive by design. A reviewer integration that is ready
to enforce causal proof must explicitly call
`require_role_complete_causal_evidence()`. For `benchmark_emulation` and
`scorer_contract_manipulation`, that check requires all four roles and at least
two distinct causal locations per category. This explicit activation boundary
prevents a library update from retroactively invalidating stored v1 findings.
