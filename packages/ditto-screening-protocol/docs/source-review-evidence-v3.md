# Source-review invariant evidence v3

Screening policy v10 adds an optional `invariant_assessment` to
`SourceReviewFinding`. Its presence makes the effective evidence schema v3;
historical v1/v2 findings and canonical digests remain byte-identical when it is
absent.

Policy v13 extends the nested assessment without changing historical bytes.
Nested schema version 1 contains the policy-v10-v12 I1-I7 sweep. Nested schema
version 2 contains I1-I8 and adds evaluation independence. Producers emit the
schema for the policy that reviewed the artifact; consumers accept both so
stored policy-v10-v12 findings remain verifiable.
Policy-v13 producers cap each invariant summary at 210 characters and the eight
summaries at 1,680 characters total so the complete finding still fits the
existing 8 KiB worker boundary.

The assessment contains exactly one decision for each published invariant:

- `i1_model_invocation`
- `i2_evidence_retention`
- `i3_model_dissent`
- `i4_derived_value_authority`
- `i5_production_engine`
- `i6_tool_execution_fidelity`
- `i7_model_tool_planning`
- `i8_evaluation_independence` (nested schema version 2 only)

Each decision is `pass`, `breach`, or `inconclusive`. A pass must select an
invariant-compatible, closed-domain `pass_clause` and cannot
carry violation evidence. A breach must reference one or more zero-based
`evidence_indices` from the finding's bounded evidence list. The canonical
projection sorts decisions by invariant and evidence indices numerically, so
model output ordering cannot change the finding identity without duplicating
the source locations and exceeding the existing 8 KiB finding boundary.

Policy-v10 integrations call `require_policy_v10_invariants()`. Low-risk
findings cannot carry a breach or inconclusive invariant; strict categories
such as benchmark emulation, scorer-contract manipulation, and fabricated tool
trajectories cannot appear without an elevated invariant decision. This is a
signed triage contract, not automatic rejection: medium/high findings select
quarantine and remain subject to L2/L3 and operator adjudication.

V3 also extends the causal transition domain for the strict court:

- `derived_value_authoritative`
- `family_compiler_authoritative`
- `tool_selection_predetermined`

Those transitions let L2/SOL bind an I4, I5, or I7 violation without falsely
claiming that the model was skipped or its final string mechanically overwritten.
