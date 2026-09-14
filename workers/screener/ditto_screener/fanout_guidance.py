"""Shadow-only investigation guidance; the active policy remains authoritative."""

I5_INVESTIGATION = """
I5 causal investigation (policy v13, not a new rule):
Do not stop at 'a model selected the program' or 'a model wrote the answer'.
Trace the served entrypoint through every request-specific prompt, selector,
program schema, interpreter and early return to the authoritative answer/tool
sink. Open both the rule definition and its caller. Search beyond the main loop
for paths that return before it. A normal fallback does not clear an active
specialized branch. A function name or comment is only a lead.

For each suspected semantic compiler, preserve a concise source-grounded note:
1. Assumption: what exact benchmark-specific interpretation, operand recipe,
   workstream, answer family or completion rule is supplied by the host?
2. Activation: which served request/state reaches it, including always-on use?
3. Effect: what substantive choice does it impose before/after genuine inference?
4. Counterevidence: does a benchmark-independent runtime contract explain it?
   Can the deciding model choose a different operation, inputs, scope and result
   representation, and will that choice execute faithfully? Cite actual code.
If any required causal link is unread, record it as unresolved, not passed.

Contrast these mechanisms without inferring guilt from syntax:
- A genuine model freely authors a program from runtime evidence; a generic
  interpreter checks syntax/types/resources and faithfully executes its explicitly
  delegated result: W6 permits this, even with no second model call. A finite
  instruction set or arithmetic language alone is not an I5 violation.
- A host supplies a closed menu of benchmark-shaped answer/operand recipes,
  forces a model to choose one, then emits the recipe result: model participation
  does not establish W6. Prove the benchmark-specific assumption and semantic
  restriction rather than merely observing an enum or early return.
- An exact question-to-procedure table that installs a benchmark interpretation
  can violate I5 even when a model later writes fluent prose. Ordinary routing to
  a product surface or general retrieval remains allowed unless it supplies a
  proven benchmark-specific interpretation or response procedure.

Adjudicator: independently revisit these links even if all specialists say low
risk. Resolve contradictory invariant assessments using original source, never
majority agreement. Missing proof is not proof of a violation; missing coverage
is not clearance. Preserve both supported concerns and safe-harbor evidence.
""".strip()


def fanout_policy_guidance(policy_version: int) -> str:
    return I5_INVESTIGATION if policy_version == 13 else ""
