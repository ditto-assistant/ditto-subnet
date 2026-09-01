# Shadow coding result submission

The validator client mirrors Platform's existing signed terminal-result route:

```text
POST /api/v1/validator/agent/{agent_id}/coding-shadow-result
```

The signature binds the validator and agent, run and ticket UUIDs, benchmark
version, ticket deadline, exact agent and screened-image digests, and canonical
known-field run-evidence digest. The request model also requires the evidence's
validator ticket identity to equal the envelope ticket before any HTTP request.
Before signing, the client replays the aggregate against the exact run manifest
and every per-task evidence root and checks the manifest's agent and artifact.

The client disables redirects, never includes a rejected response body in an
error, streams the response under a 64 KiB ceiling, and accepts it only when the
agent, run, ticket, and coding-run identities match the submitted evidence.
Platform remains the durable idempotency authority: exact replay returns the
same accepted result, while different evidence for one ticket conflicts.

Result acceptance is evidence-gated in the insertion transaction. Every path
requires the exact raw terminal request and a finalized ticket-generation
transcript. A result with an accepted authoring freeze additionally requires
the finalized frozen submission, authoring request, and exact authoring
acknowledgement. The acknowledgement may be either canonical response body the
worker could have received (`idempotent=false` initially or `idempotent=true`
after transport recovery). No-freeze terminal infrastructure paths require only
transcript plus terminal request. The post-response terminal acknowledgement is
deliberately absent from this gate and completes the claim later.

## Activation

The default-off coding worker calls this method only after phase-valid evidence
finalization. It does not independently claim a coding job,
start the coding harness, fetch artifacts, invoke Luna, execute the pristine
grader, write an ordinary score, affect rank, or set weights. It only completes
the signed shadow transport. Coding contract v1 and
all submitted results remain permanently `weight_eligible=false`.
