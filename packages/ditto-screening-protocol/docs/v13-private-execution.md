# V13 protected pair provisioning and execution

`v13_private_provision.py` consumes an operator-protected, semantics-reviewed
blueprint bank **after** Platform has committed the exact attempt, artifact,
and verified image. It draws two independent CSPRNG seeds, selects disjoint
pairs using keyed HMAC ordering, stores seed bytes and case payloads only in a
sealed private store, writes a content-addressed manifest, and registers the
digest-only package last. A missing bank, insufficient class coverage, or
failed store write prevents registration. The public repository deliberately
contains no private blueprints or case values.

`v13_private_execute.py` loads the registered package, rechecks each payload
digest at execution, and evaluates every control/variant side through an
interface requiring fresh isolated state and a trusted model-authority receipt.
It reports only per-class/per-seed counts and a digest of pair outcomes. It
does **not** calculate a policy effect, confidence interval, multiple-comparison
result, or violation. A failed case, missing model authority, or bad payload
leaves verification incomplete.

This PR does not supply the protected bank, sealed storage/registry adapter,
or production sandbox adapter. Before activation, those must be provisioned
with a private challenge authoring review, a Platform-owned append-only
registration bound to the immutable attempt SHA and verified image, a sandbox
that creates separate container/network/broker state per side, and audited
statistical analysis. Public benchmark datagen cannot substitute for the
private bank. None of these primitives authorizes V13 CLEAR or REJECT; the
other mandatory checks and exact held-artifact replay remain separate gates.
