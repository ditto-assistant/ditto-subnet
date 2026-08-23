# Shadow coding grading-lease delivery

Platform exposes one signed validator-only route:

```text
POST /api/v1/validator/coding-shadow/grading-lease
```

The request binds the validator, agent, run, ticket, immutable authoring freeze,
canonical authoring-evidence digest, one-use nonce, and fresh UTC timestamp.
Platform verifies the signature and current chain permission, retains the
nonce until the signed timestamp leaves the five-minute freshness window,
and authorizes the exact stored freeze before reading the private catalog
or signing any object URL.

A freeze is gradeable only when its ticket is active, no terminal result exists,
the run and freeze are coding contract v1 and shadow-only, the authoritative
transcript and event counts are nonzero, at least one path changed, protected
paths remain intact, and locked-model usage is complete. The response binds the
run manifest and task-set digests, freeze and evidence digests, frozen patch and
content-addressed submission key, ticket deadline, and exactly three short-lived
capabilities in canonical order:

```text
visible-bundle
resource-profile
grader-bundle
```

The freeze-gated response also carries the canonical protected grader-plan and
resource-profile preimages. Platform checks their digests against the selected
task, the compiled grader-v1 contract, and the case/variant/bundle/image/test
identity. The authoring runner plan, memory material, and inference grant are
structurally absent.

The grading-only minter never checks or signs the memory bundle. Platform
rechecks the active artifact certification, ticket, and freeze after URL
minting and discards the bearer URLs if authority changed. Responses are
`Cache-Control: no-store`; URL values are excluded from model reprs and errors.
The validator client also requires the locally frozen patch SHA-256 and rejects
a coherent-looking response that names different patch bytes.

## Trust boundary

This route authorizes artifact transport. It does not fetch or extract bytes,
apply the frozen patch, run the grader, submit a result, or prove that a
validator-local authoring process stopped. The future validator supervisor must
stream each object within its declared bound, verify its SHA-256 before use,
materialize a pristine workspace, apply the exact frozen submission, and expose
the grader bundle only to the protected networkless grader.

This process boundary prevents the miner harness from receiving grader bytes
and prevents bulk catalog distribution. It cannot make an assigned grader
bundle permanently confidential from the operator of the validator host; that
stronger property would require remote grading or an attested confidential
runtime.

The route and validator client are intentionally unused in production. There is
no scheduler, worker, Luna relay, grader execution, ordinary score write,
ranking input, or emissions effect in this change. Coding contract v1 remains
permanently `weight_eligible=false`.
