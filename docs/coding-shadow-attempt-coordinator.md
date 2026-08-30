# Shadow coding attempt coordinator

`ditto.validator.coding_attempt` defines an unused, dependency-injected
coordinator for one gradeable coding contract v1 task. It fixes the order:

1. reject a ticket that is already expired;
2. request and verify the authoring lease;
3. run authoring through a trusted runtime port;
4. require revoked capabilities, a destroyed authoring environment, nonempty
   authoritative activity, intact protected paths, and a content-addressed
   frozen submission;
5. submit and verify the immutable authoring freeze;
6. recheck the deadline, then request a freeze-bound grading lease;
7. verify the patch and manifest authority before pristine grading;
8. replay the run manifest and every per-task evidence root, then submit the
   signed shadow result.

The runtime returns per-task evidence only. The validator-owned terminal
builder derives task order, evidence roots, domain counts, scoreable count, and
binary repair mean; runtime-reported aggregate values are not accepted.

The runtime port must provide idempotent authoring and grading abort operations.
The coordinator shields and invokes the relevant abort on any runtime exception
or cancellation before propagating the failure. Once a runtime returns a normal
outcome, its typed result attests that the phase capabilities were revoked and
the environment was destroyed.

This coordinator deliberately handles only the complete gradeable path. A
future worker must classify pre-authoritative, task-invalid, candidate-integrity,
and control-plane failures into canonical evidence without granting a clean
retry or releasing grader material early.

The separate typed failure classifier now defines those post-lease mappings and
component-evidence requirements. It remains unwired; a future worker must decide
when a trusted runtime stage occurred rather than classifying exception text.

## Activation

No composition root imports or starts the coordinator. The runtime is a
protocol, not a Docker, scorer, or relay implementation. This change does not
claim tickets, fetch artifacts, start a miner, invoke Luna, grade production
code, submit ordinary scores, rank agents, or set weights. Coding contract v1
remains permanently `weight_eligible=false`.
