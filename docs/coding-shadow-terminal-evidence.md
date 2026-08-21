# Shadow coding terminal evidence builder

`ditto.validator.coding_terminal.build_coding_run_evidence` makes terminal
aggregation validator-owned. A runtime adapter may return only canonical
per-task evidence; it cannot report run counts, scoreable cardinality, repair
mean, task order, or task evidence roots.

The builder reparses the manifest and tasks through their known-field wire
models, rejects missing or duplicate task identities, orders results by the
immutable run manifest, and derives each task root with the manifest and
validator ticket. It then reproduces all six terminal-domain counts:

```text
resolved
repair_failure
validator_infrastructure
task_invalid
candidate_integrity
control_plane_integrity
```

Only resolved, repair-failure, and candidate-integrity tasks are scoreable. The
shadow binary repair mean is computed with integer arithmetic:

```text
scoreable = resolved + repair_failure + candidate_integrity
repair_mean_micros = floor(resolved * 1_000_000 / scoreable)
```

When `scoreable == 0`, the mean is zero. The completed aggregate is replayed
again against every task before it can reach the signed result client.

## Activation

The builder is used only by the still-unwired shadow attempt coordinator. No
runtime scheduler, worker, miner execution, ordinary score, rank, or emissions
path imports it. Coding contract v1 remains permanently
`weight_eligible=false`.
