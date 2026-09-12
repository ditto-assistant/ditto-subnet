# Policy v13 opaque-component verification

Status: required companion specification for any policy-v13 activation that
admits submission-controlled opaque components.

This specification determines which evidence an opaque component must provide.
It does not make opacity suspicious and does not permit a component merely
because it has a digest or familiar file format.

## Common declaration

For each opaque component loaded anywhere in the approved lifecycle, record:

- cryptographic digest and byte size;
- effective build and runtime load site;
- configuration selecting it;
- input and output schemas;
- whether inputs contain private user data;
- whether outputs can remove evidence or capabilities, select a plan, execute
  code, or affect a scored field;
- expected failure behavior and downstream fallback;
- producer and public provenance when available.

Hosted inference records the provider, model identifier/version, endpoint trust
boundary, and inference receipt supported by the integration contract. A model
weight digest is not required when the approved provider does not expose one.

## Role requirements

### Unloaded or unreachable component

Required evidence:

- complete build/configuration/load-site closure proving no authorized lifecycle
  path loads or interprets the bytes; and
- proof that enabling it changes the approved configuration envelope.

No private behavioral test is required. This finding clears only the component's
reachability lead, not the complete artifact.

### Presentation-only permutation

Required evidence:

- source proof that downstream logic receives the same complete material set;
- stable item identity across the boundary; and
- tests demonstrating that reordering does not become hidden truncation,
  filtering, deduplication, or selection.

A ranker followed by top-k selection is an evidence selector, not
presentation-only.

### Evidence selector or capability router

Required evidence:

- declaration of the candidate set before and after the component;
- source/instrumentation proving the component's output controls selection;
- private paired tests covering applicable corrections, qualifications,
  paraphrases, quoted entities, compound requests, and relevant decoys;
- predeclared retention/materiality thresholds and clean controls; and
- evidence that evaluation identity is not an input or proxy input.

An isolated miss is a quality result. An integrity finding still requires the
I2, I5, I7, or I8 causal standard.

### Advisory candidate or critic

Required evidence:

- downstream deciding authority receives necessary originals and can disagree;
- rejection, uncertainty, and refusal remain valid outcomes;
- no hidden expected-answer acceptance loop; and
- tests with deliberately dissenting candidates.

### Authoritative planner

Required evidence:

- exact planner identity and immutable configuration;
- complete applicable catalog and authorization context at planning time;
- plan output and execution receipts;
- safe stopping/replanning after failed or surprising results;
- private tests over tool aliases, catalog order, compound tasks, no-call
  outcomes, and revoked authorization; and
- proof that active evaluation identity is not an input or routing key.

Pinned deterministic execution is permitted only after this authority is
established.

### Scored-output component

Required evidence:

- field-level input/output authority map;
- proof that finalized model-authored fields are copied faithfully;
- tests for `unknown`, `none`, `n/a`, signed values, multiple units, Unicode,
  wrappers, and long valid values; and
- confirmation that no miner-defined limit changes field precedence.

### Executable loader or generated executable

Required evidence:

- accepted inert format or isolated loader contract;
- source and destination trust boundaries;
- filesystem, network, subprocess, privilege, persistence, and resource limits;
- failure and cancellation behavior; and
- controlled security tests when untrusted bytes can influence execution.

Unsafe executable deserialization or undeclared executable substitution is a
security finding, not an opaque-model classification issue.

## Behavioral package

Where a role above requires private testing, the package must be:

- committed after the artifact and before challenge randomness is revealed;
- versioned and content-addressed;
- predeclared for sample size, effect threshold, confidence, replication, and
  multiple-comparison handling;
- executed with paired clean controls through equivalent infrastructure;
- semantics-preserving for public schemas, identities, relationships, units,
  chronology, authorization, and tool behavior; and
- replicated across independent hidden rotations and fresh task compositions.

Ordinary degradation establishes a robustness result. An integrity rejection
requires attribution to a prohibited mechanism under policy v13.

## Accepted equivalent evidence

An equivalent route is valid only when this specification or a versioned
successor names it before the artifact is reviewed. A human assertion that a
missing mandatory test is unnecessary is not an equivalent route.

Complete deterministic source causality may replace behavioral testing only
when the applicable role requirement says source proof is sufficient and the
review covers the entire effective downstream path.

## Failure treatment

- Missing required submission evidence is `V1.required_evidence_missing`.
- Verification that remains incomplete after published retries/deadlines is
  `V2.verification_not_completed`.
- V1/V2 do not establish misconduct and record `violation_proven: false`.
- Infrastructure or provider failure remains attributable to that failure
  domain and receives the published re-review treatment.
