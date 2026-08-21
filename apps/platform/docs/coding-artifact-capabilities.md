# Shadow coding artifact capabilities

Platform can mint four short-lived, ticket-bounded object capabilities from one
verified shadow task-lease core: visible repository bundle, scoped memory
bundle, resource profile, and private grader bundle.

Object keys are fixed and content-addressed:

```text
coding-artifacts/v1/<artifact-kind>/sha256/<digest>
```

Callers cannot provide a key, prefix, bucket, URL, or object kind. Platform
checks each object with `HEAD` before signing and requires matching `sha256` and
`artifact-kind` metadata plus a positive per-kind bounded size. URLs expire at
the smaller of the configured ceiling and remaining ticket lifetime, use HTTPS
outside explicit loopback development, and are excluded from object reprs and
logs. Platform parses the signer's S3 expiry and rejects a URL that exceeds its
requested TTL or ticket deadline. Storage keys stay inside the minter and are
not part of the returned capability projection.

`HEAD` is only a bounded signing preflight, not proof of downloaded bytes.
Every future consumer must stream within the declared size bound, recompute the
SHA-256 before extraction or use, and reject any mismatch with the capability
digest. The object store must enforce immutable writes for this namespace.

The complete capability set is server-internal and must never be serialized as
one validator or miner response. A future orchestrator projects the visible
bundle only to the trusted workspace materializer, the memory bundle only to
the trusted seed projector, and the resource profile only to the enforcing
supervisor. It releases the grader capability only to the protected grader and
only after authoring is frozen; none of these bearer URLs enters the miner
harness or model context.

The validator-only `dittobench-coding-artifact-capability-v1` wire is frozen by
`packages/dittobench-coding-contract/testdata/coding_artifact_capability_v1.json`.
Authoring permits visible, memory, and resource artifacts. Grading permits
visible, resource, and grader artifacts. The shared Python/Go models reject
grader delivery during authoring and memory delivery during grading. The
synthetic vector URL is not part of canonical identity and is not consumed by
Rust or miner code.

Environment and grader images remain immutable OCI digests. Base-tree,
test-manifest, and grader-plan hashes remain identities within their parent
bundles rather than separately presigned objects.

## Activation boundary

The authenticated authoring-lease route may project visible, memory, and
resource capabilities for one already-issued explicit shadow ticket. Its
authoring-only minter never checks or signs the grader object. No scheduler or
validator worker invokes the route, and there is no workspace capability, Luna
grant, execution, scoring, deployment, or emissions effect. Coding contract v1
remains permanently `weight_eligible=false`.
