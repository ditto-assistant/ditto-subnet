# Shadow coding failure classification

`ditto.validator.coding_failure` maps typed execution stages to canonical
terminal domains. Callers cannot choose a terminal domain directly.

| Typed stage | Required failure code | Terminal domain | Component evidence |
|---|---|---|---|
| `post_lease_transport` | `post_lease_transport` | `validator_infrastructure` | authoring and grader forbidden |
| `task_material` | `task_material_invalid` | `task_invalid` | authoring and grader forbidden |
| `authoring_infrastructure` | `authoring_runtime` | `validator_infrastructure` | authoring and grader forbidden |
| `candidate_integrity` | `candidate_policy_violation` | `candidate_integrity` | authoring required; failed grader optional and requires gradeable authoring |
| `grading_infrastructure` | `grading_runtime` | `validator_infrastructure` | gradeable authoring required; grader forbidden |
| `repair_failure` | `grader_tests_failed` | `repair_failure` | gradeable authoring and failed grader required |
| `control_plane_integrity` | `control_plane_mismatch` | `control_plane_integrity` | authoring optional; grader forbidden |

Every output is reconstructed from the selected run manifest, ticket, agent,
artifact, task-set, and exact task identity, assigned zero repair score and a
stage-specific typed failure code, then passed through the authority-aware
task-evidence digest validator. Raw exception text and cross-stage codes are
rejected. Resolved grader evidence is rejected for both repair failure and
candidate integrity. Any stage carrying grader evidence must also prove the
complete, protected, changed-path authoring freeze that allowed grading.

A transport failure before the authoring lease arrives is deliberately absent:
without the selected run manifest there is no task authority to sign or submit.
That condition remains retryable validator infrastructure rather than fabricated
terminal evidence.

## Activation

The classifier remains a typed boundary for later terminal reconciliation; the
default-off success-path worker does not infer stages from exception prose. It
does not run a miner, retrieve private artifacts,
invoke Luna, execute a grader, submit an ordinary score, affect rank, or set
weights. Coding contract v1 remains permanently `weight_eligible=false`.
