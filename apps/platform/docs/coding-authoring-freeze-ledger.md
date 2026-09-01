# Shadow coding authoring-freeze ledger

Platform exposes one signed, append-only transition:

```text
POST /api/v1/validator/coding-shadow/authoring-freeze
```

The validator signs the agent, run, ticket, deadline, coding run, screened
artifact, run/task-set manifests, canonical authoring-evidence digest, and the
content-addressed transcript and frozen-submission references. Platform
revalidates current chain permission, exact ticket ownership, artifact and
manifest identity, the ticket-bound inference grant, and certification
lifetime before inserting one row.

The same transaction locks the ticket generation's sealed-evidence
reservations and finalizations. It requires the exact transcript digest and
byte size, the frozen-submission digest, and the SHA-256 and byte size of the
raw signed HTTP request. Re-serializing parsed JSON is not accepted. Missing,
wrong-generation, pending, or changed evidence rejects both new inserts and
idempotent replay.

The signed transition also carries the transcript byte size and event count.
They must be jointly zero or jointly nonzero and remain within the runner's
512 MiB and 1,000-event hard ceilings.

One ticket can name only one freeze. Exact replay is idempotent; changing any
authoring evidence or object reference conflicts. A finalized result cannot be
backfilled with a later freeze. Once a freeze exists, even an empty patch or
candidate-integrity outcome stays immutable so authoring cannot be retried
after authoritative activity.

The ledger stores complete known-field authoring evidence for later integrity
comparison. The admin evaluation view exposes only bounded hashes, counts, the
protected-path flag, and freeze time. It does not expose model receipts,
transcript bytes, frozen patch bytes, object keys, repository content, or
private task identity.

For coding contract v1, authoring evidence is accepted only for the locked
`openai/gpt-5.6-luna` / `azure/eu` / `luna-azure-eu-zdr-v1` route with medium
reasoning. A validator signature cannot substitute another solver or route;
that requires a new immutable coding contract.

## Trust boundary

The row records a validator-signed claim produced after the trusted runner
freezes authoring. Platform cannot independently prove that a validator-local
process stopped, but it does prove that the exact claimed bytes were fully
verified by the dedicated evidence finalizer before accepting the freeze. The
execution orchestrator remains responsible for
revoking the workspace and inference capabilities before submitting the
freeze, persisting bytes under the declared content addresses, and destroying
the authoring environment.

This endpoint returns no artifact capability. The separate grading-lease route
requires the exact stored freeze and may release visible, resource, and grader
artifacts only when the freeze is gradeable. It rechecks phase authority after
URL minting and never checks or signs the memory bundle.

## Activation boundary

The default-off validator worker calls this endpoint only after finalizing the
required sealed evidence. The endpoint does not start Luna, materialize a
workspace, grade a patch, write an ordinary score, or
affect emissions. Every row and lease is permanently `weight_eligible=false`.
